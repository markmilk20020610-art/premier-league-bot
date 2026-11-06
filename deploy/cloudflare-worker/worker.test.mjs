/**
 * worker.js 的行为测试（Node 原生 ESM，不依赖 wrangler / miniflare）。
 *
 * 为什么要有这个文件：worker.js 是生产调度链路上唯一一个**不在 Python 测试覆盖范围内**
 * 的组件，而它恰恰是「调度风暴」和「审核回调被伪造」这两个最危险缺陷的所在地。
 * 只用正则扫源码证明不了「连续 5 分钟只会 dispatch 一次」，必须真的跑。
 *
 * 做法：
 *   · 把 worker.js 读成字符串，用 data: URL 动态 import（避免给仓库加 package.json）
 *   · 覆盖 globalThis.fetch，把所有外部调用（GitHub API / Telegram）拦在本地
 *   · 用内存 Map 冒充 KV，验证「持久化」语义真的被用到
 *   · 覆盖 Date.now，让「冷却窗口」「过期按钮」可以确定性复现
 *
 * 用法：node worker.test.mjs        （退出码 0 = 全绿）
 *       node worker.test.mjs --json （输出机器可读结果，供 Python 测试消费）
 */
import fs from "node:fs";
import { fileURLToPath } from "node:url";

const HERE = fileURLToPath(new URL(".", import.meta.url));
const SRC = fs.readFileSync(HERE + "worker.js", "utf8");
const MOD = await import(
  "data:text/javascript;base64," + Buffer.from(SRC, "utf8").toString("base64")
);
const worker = MOD.default;

/* ========================= 断言框架 ========================= */
const results = [];
function check(name, cond, detail) {
  results.push({ name, ok: !!cond, detail: detail == null ? "" : String(detail) });
}
function eq(name, actual, expected, note) {
  const ok = actual === expected;
  check(name, ok, ok ? "" : `${note || ""} 期望=${expected} 实际=${actual}`);
}

/* ========================= 时间控制 ========================= */
const REAL_NOW = Date.now;
let NOW = Date.parse("2026-11-06T02:00:00Z");
Date.now = () => NOW;
function at(iso) {
  NOW = Date.parse(iso);
}
function advance(minutes) {
  NOW += minutes * 60000;
}

/* ========================= fetch 拦截 ========================= */
let routes = [];       // [{match, handler}]
let calls = [];        // 全部请求记录

function on(substr, handler) {
  routes.push({ match: substr, handler });
}

globalThis.fetch = async (url, init = {}) => {
  const u = String(url);
  const method = (init.method || "GET").toUpperCase();
  calls.push({ url: u, method, body: init.body || null, headers: init.headers || {} });
  for (const r of routes) {
    if (u.includes(r.match)) return r.handler(u, init);
  }
  throw new Error("未拦截的请求: " + method + " " + u);
};

function res(status, body) {
  const text = typeof body === "string" ? body : JSON.stringify(body);
  return {
    status,
    ok: status >= 200 && status < 300,
    async text() {
      return text;
    },
    async json() {
      return typeof body === "string" ? JSON.parse(body) : body;
    },
  };
}

function resetRoutes() {
  routes = [];
  calls = [];
}

function dispatchCalls() {
  return calls.filter((c) => c.url.includes("/dispatches"));
}

function dispatchedInputs() {
  return dispatchCalls().map((c) => JSON.parse(c.body).inputs);
}

/* ========================= KV 冒充 ========================= */
function makeKV() {
  const m = new Map();
  return {
    _map: m,
    async get(k) {
      return m.has(k) ? m.get(k) : null;
    },
    async put(k, v) {
      m.set(k, v);
    },
    async delete(k) {
      m.delete(k);
    },
  };
}

/* ========================= env 工厂 ========================= */
const REPO = "markmilk20020610-art/premier-league-bot";

function baseEnv(over = {}) {
  return {
    REPO,
    WORKFLOW_FILE: "scanner.yml",
    DISPATCH_REF: "main",
    GITHUB_TOKEN: "ghp_fake",
    TELEGRAM_BOT_TOKEN: "123:fake",
    TELEGRAM_OPS_CHAT_ID: "-100777",
    TELEGRAM_WEBHOOK_SECRET: "whsec_fake",
    TELEGRAM_REVIEW_CHAT_ID: "-100888",
    REVIEW_OPERATOR_IDS: "111,222",
    REVIEW_CALLBACK_HMAC_SECRET: "hmac_secret_fake",
    FAILOVER_MINUTES: "12",
    DISPATCH_COOLDOWN_MINUTES: "10",
    ALERT_MINUTES: "30",
    READ_FAIL_ALERT_MINUTES: "8",
    MORNING_GRACE_MINUTES: "180",
    OVERSIGHT_KV: makeKV(),
    ...over,
  };
}

function ctx() {
  const pending = [];
  return {
    waitUntil(p) {
      pending.push(p);
    },
    async drain() {
      await Promise.all(pending);
    },
  };
}

/* state.json 心跳 */
function stateHandler(state) {
  return () => res(200, state);
}
function heartbeatAgo(minutes, extra = {}) {
  return {
    last_scan_at: new Date(NOW - minutes * 60000).toISOString(),
    last_morning_report_date: "2026-11-06",
    ...extra,
  };
}
/* run 列表 */
function runsHandler(runs) {
  return () => res(200, { workflow_runs: runs });
}
function runAgo(minutes, over = {}) {
  return {
    id: 5000 + Math.round(minutes),
    status: "in_progress",
    created_at: new Date(NOW - minutes * 60000).toISOString(),
    ...over,
  };
}

/* HMAC（与 worker.js 内部算法一致，用来伪造合法/非法按钮） */
async function sig(secret, ver, action, eventId, exp) {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const buf = await crypto.subtle.sign("HMAC", key, enc.encode(`${ver}|${action}|${eventId}|${exp}`));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 20);
}

async function makeCallback(eventId, action, { ttlSec = 3600, secret = "hmac_secret_fake", ver = "v1" } = {}) {
  const exp = Math.floor(NOW / 1000) + ttlSec;
  const s = await sig(secret, ver, action, eventId, exp);
  return `${ver}:${action}:${eventId}:${exp}:${s}`;
}

function telegramUpdate({ data, fromId = "111", chatId = "-100888", cqId = "cq1" }) {
  return {
    update_id: 1,
    callback_query: {
      id: cqId,
      from: { id: Number(fromId) },
      message: { message_id: 42, chat: { id: Number(chatId) } },
      data,
    },
  };
}

async function postWebhook(env, update, headers = {}) {
  const req = new Request("https://w.example/", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "X-Telegram-Bot-Api-Secret-Token": env.TELEGRAM_WEBHOOK_SECRET,
      ...headers,
    },
    body: JSON.stringify(update),
  });
  const c = ctx();
  const r = await worker.fetch(req, env, c);
  const body = await r.json();
  await c.drain();
  return { status: r.status, body };
}

async function tick(env, { force = false, dry = false } = {}) {
  const q = force ? "?force=1" : dry ? "?dry=1" : "";
  const req = new Request("https://w.example/" + q, { method: "GET" });
  const c = ctx();
  const r = await worker.fetch(req, env, c);
  const body = await r.json();
  await c.drain();
  return body;
}

/* ==========================================================================
 * A3：Telegram webhook 校验链
 * ==========================================================================*/
async function testWebhook() {
  /* 1. 未配置 Webhook Secret → 503，绝不接受 */
  resetRoutes();
  {
    const env = baseEnv({ TELEGRAM_WEBHOOK_SECRET: "" });
    const r = await postWebhook(env, telegramUpdate({ data: "x" }));
    eq("A3-1 缺 webhook secret 时拒绝", r.status, 503);
    eq("A3-1 缺 webhook secret 时不 dispatch", dispatchCalls().length, 0);
  }

  /* 2. Secret Token 不对 → 401 */
  resetRoutes();
  {
    const env = baseEnv();
    const data = await makeCallback("abcdef123456", "a");
    const r = await postWebhook(env, telegramUpdate({ data }), {
      "X-Telegram-Bot-Api-Secret-Token": "wrong",
    });
    eq("A3-2 错误 secret token 拒绝", r.status, 401);
    eq("A3-2 错误 secret token 不 dispatch", dispatchCalls().length, 0);
  }

  /* 3. 非 callback_query → 200 忽略（否则 Telegram 会重投） */
  resetRoutes();
  {
    const env = baseEnv();
    const r = await postWebhook(env, { update_id: 2, message: { text: "hi" } });
    eq("A3-3 非回调消息返回 200", r.status, 200);
    eq("A3-3 非回调消息被忽略", r.body.ignored, "not_callback_query");
    eq("A3-3 非回调消息不 dispatch", dispatchCalls().length, 0);
  }

  /* 4. 操作人不在白名单 → 拒绝且不 dispatch */
  resetRoutes();
  {
    const env = baseEnv();
    on("/answerCallbackQuery", () => res(200, { ok: true }));
    const data = await makeCallback("abcdef123456", "a");
    const r = await postWebhook(env, telegramUpdate({ data, fromId: "999" }));
    eq("A3-4 非白名单操作人被拒", r.body.rejected, "operator_not_allowed");
    eq("A3-4 非白名单操作人不 dispatch", dispatchCalls().length, 0);
  }

  /* 5. 回调不是来自审核群 → 拒绝 */
  resetRoutes();
  {
    const env = baseEnv();
    on("/answerCallbackQuery", () => res(200, { ok: true }));
    const data = await makeCallback("abcdef123456", "a");
    const r = await postWebhook(env, telegramUpdate({ data, chatId: "-100999" }));
    eq("A3-5 非审核群回调被拒", r.body.rejected, "not_review_chat");
    eq("A3-5 非审核群回调不 dispatch", dispatchCalls().length, 0);
  }

  /* 6. 签名错 → 拒绝（不能仅凭 event_id 就接受批准） */
  resetRoutes();
  {
    const env = baseEnv();
    on("/answerCallbackQuery", () => res(200, { ok: true }));
    const exp = Math.floor(NOW / 1000) + 3600;
    const forged = `v1:a:abcdef123456:${exp}:${"0".repeat(20)}`;
    const r = await postWebhook(env, telegramUpdate({ data: forged }));
    eq("A3-6 伪造签名被拒", r.body.rejected, "bad_callback");
    eq("A3-6 伪造签名不 dispatch", dispatchCalls().length, 0);
  }

  /* 7. 过期按钮 → 拒绝（旧按钮重放） */
  resetRoutes();
  {
    const env = baseEnv();
    on("/answerCallbackQuery", () => res(200, { ok: true }));
    const data = await makeCallback("abcdef123456", "a", { ttlSec: -60 });
    const r = await postWebhook(env, telegramUpdate({ data }));
    eq("A3-7 过期按钮被拒", r.body.rejected, "bad_callback");
    eq("A3-7 过期按钮不 dispatch", dispatchCalls().length, 0);
  }

  /* 8. 合法批准 → dispatch mode=review_action，参数完整 */
  resetRoutes();
  {
    const env = baseEnv();
    on("/answerCallbackQuery", () => res(200, { ok: true }));
    on("/dispatches", () => res(204, ""));
    const data = await makeCallback("abcdef123456", "a");
    const r = await postWebhook(env, telegramUpdate({ data, fromId: "111" }));
    eq("A3-8 合法批准被接受", r.body.accepted, true);
    const inputs = dispatchedInputs();
    eq("A3-8 只 dispatch 一次", inputs.length, 1);
    eq("A3-8 dispatch 的 mode", inputs[0] && inputs[0].mode, "review_action");
    eq("A3-8 dispatch 的事件 ID", inputs[0] && inputs[0].review_event_id, "abcdef123456");
    eq("A3-8 dispatch 的动作", inputs[0] && inputs[0].review_action, "approve");
    eq("A3-8 dispatch 的操作人", inputs[0] && inputs[0].review_operator_id, "111");
    check(
      "A3-8 已立即回应 callback query",
      calls.some((c) => c.url.includes("/answerCallbackQuery")),
    );
  }

  /* 9. 合法拒绝 → review_action=decline */
  resetRoutes();
  {
    const env = baseEnv();
    on("/answerCallbackQuery", () => res(200, { ok: true }));
    on("/dispatches", () => res(204, ""));
    const data = await makeCallback("abcdef123456", "d");
    await postWebhook(env, telegramUpdate({ data }));
    const inputs = dispatchedInputs();
    eq("A3-9 拒绝映射为 decline", inputs[0] && inputs[0].review_action, "decline");
  }

  /* 10. 30 秒内双击 → 只 dispatch 一次 */
  resetRoutes();
  {
    const env = baseEnv();
    on("/answerCallbackQuery", () => res(200, { ok: true }));
    on("/dispatches", () => res(204, ""));
    const data = await makeCallback("abcdef123456", "a");
    await postWebhook(env, telegramUpdate({ data, cqId: "cq1" }));
    advance(0.1);
    const r2 = await postWebhook(env, telegramUpdate({ data, cqId: "cq2" }));
    eq("A3-10 双击被去重", r2.body.deduped, "double_click");
    eq("A3-10 双击只 dispatch 一次", dispatchCalls().length, 1);
  }

  /* 11. dispatch 失败 → 记错误 + 发运维告警（操作人已点按钮，流水线却没起来） */
  resetRoutes();
  {
    const env = baseEnv();
    on("/answerCallbackQuery", () => res(200, { ok: true }));
    on("/dispatches", () => res(422, { message: "workflow not found" }));
    on("/sendMessage", () => res(200, { ok: true }));
    const data = await makeCallback("abcdef123456", "a");
    await postWebhook(env, telegramUpdate({ data }));
    const ops = calls.filter((c) => c.url.includes("/sendMessage"));
    eq("A3-11 dispatch 失败时发告警", ops.length, 1);
    check(
      "A3-11 告警发到私有运维群",
      ops[0] && String(ops[0].body).includes("-100777"),
      ops[0] && ops[0].body,
    );
    const err = await env.OVERSIGHT_KV.get("oversight:last_review_dispatch_error");
    check("A3-11 dispatch 失败被记录", !!err && String(err).includes("422"), err);
  }

  /* 12. 审核按钮绝不触发 scan */
  resetRoutes();
  {
    const env = baseEnv();
    on("/answerCallbackQuery", () => res(200, { ok: true }));
    on("/dispatches", () => res(204, ""));
    const data = await makeCallback("abcdef123456", "a");
    await postWebhook(env, telegramUpdate({ data }));
    const modes = dispatchedInputs().map((i) => i.mode);
    check(
      "A3-12 审核回调绝不 dispatch scan",
      modes.every((m) => m === "review_action"),
      modes.join(","),
    );
  }
}

/* ==========================================================================
 * B3/B4/B5：看门狗调度与告警
 * ==========================================================================*/
async function testWatchdog() {
  /* 13. 心跳新鲜 → healthy，绝不 dispatch */
  resetRoutes();
  {
    const env = baseEnv();
    at("2026-11-06T02:00:00Z");
    on("/contents/state.json", stateHandler(heartbeatAgo(1)));
    on("/runs", runsHandler([runAgo(1, { status: "completed" })]));
    const out = await tick(env);
    eq("B-1 健康心跳判定", out.action, "healthy");
    eq("B-1 健康时不 dispatch", dispatchCalls().length, 0);
  }

  /* 14. 正常 5 分钟调度不被误判（FAILOVER=12） */
  resetRoutes();
  {
    const env = baseEnv();
    at("2026-11-06T02:00:00Z");
    on("/contents/state.json", stateHandler(heartbeatAgo(6)));
    on("/runs", runsHandler([runAgo(6, { status: "completed" })]));
    const out = await tick(env);
    eq("B-2 6 分钟心跳不算故障", out.action, "healthy");
    eq("B-2 6 分钟心跳不 dispatch", dispatchCalls().length, 0);
  }

  /* 15. 心跳陈旧 + 已有 run 在跑 → 不 dispatch */
  resetRoutes();
  {
    const env = baseEnv();
    at("2026-11-06T02:00:00Z");
    on("/contents/state.json", stateHandler(heartbeatAgo(20)));
    on("/runs", runsHandler([runAgo(2, { status: "in_progress" })]));
    const out = await tick(env);
    eq("B-3 已有 run 在跑时跳过", out.action, "skip_run_in_flight");
    eq("B-3 已有 run 在跑时不 dispatch", dispatchCalls().length, 0);
  }

  /* 16. 心跳陈旧 + 冷却窗口内刚 dispatch 过 → 不 dispatch（防风暴核心） */
  resetRoutes();
  {
    const env = baseEnv();
    at("2026-11-06T02:00:00Z");
    on("/contents/state.json", stateHandler(heartbeatAgo(20)));
    on("/runs", runsHandler([runAgo(3, { status: "completed" })]));
    const out = await tick(env);
    eq("B-4 冷却窗口内不重复 dispatch", out.action, "skip_cooldown");
    eq("B-4 冷却窗口内 dispatch 次数为 0", dispatchCalls().length, 0);
  }

  /* 17. 心跳陈旧 + 无活跃 run + 无冷却 → 接管一次 */
  resetRoutes();
  {
    const env = baseEnv();
    at("2026-11-06T02:00:00Z");
    on("/contents/state.json", stateHandler(heartbeatAgo(20)));
    on("/runs", runsHandler([runAgo(40, { status: "completed" })]));
    on("/dispatches", () => res(204, ""));
    const out = await tick(env, { force: true });
    eq("B-5 该接管时接管", out.action, "dispatched");
    eq("B-5 只 dispatch 一次", dispatchCalls().length, 1);
    eq("B-5 接管的是 scan", dispatchedInputs()[0].mode, "scan");
  }

  /* 18. ★ 调度风暴回归：心跳一直陈旧，连续 10 分钟只会 dispatch 一次 */
  resetRoutes();
  {
    const env = baseEnv();
    at("2026-11-06T02:00:00Z");
    const runs = [];
    on("/contents/state.json", stateHandler(heartbeatAgo(20)));
    on("/runs", () => res(200, { workflow_runs: runs.slice() }));
    on("/dispatches", () => {
      // dispatch 之后 GitHub 那边会多一条 queued 的 run —— 模拟真实行为
      runs.push({
        id: 9001,
        status: "queued",
        created_at: new Date(NOW).toISOString(),
      });
      return res(204, "");
    });
    let dispatches = 0;
    for (let i = 0; i < 10; i++) {
      const before = dispatchCalls().length;
      await tick(env, { force: true });
      if (dispatchCalls().length > before) dispatches++;
      advance(1);
      // 2 分钟后 GitHub 那边把这条 run 标成 completed，但心跳仍没动（扫描失败）
      if (runs.length && i === 1) runs[0].status = "completed";
    }
    eq("B-6 连续 10 分钟只 dispatch 一次", dispatches, 1);
    eq("B-6 dispatch 总次数", dispatchCalls().length, 1);
  }

  /* 19. 读 state 失败 → 不盲目 dispatch */
  resetRoutes();
  {
    const env = baseEnv();
    at("2026-11-06T02:00:00Z");
    on("/contents/state.json", () => res(500, { message: "boom" }));
    const out = await tick(env, { force: true });
    eq("B-7 读失败时不 dispatch", dispatchCalls().length, 0);
    eq("B-7 读失败动作", out.action, "read_failed");
    check("B-7 读失败被记为故障", out.faults.includes("state_read_failed"), out.faults.join(","));
  }

  /* 20. 读失败连续 20 分钟只告警一次（旧实现每分钟一条） */
  resetRoutes();
  {
    const env = baseEnv();
    at("2026-11-06T02:00:00Z");
    on("/contents/state.json", () => res(500, { message: "boom" }));
    on("/sendMessage", () => res(200, { ok: true }));
    let alerts = 0;
    for (let i = 0; i < 20; i++) {
      const before = calls.filter((c) => c.url.includes("/sendMessage")).length;
      await tick(env, { force: true });
      if (calls.filter((c) => c.url.includes("/sendMessage")).length > before) alerts++;
      advance(1);
    }
    eq("B-8 读失败 20 分钟只告警一次", alerts, 1);
  }

  /* 21. 心跳过期告警同样只发一次 */
  resetRoutes();
  {
    const env = baseEnv();
    at("2026-11-06T02:00:00Z");
    on("/contents/state.json", stateHandler(heartbeatAgo(60)));
    on("/runs", runsHandler([runAgo(5, { status: "in_progress" })]));
    on("/sendMessage", () => res(200, { ok: true }));
    let alerts = 0;
    for (let i = 0; i < 15; i++) {
      const before = calls.filter((c) => c.url.includes("/sendMessage")).length;
      await tick(env, { force: true });
      if (calls.filter((c) => c.url.includes("/sendMessage")).length > before) alerts++;
      advance(1);
    }
    eq("B-9 心跳过期只告警一次", alerts, 1);
  }

  /* 22. 恢复后清除故障并播报一次恢复 */
  resetRoutes();
  {
    const env = baseEnv();
    at("2026-11-06T02:00:00Z");
    on("/sendMessage", () => res(200, { ok: true }));
    on("/runs", runsHandler([runAgo(5, { status: "in_progress" })]));
    // 先进入故障并告警
    let state = heartbeatAgo(60);
    on("/contents/state.json", () => res(200, state));
    await tick(env, { force: true });
    const afterFault = calls.filter((c) => c.url.includes("/sendMessage")).length;
    eq("B-10 故障期发过告警", afterFault, 1);
    check(
      "B-10 故障记录已持久化",
      !!(await env.OVERSIGHT_KV.get("oversight:fault:heartbeat_stale")),
    );
    // 心跳恢复
    state = heartbeatAgo(1);
    advance(2);
    const out = await tick(env, { force: true });
    eq("B-10 恢复后判定健康", out.action, "healthy");
    check("B-10 恢复后故障被清除", out.recovered.length >= 1, JSON.stringify(out.recovered));
    eq(
      "B-10 恢复后故障记录从 KV 删除",
      await env.OVERSIGHT_KV.get("oversight:fault:heartbeat_stale"),
      null,
    );
    const afterRecover = calls.filter((c) => c.url.includes("/sendMessage")).length;
    eq("B-10 恢复时播报一条", afterRecover, 2);
  }

  /* 23. dispatch 失败 → 记账 + 告警一次 */
  resetRoutes();
  {
    const env = baseEnv();
    at("2026-11-06T02:00:00Z");
    on("/contents/state.json", stateHandler(heartbeatAgo(20)));
    on("/runs", runsHandler([runAgo(40, { status: "completed" })]));
    on("/dispatches", () => res(401, { message: "Bad credentials" }));
    on("/sendMessage", () => res(200, { ok: true }));
    const out = await tick(env, { force: true });
    eq("B-11 dispatch 失败动作", out.action, "dispatch_failed");
    check("B-11 dispatch 失败被记为故障", out.faults.includes("dispatch_failed"), out.faults.join(","));
    const alerts = calls.filter((c) => c.url.includes("/sendMessage"));
    eq("B-11 dispatch 失败告警一次", alerts.length, 1);
    check(
      "B-11 告警内容含错误原因",
      alerts[0] && String(alerts[0].body).includes("401"),
      alerts[0] && alerts[0].body,
    );
  }

  /* 24. dispatch 成功但扫描未恢复 → dispatch_no_recovery */
  resetRoutes();
  {
    const env = baseEnv();
    at("2026-11-06T02:00:00Z");
    on("/contents/state.json", stateHandler(heartbeatAgo(40)));
    on("/runs", runsHandler([runAgo(4, { status: "completed" })]));
    on("/sendMessage", () => res(200, { ok: true }));
    const out = await tick(env, { force: true });
    eq("B-12 补触发后未恢复动作", out.action, "skip_cooldown");
    check(
      "B-12 识别出「dispatch 成功但未恢复」",
      out.faults.includes("dispatch_no_recovery"),
      out.faults.join(","),
    );
  }

  /* 25. 早报超补偿窗口 → 告警一次，且不影响扫描判定 */
  resetRoutes();
  {
    const env = baseEnv();
    // 北京 09:30 = UTC 01:30，超过 06:00+180 分钟
    at("2026-11-06T01:30:00Z");
    on("/contents/state.json", stateHandler(heartbeatAgo(1, { last_morning_report_date: "2026-11-05" })));
    on("/runs", runsHandler([]));
    on("/sendMessage", () => res(200, { ok: true }));
    const out = await tick(env, { force: true });
    check("B-13 早报未送达被记为故障", out.faults.includes("morning_missed"), out.faults.join(","));
    const alerts = calls.filter((c) => c.url.includes("/sendMessage"));
    eq("B-13 早报告警一次", alerts.length, 1);
    check(
      "B-13 告警文案含日期",
      alerts[0] && String(alerts[0].body).includes("2026-11-06"),
      alerts[0] && alerts[0].body,
    );
  }

  /* 26. 补偿窗口内不告警（06:00~09:00 交给 ensure_morning_running.py） */
  resetRoutes();
  {
    const env = baseEnv();
    at("2026-11-06T01:00:00Z"); // 北京 09:00
    on("/contents/state.json", stateHandler(heartbeatAgo(1, { last_morning_report_date: "2026-11-05" })));
    on("/runs", runsHandler([]));
    on("/sendMessage", () => res(200, { ok: true }));
    const out = await tick(env, { force: true });
    check("B-14 窗口内不报早报故障", !out.faults.includes("morning_missed"), out.faults.join(","));
  }

  /* 27. 运维告警绝不发到公共情报频道 */
  resetRoutes();
  {
    const env = baseEnv({ TELEGRAM_OPS_CHAT_ID: "" });
    at("2026-11-06T02:00:00Z");
    on("/contents/state.json", stateHandler(heartbeatAgo(60)));
    on("/runs", runsHandler([runAgo(5, { status: "in_progress" })]));
    on("/sendMessage", () => res(200, { ok: true }));
    await tick(env, { force: true });
    const alerts = calls.filter((c) => c.url.includes("/sendMessage"));
    eq("B-15 无运维群时不发告警（不回落到公共频道）", alerts.length, 0);
  }

  /* 28. KV 缺失 → 诚实标记 durable=false，但依然不重复 dispatch */
  resetRoutes();
  {
    const env = baseEnv({ OVERSIGHT_KV: undefined });
    at("2026-11-06T02:00:00Z");
    on("/contents/state.json", stateHandler(heartbeatAgo(20)));
    const runs = [];
    on("/runs", () => res(200, { workflow_runs: runs.slice() }));
    on("/dispatches", () => {
      runs.push({ id: 9100, status: "queued", created_at: new Date(NOW).toISOString() });
      return res(204, "");
    });
    const out = await tick(env, { force: true });
    eq("B-16 无 KV 时 durable=false", out.durable, false);
    let n = 1;
    for (let i = 0; i < 5; i++) {
      advance(1);
      const before = dispatchCalls().length;
      await tick(env, { force: true });
      if (dispatchCalls().length > before) n++;
    }
    eq("B-16 无 KV 也不重复 dispatch（靠 GitHub run 列表）", n, 1);
  }

  /* 29. 只读诊断（默认 GET）绝不产生副作用 */
  resetRoutes();
  {
    const env = baseEnv();
    at("2026-11-06T02:00:00Z");
    on("/contents/state.json", stateHandler(heartbeatAgo(60)));
    on("/runs", runsHandler([runAgo(40, { status: "completed" })]));
    on("/sendMessage", () => res(200, { ok: true }));
    const out = await tick(env);
    eq("B-17 默认 GET 是 dry run", out.dry_run, true);
    eq("B-17 dry run 不 dispatch", dispatchCalls().length, 0);
    eq("B-17 dry run 不告警", calls.filter((c) => c.url.includes("/sendMessage")).length, 0);
    eq("B-17 dry run 报告将接管", out.action, "would_dispatch");
  }

  /* 30. 阈值全部可配置，且默认值来自实测 */
  resetRoutes();
  {
    const env = baseEnv();
    at("2026-11-06T02:00:00Z");
    on("/contents/state.json", stateHandler(heartbeatAgo(1)));
    on("/runs", runsHandler([]));
    const out = await tick(env);
    eq("B-18 默认 FAILOVER_MINUTES", out.thresholds.failover_minutes, 12);
    eq("B-18 默认 DISPATCH_COOLDOWN_MINUTES", out.thresholds.dispatch_cooldown_minutes, 10);
    eq("B-18 默认 ALERT_MINUTES", out.thresholds.alert_minutes, 30);
    eq("B-18 期望扫描间隔", out.thresholds.expected_scan_interval_minutes, 5);
    eq("B-18 早报补偿窗口与 ensure_morning 一致", out.thresholds.morning_grace_minutes, 180);
  }
}

/* ========================= 主流程 ========================= */
try {
  await testWebhook();
  await testWatchdog();
} catch (err) {
  check("harness", false, "测试脚本自身异常：" + (err && err.stack ? err.stack : err));
}

Date.now = REAL_NOW;

const failed = results.filter((r) => !r.ok);
const asJson = process.argv.includes("--json");
if (asJson) {
  console.log(JSON.stringify({ total: results.length, failed: failed.length, results }, null, 2));
} else {
  for (const r of results) {
    console.log((r.ok ? "  PASS " : "  FAIL ") + r.name + (r.detail ? "  ← " + r.detail : ""));
  }
  console.log(`\n${results.length - failed.length}/${results.length} passed`);
}
process.exit(failed.length === 0 ? 0 : 1);
