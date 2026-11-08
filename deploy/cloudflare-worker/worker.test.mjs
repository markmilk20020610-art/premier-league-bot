/**
 * worker.js + review_vault.js 的行为测试（Node 原生，不依赖 wrangler / miniflare）
 *
 * 为什么必须真的跑：worker.js 是生产链路上唯一不在 Python 测试覆盖范围内的组件，
 * 而它恰好是「调度风暴」和「审核回调被伪造 / 重放」这两个最危险缺陷的所在地。
 * 只用正则扫源码证明不了「连续 10 分钟只 dispatch 一次」，必须真的跑。
 *
 * 做法：
 *   · 读 worker.js / review_vault.js 源码，把 import 内联后用 data: URL 动态 import
 *     （避免给仓库加 package.json）
 *   · Durable Object 用 node:sqlite 真实模拟，跑的是**同一份 SQL**
 *   · 覆盖 globalThis.fetch，把 GitHub / Telegram 全部拦在本地
 *   · 覆盖 Date.now，让冷却窗口、过期按钮可以确定性复现
 *
 * 用法：node worker.test.mjs          （退出码 0 = 全绿）
 *       node worker.test.mjs --json
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { DatabaseSync } from "node:sqlite";

const HERE = fileURLToPath(new URL(".", import.meta.url));

/** 把 worker.js 里的相对 import 内联，使其能用 data: URL 单独加载 */
function loadModule(mainFile) {
  let src = fs.readFileSync(HERE + mainFile, "utf8");
  const vaultSrc = fs.readFileSync(HERE + "review_vault.js", "utf8");
  src = src.replace('import { Vault } from "./review_vault.js";', vaultSrc);
  src = src.replace('export { Vault };', "");
  return src;
}

const MOD = await import(
  "data:text/javascript;base64," + Buffer.from(loadModule("worker.js"), "utf8").toString("base64")
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
let routes = [];
let calls = [];
function on(substr, handler) {
  routes.push({ match: substr, handler });
}
globalThis.fetch = async (url, init = {}) => {
  const u = String(url);
  const method = (init.method || "GET").toUpperCase();
  calls.push({ url: u, method, body: init.body || null });
  for (const r of routes) if (u.includes(r.match)) return r.handler(u, init);
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
function alertCalls() {
  return calls.filter((c) => c.url.includes("/sendMessage"));
}

/* ========================= Durable Object 模拟 ========================= */
/** 把 node:sqlite 包装成 DO 的 `ctx.storage.sql` 接口 */
function makeSql(db) {
  return {
    exec(query, ...params) {
      const stmt = db.prepare(query);
      if (/^\s*SELECT/i.test(query)) {
        const rows = stmt.all(...params);
        return { toArray: () => rows, one: () => (rows.length ? rows[0] : null),
                 rowsRead: rows.length, rowsWritten: 0 };
      }
      const info = stmt.run(...params);
      return { toArray: () => [], one: () => null, rowsRead: 0, rowsWritten: info.changes };
    },
  };
}

/**
 * 造一个 Vault 实例。
 * `dbPath` 传同一个文件两次 = 模拟「Worker 重启后接上同一份持久化存储」。
 */
function makeVault(env, dbPath) {
  const db = new DatabaseSync(dbPath || ":memory:");
  const state = {
    storage: { sql: makeSql(db) },
    blockConcurrencyWhile(fn) {
      return fn();
    },
  };
  const instance = new MOD.Vault(state, env);
  instance._db = db;
  return instance;
}

/** 包装成 env.VAULT 命名空间，让 worker.js 像调真实 DO 一样调用 */
function makeNamespace(instance) {
  return {
    idFromName: (n) => ({ name: n }),
    get: () => ({
      fetch: (url, init) => instance.fetch(new Request(url, init)),
    }),
  };
}

/* ========================= env 工厂 ========================= */
const REPO = "markmilk20020610-art/premier-league-bot";
const SERVICE_KEY = "svc_fake_key";

function baseEnv(over = {}) {
  const env = {
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
    INTERNAL_SERVICE_KEY: SERVICE_KEY,
    FAILOVER_MINUTES: "12",
    DISPATCH_COOLDOWN_MINUTES: "10",
    ALERT_MINUTES: "30",
    READ_FAIL_ALERT_MINUTES: "8",
    MORNING_GRACE_MINUTES: "180",
    ...over,
  };
  if (!("VAULT" in over)) env.VAULT = makeNamespace(makeVault(env));
  return env;
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

/* ========================= HMAC / callback ========================= */
async function sig(secret, ver, action, ref, exp) {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"],
  );
  const buf = await crypto.subtle.sign("HMAC", key, enc.encode(`${ver}|${action}|${ref}|${exp}`));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 16);
}
async function makeCallback(ref, action, { ttlSec = 3600, secret = "hmac_secret_fake" } = {}) {
  const exp = Math.floor(NOW / 1000) + ttlSec;
  const s = await sig(secret, "v1", action, ref, exp);
  return `v1:${action}:${ref}:${exp}:${s}`;
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
  const c = ctx();
  const r = await worker.fetch(new Request("https://w.example/" + q, { method: "GET" }), env, c);
  const body = await r.json();
  await c.drain();
  return body;
}
/** 直接调用内部接口（模拟 GitHub Actions 带服务密钥） */
async function internal(env, path, { method = "POST", body, key } = {}) {
  const c = ctx();
  const r = await worker.fetch(new Request("https://w.example" + path, {
    method,
    headers: {
      "content-type": "application/json",
      "X-Service-Key": key === undefined ? SERVICE_KEY : key,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  }), env, c);
  const out = await r.json();
  await c.drain();
  return { status: r.status, body: out };
}

/** 直接在 DO 里造一条待审记录，返回 review_ref */
async function seedReview(env, eventId = "abcdef123456", extra = {}) {
  const out = await internal(env, "/internal/review/enqueue", {
    body: { payload: {
      event_id: eventId,
      title: "某队主帅即将下课（未证实）",
      fact_summary: "据某小报报道，某队主帅将在 48 小时内被解雇。",
      articles: [{ title: "Boss to be sacked", url: "https://example.com/secret-article" }],
      urls: ["https://example.com/secret-article"],
      publishers: ["example.com"],
      importance: "S",
      cred: "E4",
      ...extra,
    } },
  });
  return out.body.review_ref;
}

/**
 * 直接打 DO（跳过 Worker 的 /internal 前缀与鉴权链）。
 * P1 的租约过期场景需要直接操作存储，走 Worker 那层够不着。
 */
function vaultCall(v, path, body, method = "POST") {
  return v.fetch(new Request("https://v" + path, {
    method,
    headers: { "content-type": "application/json", "X-Service-Key": SERVICE_KEY },
    body: body === undefined ? undefined : JSON.stringify(body),
  })).then((r) => r.json());
}

/** 直接往指定 DO 实例里入队一条审核记录，返回 review_ref */
async function seedDirect(v, eventId) {
  const out = await vaultCall(v, "/review/enqueue", { payload: {
    event_id: eventId,
    title: "某队主帅即将下课（未证实）",
    fact_summary: "据某小报报道，某队主帅将在 48 小时内被解雇。",
    articles: [{ title: "Boss to be sacked", url: "https://example.com/secret-article" }],
    urls: ["https://example.com/secret-article"],
    publishers: ["example.com"],
    importance: "S",
    cred: "E4",
  } });
  return out.review_ref;
}

/**
 * 把租约推到过去，模拟「进程在 Telegram 发送成功后、finish 回写前被杀，
 * 时间过了 TTL」。review_vault.js 用真实 Date.now()，所以这里也用真实时间
 * 往前推，而不是用测试里的假时钟 NOW。
 */
function expireLease(v, ref) {
  const past = new Date(Date.now() - 60 * 1000).toISOString();
  v._db.prepare("UPDATE review SET claim_expires_at = ? WHERE review_ref = ?")
    .run(past, ref);
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
    const ref = await seedReview(env);
    const data = await makeCallback(ref, "a");
    const r = await postWebhook(env, telegramUpdate({ data }), {
      "X-Telegram-Bot-Api-Secret-Token": "wrong",
    });
    eq("A3-2 错误 secret token 拒绝", r.status, 401);
    eq("A3-2 错误 secret token 不 dispatch", dispatchCalls().length, 0);
  }

  /* 3. 非 callback_query → 200 忽略 */
  resetRoutes();
  {
    const env = baseEnv();
    const r = await postWebhook(env, { update_id: 2, message: { text: "hi" } });
    eq("A3-3 非回调消息返回 200", r.status, 200);
    eq("A3-3 非回调消息被忽略", r.body.ignored, "not_callback_query");
    eq("A3-3 非回调消息不 dispatch", dispatchCalls().length, 0);
  }

  /* 4. 操作人不在白名单 → 拒绝 */
  resetRoutes();
  {
    const env = baseEnv();
    on("/answerCallbackQuery", () => res(200, { ok: true }));
    const ref = await seedReview(env);
    const data = await makeCallback(ref, "a");
    const r = await postWebhook(env, telegramUpdate({ data, fromId: "999" }));
    eq("A3-4 非白名单操作人被拒", r.body.rejected, "operator_not_allowed");
    eq("A3-4 非白名单操作人不 dispatch", dispatchCalls().length, 0);
  }

  /* 5. 回调不是来自审核群 → 拒绝 */
  resetRoutes();
  {
    const env = baseEnv();
    on("/answerCallbackQuery", () => res(200, { ok: true }));
    const ref = await seedReview(env);
    const data = await makeCallback(ref, "a");
    const r = await postWebhook(env, telegramUpdate({ data, chatId: "-100999" }));
    eq("A3-5 非审核群回调被拒", r.body.rejected, "not_review_chat");
    eq("A3-5 非审核群回调不 dispatch", dispatchCalls().length, 0);
  }

  /* 6. 签名错 → 拒绝 */
  resetRoutes();
  {
    const env = baseEnv();
    on("/answerCallbackQuery", () => res(200, { ok: true }));
    const ref = await seedReview(env);
    const exp = Math.floor(NOW / 1000) + 3600;
    const forged = `v1:a:${ref}:${exp}:${"0".repeat(16)}`;
    const r = await postWebhook(env, telegramUpdate({ data: forged }));
    eq("A3-6 伪造签名被拒", r.body.rejected, "bad_callback");
    eq("A3-6 伪造签名不 dispatch", dispatchCalls().length, 0);
  }

  /* 7. 过期按钮 → 拒绝 */
  resetRoutes();
  {
    const env = baseEnv();
    on("/answerCallbackQuery", () => res(200, { ok: true }));
    const ref = await seedReview(env);
    const data = await makeCallback(ref, "a", { ttlSec: -60 });
    const r = await postWebhook(env, telegramUpdate({ data }));
    eq("A3-7 过期按钮被拒", r.body.rejected, "bad_callback");
    eq("A3-7 过期按钮不 dispatch", dispatchCalls().length, 0);
  }

  /* 8. ★ 合法批准 → dispatch inputs 只有 mode + review_id */
  resetRoutes();
  {
    const env = baseEnv();
    on("/answerCallbackQuery", () => res(200, { ok: true }));
    on("/dispatches", () => res(204, ""));
    const ref = await seedReview(env);
    const data = await makeCallback(ref, "a");
    const r = await postWebhook(env, telegramUpdate({ data, fromId: "111" }));
    eq("A3-8 合法批准被接受", r.body.accepted, true);
    const inputs = dispatchedInputs();
    eq("A3-8 只 dispatch 一次", inputs.length, 1);
    eq("A3-8 dispatch 的 mode", inputs[0] && inputs[0].mode, "review_action");
    eq("A3-8 dispatch 的 review_id", inputs[0] && inputs[0].review_id, ref);
    const keys = Object.keys(inputs[0] || {}).sort();
    eq("A3-8 inputs 只允许两个字段", keys.join(","), "mode,review_id");
    const raw = JSON.stringify(inputs[0] || {});
    check("A3-8 inputs 不含正文", !raw.includes("某队主帅"), raw);
    check("A3-8 inputs 不含原始链接", !raw.includes("secret-article"), raw);
    check("A3-8 inputs 不含操作人", !raw.includes("111"), raw);
    check("A3-8 inputs 不含 HMAC", !raw.includes("hmac_secret"), raw);
    check("A3-8 已立即回应 callback query",
      calls.some((c) => c.url.includes("/answerCallbackQuery")));
  }

  /* 9. 合法拒绝 → 决定记为 decline */
  resetRoutes();
  {
    const env = baseEnv();
    on("/answerCallbackQuery", () => res(200, { ok: true }));
    on("/dispatches", () => res(204, ""));
    const ref = await seedReview(env);
    const data = await makeCallback(ref, "d");
    const r = await postWebhook(env, telegramUpdate({ data }));
    eq("A3-9 拒绝被接受", r.body.action, "decline");
    const st = await internal(env, "/internal/review/" + ref + "/state", { method: "GET" });
    eq("A3-9 私有存储记录的决定", st.body.action, "decline");
  }

  /* 10. ★ 重复点击同一按钮 → 只 dispatch 一次（DO 原子迁移挡住） */
  resetRoutes();
  {
    const env = baseEnv();
    on("/answerCallbackQuery", () => res(200, { ok: true }));
    on("/dispatches", () => res(204, ""));
    const ref = await seedReview(env);
    const data = await makeCallback(ref, "a");
    await postWebhook(env, telegramUpdate({ data, cqId: "cq1" }));
    advance(0.1);
    const r2 = await postWebhook(env, telegramUpdate({ data, cqId: "cq2" }));
    eq("A3-10 重复点击被拒", r2.body.rejected, "already_handled");
    eq("A3-10 重复点击只 dispatch 一次", dispatchCalls().length, 1);
  }

  /* 11. ★ 旧按钮重放（同一签名，过期前）→ 无法再次批准 */
  resetRoutes();
  {
    const env = baseEnv();
    on("/answerCallbackQuery", () => res(200, { ok: true }));
    on("/dispatches", () => res(204, ""));
    const ref = await seedReview(env);
    const data = await makeCallback(ref, "a", { ttlSec: 7200 });
    await postWebhook(env, telegramUpdate({ data, cqId: "cq1" }));
    advance(30);
    const r2 = await postWebhook(env, telegramUpdate({ data, cqId: "cq2" }));
    eq("A3-11 旧按钮重放被拒", r2.body.rejected, "already_handled");
    eq("A3-11 重放未产生第二次 dispatch", dispatchCalls().length, 1);
  }

  /* 12. 审核回调绝不 dispatch scan */
  resetRoutes();
  {
    const env = baseEnv();
    on("/answerCallbackQuery", () => res(200, { ok: true }));
    on("/dispatches", () => res(204, ""));
    const ref = await seedReview(env);
    const data = await makeCallback(ref, "a");
    await postWebhook(env, telegramUpdate({ data }));
    const modes = dispatchedInputs().map((i) => i.mode);
    check("A3-12 审核回调绝不 dispatch scan", modes.every((m) => m === "review_action"),
      modes.join(","));
  }

  /* 13. dispatch 失败 → 发私有运维群告警，不发公共频道 */
  resetRoutes();
  {
    const env = baseEnv();
    on("/answerCallbackQuery", () => res(200, { ok: true }));
    on("/dispatches", () => res(422, { message: "workflow not found" }));
    on("/sendMessage", () => res(200, { ok: true }));
    const ref = await seedReview(env);
    const data = await makeCallback(ref, "a");
    await postWebhook(env, telegramUpdate({ data }));
    const ops = alertCalls();
    eq("A3-13 dispatch 失败时发告警", ops.length, 1);
    check("A3-13 告警发到私有运维群",
      ops[0] && String(ops[0].body).includes("-100777"), ops[0] && ops[0].body);
    check("A3-13 告警不含新闻正文",
      ops[0] && !String(ops[0].body).includes("某队主帅"), ops[0] && ops[0].body);
  }
}

/* ==========================================================================
 * B：看门狗调度与告警
 * ==========================================================================*/
async function testWatchdog() {
  /* 14. 心跳新鲜 → healthy */
  resetRoutes();
  {
    const env = baseEnv();
    at("2026-11-06T02:00:00Z");
    on("/contents/state.json", stateHandler(heartbeatAgo(1)));
    on("/runs", runsHandler([runAgo(1, { status: "completed" })]));
    const out = await tick(env);
    eq("B-1 健康心跳判定", out.action, "healthy");
    eq("B-1 健康时不 dispatch", dispatchCalls().length, 0);
    eq("B-1 存储后端", out.store, "durable_object_sqlite");
  }

  /* 15. 正常 5 分钟调度不被误判 */
  resetRoutes();
  {
    const env = baseEnv();
    at("2026-11-06T02:00:00Z");
    on("/contents/state.json", stateHandler(heartbeatAgo(6)));
    on("/runs", runsHandler([runAgo(6, { status: "completed" })]));
    const out = await tick(env);
    eq("B-2 6 分钟心跳不算故障", out.action, "healthy");
  }

  /* 16. 已有 run 在跑 → 不 dispatch */
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

  /* 17. 该接管时接管一次 */
  resetRoutes();
  {
    const env = baseEnv();
    at("2026-11-06T02:00:00Z");
    on("/contents/state.json", stateHandler(heartbeatAgo(20)));
    on("/runs", runsHandler([runAgo(40, { status: "completed" })]));
    on("/dispatches", () => res(204, ""));
    const out = await tick(env, { force: true });
    eq("B-4 该接管时接管", out.action, "dispatched");
    eq("B-4 只 dispatch 一次", dispatchCalls().length, 1);
    eq("B-4 接管的是 scan", dispatchedInputs()[0].mode, "scan");
  }

  /* 18. ★ 调度风暴回归：连续 10 分钟只 dispatch 一次（DO 冷却锁） */
  resetRoutes();
  {
    const env = baseEnv();
    at("2026-11-06T02:00:00Z");
    const runs = [];
    on("/contents/state.json", stateHandler(heartbeatAgo(20)));
    on("/runs", () => res(200, { workflow_runs: runs.slice() }));
    on("/dispatches", () => {
      runs.push({ id: 9001, status: "completed", created_at: new Date(NOW).toISOString() });
      return res(204, "");
    });
    for (let i = 0; i < 10; i++) {
      await tick(env, { force: true });
      advance(1);
    }
    eq("B-5 连续 10 分钟只 dispatch 一次", dispatchCalls().length, 1);
  }

  /* 19. ★ DO 未绑定 → 拒绝接管（不假装有锁） */
  resetRoutes();
  {
    const env = baseEnv({ VAULT: undefined });
    at("2026-11-06T02:00:00Z");
    on("/contents/state.json", stateHandler(heartbeatAgo(40)));
    on("/runs", runsHandler([runAgo(60, { status: "completed" })]));
    on("/dispatches", () => res(204, ""));
    const out = await tick(env, { force: true });
    eq("B-6 无 DO 时 durable=false", out.durable, false);
    eq("B-6 无 DO 时拒绝接管", out.action, "skip_no_durable_lock");
    eq("B-6 无 DO 时绝不 dispatch", dispatchCalls().length, 0);
  }

  /* 20. 读 state 失败 → 不盲目 dispatch */
  resetRoutes();
  {
    const env = baseEnv();
    at("2026-11-06T02:00:00Z");
    on("/contents/state.json", () => res(500, { message: "boom" }));
    const out = await tick(env, { force: true });
    eq("B-7 读失败时不 dispatch", dispatchCalls().length, 0);
    eq("B-7 读失败动作", out.action, "read_failed");
    check("B-7 读失败被记为故障", out.faults.includes("state_read_failed"),
      out.faults.join(","));
  }

  /* 21. ★ 读失败连续 20 分钟只告警一次 */
  resetRoutes();
  {
    const env = baseEnv();
    at("2026-11-06T02:00:00Z");
    on("/contents/state.json", () => res(500, { message: "boom" }));
    on("/sendMessage", () => res(200, { ok: true }));
    for (let i = 0; i < 20; i++) {
      await tick(env, { force: true });
      advance(1);
    }
    eq("B-8 读失败 20 分钟只告警一次", alertCalls().length, 1);
  }

  /* 22. ★ 心跳过期告警同样只发一次 */
  resetRoutes();
  {
    const env = baseEnv();
    at("2026-11-06T02:00:00Z");
    on("/contents/state.json", stateHandler(heartbeatAgo(60)));
    on("/runs", runsHandler([runAgo(5, { status: "in_progress" })]));
    on("/sendMessage", () => res(200, { ok: true }));
    for (let i = 0; i < 15; i++) {
      await tick(env, { force: true });
      advance(1);
    }
    eq("B-9 心跳过期只告警一次", alertCalls().length, 1);
  }

  /* 23. ★ 恢复后清故障 + 播报一次 */
  resetRoutes();
  {
    const env = baseEnv();
    at("2026-11-06T02:00:00Z");
    on("/sendMessage", () => res(200, { ok: true }));
    on("/runs", runsHandler([runAgo(5, { status: "in_progress" })]));
    let state = heartbeatAgo(60);
    on("/contents/state.json", () => res(200, state));
    await tick(env, { force: true });
    eq("B-10 故障期发过告警", alertCalls().length, 1);
    state = heartbeatAgo(1);
    advance(2);
    const out = await tick(env, { force: true });
    eq("B-10 恢复后判定健康", out.action, "healthy");
    check("B-10 恢复后故障被清除", out.recovered.length >= 1, JSON.stringify(out.recovered));
    eq("B-10 恢复时播报一条", alertCalls().length, 2);
  }

  /* 24. dispatch 失败 → 记账 + 告警一次 */
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
    check("B-11 dispatch 失败被记为故障", out.faults.includes("dispatch_failed"),
      out.faults.join(","));
    eq("B-11 dispatch 失败告警一次", alertCalls().length, 1);
  }

  /* 25. 早报超补偿窗口 → 告警一次 */
  resetRoutes();
  {
    const env = baseEnv();
    at("2026-11-06T01:30:00Z"); // 北京 09:30
    on("/contents/state.json",
      stateHandler(heartbeatAgo(1, { last_morning_report_date: "2026-11-05" })));
    on("/runs", runsHandler([]));
    on("/sendMessage", () => res(200, { ok: true }));
    const out = await tick(env, { force: true });
    check("B-12 早报未送达被记为故障", out.faults.includes("morning_missed"),
      out.faults.join(","));
    eq("B-12 早报告警一次", alertCalls().length, 1);
  }

  /* 26. 补偿窗口内不告警 */
  resetRoutes();
  {
    const env = baseEnv();
    at("2026-11-06T01:00:00Z"); // 北京 09:00
    on("/contents/state.json",
      stateHandler(heartbeatAgo(1, { last_morning_report_date: "2026-11-05" })));
    on("/runs", runsHandler([]));
    on("/sendMessage", () => res(200, { ok: true }));
    const out = await tick(env, { force: true });
    check("B-13 窗口内不报早报故障", !out.faults.includes("morning_missed"),
      out.faults.join(","));
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
    eq("B-14 无运维群时不发告警（不回落到公共频道）", alertCalls().length, 0);
  }

  /* 28. 只读诊断绝不产生副作用 */
  resetRoutes();
  {
    const env = baseEnv();
    at("2026-11-06T02:00:00Z");
    on("/contents/state.json", stateHandler(heartbeatAgo(60)));
    on("/runs", runsHandler([runAgo(40, { status: "completed" })]));
    on("/sendMessage", () => res(200, { ok: true }));
    const out = await tick(env);
    eq("B-15 默认 GET 是 dry run", out.dry_run, true);
    eq("B-15 dry run 不 dispatch", dispatchCalls().length, 0);
    eq("B-15 dry run 不告警", alertCalls().length, 0);
    eq("B-15 dry run 报告将接管", out.action, "would_dispatch");
  }

  /* 29. 阈值默认值来自实测 */
  resetRoutes();
  {
    const env = baseEnv();
    at("2026-11-06T02:00:00Z");
    on("/contents/state.json", stateHandler(heartbeatAgo(1)));
    on("/runs", runsHandler([]));
    const out = await tick(env);
    eq("B-16 默认 FAILOVER_MINUTES", out.thresholds.failover_minutes, 12);
    eq("B-16 默认 DISPATCH_COOLDOWN_MINUTES", out.thresholds.dispatch_cooldown_minutes, 10);
    eq("B-16 默认 ALERT_MINUTES", out.thresholds.alert_minutes, 30);
    eq("B-16 早报补偿窗口与 ensure_morning 一致", out.thresholds.morning_grace_minutes, 180);
  }
}

/* ==========================================================================
 * C：Durable Object 安全语义（并发 / 重放 / 重启持久化）
 * ==========================================================================*/
async function testVault() {
  /* 30. ★ 重复批准只有一次成功 */
  resetRoutes();
  {
    const env = baseEnv();
    const ref = await seedReview(env, "aaaa11112222");
    const a = await internal(env, `/internal/review/${ref}/decide`,
      { body: { action: "approve", operator_id: "111" } });
    const b = await internal(env, `/internal/review/${ref}/decide`,
      { body: { action: "approve", operator_id: "111" } });
    eq("C-1 第一次批准成功", a.body.ok, true);
    eq("C-1 第二次批准被拒", b.body.ok, false);
    eq("C-1 第二次标记为冲突", b.body.conflict, true);
  }

  /* 31. ★ 批准与拒绝并发只能一个生效 */
  resetRoutes();
  {
    const env = baseEnv();
    const ref = await seedReview(env, "bbbb11112222");
    const [x, y] = await Promise.all([
      internal(env, `/internal/review/${ref}/decide`,
        { body: { action: "approve", operator_id: "111" } }),
      internal(env, `/internal/review/${ref}/decide`,
        { body: { action: "decline", operator_id: "222" } }),
    ]);
    const oks = [x.body.ok, y.body.ok].filter((v) => v === true).length;
    eq("C-2 并发决定只有一个成功", oks, 1);
    const st = await internal(env, `/internal/review/${ref}/state`, { method: "GET" });
    eq("C-2 最终状态是 decided", st.body.status, "decided");
    check("C-2 最终动作是二者之一",
      ["approve", "decline"].includes(st.body.action), st.body.action);
  }

  /* 32. ★ claim 一次性：第二次 claim 失败 */
  resetRoutes();
  {
    const env = baseEnv();
    const ref = await seedReview(env, "cccc11112222");
    await internal(env, `/internal/review/${ref}/decide`,
      { body: { action: "approve", operator_id: "111" } });
    const c1 = await internal(env, `/internal/review/${ref}/claim`, { body: {} });
    const c2 = await internal(env, `/internal/review/${ref}/claim`, { body: {} });
    eq("C-3 首次 claim 成功", c1.body.ok, true);
    check("C-3 claim 返回完整 payload", !!c1.body.payload && !!c1.body.payload.title);
    eq("C-3 二次 claim 被拒", c2.body.ok, false);
    eq("C-3 二次 claim 是冲突", c2.body.conflict, true);
  }

  /* 33. ★ 只有 Telegram 成功才允许完成投递迁移 */
  resetRoutes();
  {
    const env = baseEnv();
    const ref = await seedReview(env, "dddd11112222");
    await internal(env, `/internal/review/${ref}/decide`,
      { body: { action: "approve", operator_id: "111" } });
    const c = await internal(env, `/internal/review/${ref}/claim`, { body: {} });
    // 没有 finish 之前，状态必须是 claimed，不是 delivered
    const mid = await internal(env, `/internal/review/${ref}/state`, { method: "GET" });
    eq("C-4 未回传结果时状态为 claimed", mid.body.status, "claimed");
    // 错误的 token 不能完成
    const bad = await internal(env, `/internal/review/${ref}/finish`,
      { body: { claim_token: "0".repeat(16), outcome: "delivered", message_ids: ["1"] } });
    eq("C-4 错误 token 不能完成迁移", bad.body.ok, false);
    const good = await internal(env, `/internal/review/${ref}/finish`,
      { body: { claim_token: c.body.claim_token, outcome: "delivered", message_ids: ["99"] } });
    eq("C-4 正确 token 完成迁移", good.body.ok, true);
    const fin = await internal(env, `/internal/review/${ref}/state`, { method: "GET" });
    eq("C-4 最终状态 delivered", fin.body.status, "delivered");
    eq("C-4 message_ids 已落库", (fin.body.message_ids || []).join(","), "99");
  }

  /* 33b. ★ 拒绝同样走 claim → finish：claim 之后必须能归档成 declined */
  resetRoutes();
  {
    const env = baseEnv();
    const ref = await seedReview(env, "d0d011112222");
    await internal(env, `/internal/review/${ref}/decide`,
      { body: { action: "decline", operator_id: "111" } });
    const c = await internal(env, `/internal/review/${ref}/claim`, { body: {} });
    eq("C-4b 拒绝也能被认领", c.body.ok, true);
    const fin = await internal(env, `/internal/review/${ref}/finish`,
      { body: { claim_token: c.body.claim_token, outcome: "declined", message_ids: [] } });
    eq("C-4b claim 之后拒绝可归档", fin.body.ok, true);
    const st = await internal(env, `/internal/review/${ref}/state`, { method: "GET" });
    eq("C-4b 最终状态 declined", st.body.status, "declined");
    const re = await internal(env, `/internal/review/${ref}/claim`, { body: {} });
    eq("C-4b 已拒绝后不能再 claim", re.body.ok, false);
  }

  /* 34. ★ finish 之后 claim_token 失效（重放无法再投递） */
  resetRoutes();
  {
    const env = baseEnv();
    const ref = await seedReview(env, "eeee11112222");
    await internal(env, `/internal/review/${ref}/decide`,
      { body: { action: "approve", operator_id: "111" } });
    const c = await internal(env, `/internal/review/${ref}/claim`, { body: {} });
    await internal(env, `/internal/review/${ref}/finish`,
      { body: { claim_token: c.body.claim_token, outcome: "delivered", message_ids: ["1"] } });
    const again = await internal(env, `/internal/review/${ref}/finish`,
      { body: { claim_token: c.body.claim_token, outcome: "delivered", message_ids: ["1"] } });
    eq("C-5 重放 finish 被拒", again.body.ok, false);
    const re = await internal(env, `/internal/review/${ref}/claim`, { body: {} });
    eq("C-5 已投递后不能再 claim", re.body.ok, false);
  }

  /* 35. ★ 同一 event_id 只允许一条有效记录 */
  resetRoutes();
  {
    const env = baseEnv();
    const r1 = await seedReview(env, "ffff11112222");
    const r2 = await seedReview(env, "ffff11112222");
    eq("C-6 同一事件复用同一 review_ref", r1, r2);
    const byEvent = await internal(env, "/internal/review/by-event",
      { body: { event_id: "ffff11112222" } });
    eq("C-6 按事件查询能命中", byEvent.body.review_ref, r1);
    // Python 的 `HttpReviewStore.find_active()` 直接读这两个字段，
    // 少一个就会变成「查得到 ref 但 found=false」的静默失败。
    eq("C-6 按事件查询返回 found", byEvent.body.found, true);
    eq("C-6 按事件查询返回状态", byEvent.body.status, "pending");
    const miss = await internal(env, "/internal/review/by-event",
      { body: { event_id: "000000000000" } });
    eq("C-6 未知事件 found 为 false", miss.body.found, false);
    eq("C-6 未知事件 review_ref 为空", miss.body.review_ref, "");
  }

  /* 36. ★ 私有状态持久化：enqueue 之后状态可查、日志完整 */
  resetRoutes();
  {
    const env = baseEnv();
    const ref = await seedReview(env, "1234abcd5678");
    const st = await internal(env, `/internal/review/${ref}/state`, { method: "GET" });
    eq("C-7 入队后状态为 pending", st.body.status, "pending");
    eq("C-7 状态里带 event_id", st.body.event_id, "1234abcd5678");
    const log = await internal(env, `/internal/review/${ref}/log`, { method: "POST", body: {} });
    check("C-7 操作日志已落库",
      (log.body.entries || []).some((e) => e.action === "ENQUEUED"),
      JSON.stringify(log.body.entries));
    // 生产客户端用的是 GET（review_store.HttpReviewStore.log）。只认 POST 的话
    // 会 405，而 Python 侧把异常吞成空列表——「审核日志永远是空的」且不报错。
    const logGet = await internal(env, `/internal/review/${ref}/log`, { method: "GET" });
    check("C-7 日志接口支持 GET",
      (logGet.body.entries || []).some((e) => e.action === "ENQUEUED"),
      JSON.stringify(logGet.body));
    // 状态查询**不**返回 payload 正文
    const raw = JSON.stringify(st.body);
    check("C-7 状态接口不泄露正文", !raw.includes("某队主帅"), raw);
  }

  /* 37. ★ Worker 重启后 dispatch 冷却仍然有效 */
  resetRoutes();
  {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "vault-"));
    const dbPath = path.join(dir, "vault.db");
    at("2026-11-06T02:00:00Z");
    const env1 = baseEnv();
    const v1 = makeVault(env1, dbPath);
    env1.VAULT = makeNamespace(v1);
    const c1 = await v1.fetch(new Request("https://v/ops/cooldown", {
      method: "POST",
      headers: { "content-type": "application/json", "X-Service-Key": SERVICE_KEY },
      body: JSON.stringify({ now_ms: NOW, cooldown_ms: 600000, mark: true }),
    })).then((r) => r.json());
    eq("C-8 首次占用冷却成功", c1.blocked, false);

    // 「重启」：同一份持久化存储上新建一个 DO 实例
    const env2 = baseEnv();
    const v2 = makeVault(env2, dbPath);
    env2.VAULT = makeNamespace(v2);
    const c2 = await v2.fetch(new Request("https://v/ops/cooldown", {
      method: "POST",
      headers: { "content-type": "application/json", "X-Service-Key": SERVICE_KEY },
      body: JSON.stringify({ now_ms: NOW + 60000, cooldown_ms: 600000, mark: true }),
    })).then((r) => r.json());
    eq("C-8 重启后冷却仍然生效", c2.blocked, true);

    // 冷却窗口过去之后必须能重新占用
    const c3 = await v2.fetch(new Request("https://v/ops/cooldown", {
      method: "POST",
      headers: { "content-type": "application/json", "X-Service-Key": SERVICE_KEY },
      body: JSON.stringify({ now_ms: NOW + 660000, cooldown_ms: 600000, mark: true }),
    })).then((r) => r.json());
    eq("C-8 冷却过期后可再次占用", c3.blocked, false);
  }

  /* 38. ★ Worker 重启后告警去重仍然有效 */
  resetRoutes();
  {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "vault-"));
    const dbPath = path.join(dir, "vault.db");
    const env1 = baseEnv();
    const v1 = makeVault(env1, dbPath);
    const claim = (v, kind) => v.fetch(new Request("https://v/ops/fault", {
      method: "POST",
      headers: { "content-type": "application/json", "X-Service-Key": SERVICE_KEY },
      body: JSON.stringify({ kind, action: "claim_alert" }),
    })).then((r) => r.json());

    eq("C-9 首次告警名额抢到", (await claim(v1, "heartbeat_stale")).should_alert, true);
    eq("C-9 同一实例第二次抢不到", (await claim(v1, "heartbeat_stale")).should_alert, false);

    const env2 = baseEnv();
    const v2 = makeVault(env2, dbPath);
    eq("C-9 重启后仍然抢不到", (await claim(v2, "heartbeat_stale")).should_alert, false);

    // 恢复后清除 → 下个故障周期重新可告警
    await v2.fetch(new Request("https://v/ops/fault", {
      method: "POST",
      headers: { "content-type": "application/json", "X-Service-Key": SERVICE_KEY },
      body: JSON.stringify({ kind: "heartbeat_stale", action: "clear" }),
    }));
    eq("C-9 恢复后可重新告警", (await claim(v2, "heartbeat_stale")).should_alert, true);
  }

  /* 39. 内部接口鉴权 */
  resetRoutes();
  {
    const env = baseEnv();
    const bad = await internal(env, "/internal/review/enqueue",
      { body: { payload: { event_id: "x" } }, key: "wrong" });
    eq("C-10 错误服务密钥被拒", bad.status, 401);
    const noKey = await internal(env, "/internal/review/enqueue",
      { body: { payload: { event_id: "x" } }, key: "" });
    eq("C-10 无服务密钥被拒", noKey.status, 401);
    const noDurable = await internal(baseEnv({ VAULT: undefined }),
      "/internal/review/enqueue", { body: { payload: { event_id: "x" } } });
    eq("C-10 无 DO 绑定时拒绝（不退回公开存储）", noDurable.status, 503);
  }

  /* 40. ★★ P1：claim 租约过期后不得自动重复投递
   *
   * 这是 PR #1 审查发现的第二个缺陷。旧 SQL 里有：
   *
   *     OR (status='claimed' AND claim_expires_at < ?)
   *
   * 语义是「上一任超时了，换你上」。但投递不是可回滚的动作：
   *
   *     取到 claim → Telegram 发送成功 → 进程被杀 → 租约过期
   *                                              → 另一个 job 认领 → 重复推送
   *
   * 发送与 finish 回写不可能构成一个原子事务，所以「租约过期」只说明
   * **结果未知**，不说明**没发出去**。下面这组断言完整复现这条时间线。
   */
  resetRoutes();
  {
    const env = baseEnv();
    const v = makeVault(env, ":memory:");
    env.VAULT = makeNamespace(v);
    const call = (p, body, method) => vaultCall(v, p, body, method);

    const ref = await seedDirect(v, "eeee11112222");
    await call(`/review/${ref}/decide`, { action: "approve", operator_id: "111" });

    // ① 成功取得 claim
    const c1 = await call(`/review/${ref}/claim`, {});
    eq("C-11a 首次认领成功", c1.ok, true);
    check("C-11a 拿到一次性 claim_token", !!c1.claim_token, c1.claim_token);

    // ② 模拟 Telegram 已发送成功（消息真的出去了，但我们不 finish）
    // ③ 模拟进程在 finish 回写前中断 —— 什么都不做，就是断点
    const mid = await call(`/review/${ref}/state`, undefined, "GET");
    eq("C-11a 中断后状态停在 claimed", mid.status, "claimed");

    // ④ 把租约推进到过期
    expireLease(v, ref);

    // ⑤ 新任务不能自动重新发送
    const c2 = await call(`/review/${ref}/claim`, {});
    eq("C-11a 过期后不能重新认领", c2.ok, false);
    check("C-11a 过期认领不签发 claim_token", !c2.claim_token, c2.claim_token);
    eq("C-11a 过期认领标记 needs_manual", c2.needs_manual, true);

    // ⑥ 状态进入人工核查
    const after = await call(`/review/${ref}/state`, undefined, "GET");
    eq("C-11a 状态转为 needs_manual", after.status, "needs_manual");
    check("C-11a last_error 记录租约过期",
      String(after.last_error || "").includes("租约"), after.last_error);
    eq("C-11a claim_token 已被清空（旧 token 无法再 finish）", after.claim_token || "", "");

    // 旧 token 不能再用来完成投递（进程即使活过来也回写不了）
    const late = await call(`/review/${ref}/finish`,
      { claim_token: c1.claim_token, outcome: "delivered", message_ids: ["1"] });
    eq("C-11a 过期后旧 token 无法回写 delivered", late.ok, false);

    // 日志里必须留下这次「被迫转人工」的痕迹
    const log = await call(`/review/${ref}/log`, undefined, "GET");
    check("C-11a 日志记录 LEASE_EXPIRED",
      (log.entries || []).some((e) => e.action === "LEASE_EXPIRED"),
      JSON.stringify((log.entries || []).map((e) => e.action)));
  }

  /* 41. ★★ P1：needs_manual 是「停下来等人」，不是「过一会儿再试」 */
  resetRoutes();
  {
    const env = baseEnv();
    const v = makeVault(env, ":memory:");
    env.VAULT = makeNamespace(v);
    const call = (p, body, method) => vaultCall(v, p, body, method);
    const ref = await seedDirect(v, "eeee22223333");
    await call(`/review/${ref}/decide`, { action: "approve", operator_id: "111" });
    await call(`/review/${ref}/claim`, {});
    expireLease(v, ref);
    await call(`/review/${ref}/claim`, {});          // 转 needs_manual

    // 反复认领都只会得到 needs_manual，永远不会拿到 token
    for (let i = 0; i < 3; i += 1) {
      const again = await call(`/review/${ref}/claim`, {});
      eq(`C-11b 第 ${i + 1} 次重复认领仍被拒`, again.ok, false);
      eq(`C-11b 第 ${i + 1} 次仍标记 needs_manual`, again.needs_manual, true);
      check(`C-11b 第 ${i + 1} 次仍不签发 token`, !again.claim_token, again.claim_token);
    }
    const st = await call(`/review/${ref}/state`, undefined, "GET");
    eq("C-11b 反复认领后状态仍是 needs_manual", st.status, "needs_manual");
    check("C-11b 尝试次数没有把记录推回可认领",
      st.status !== "decided" && st.status !== "claimed", st.status);
  }

  /* 42. ★★ P1：恢复必须显式，且必须有人签名 */
  resetRoutes();
  {
    const env = baseEnv();
    const v = makeVault(env, ":memory:");
    env.VAULT = makeNamespace(v);
    const call = (p, body, method) => vaultCall(v, p, body, method);
    const ref = await seedDirect(v, "eeee33334444");
    await call(`/review/${ref}/decide`, { action: "approve", operator_id: "111" });
    await call(`/review/${ref}/claim`, {});
    expireLease(v, ref);
    await call(`/review/${ref}/claim`, {});          // → needs_manual

    // 没有 operator_id 的放行必须被拒（否则 needs_manual 就是橡皮图章）
    const anon = await call(`/review/${ref}/release`, { note: "看着像没发出去" });
    eq("C-11c 无 operator_id 的放行被拒", anon.ok, false);
    const still = await call(`/review/${ref}/state`, undefined, "GET");
    eq("C-11c 被拒的放行不改变状态", still.status, "needs_manual");

    // 对非 needs_manual 记录放行也要被拒（不能当万能钥匙）
    const other = await seedDirect(v, "eeee44445555");
    const wrong = await call(`/review/${other}/release`, { operator_id: "111" });
    eq("C-11c 对 pending 记录放行被拒", wrong.ok, false);

    // 显式放行 → decided → 可以重新认领
    const ok = await call(`/review/${ref}/release`,
      { operator_id: "editor_01", note: "已核对频道无该消息" });
    eq("C-11c 带签名的放行成功", ok.ok, true);
    eq("C-11c 放行后回到 decided", ok.status, "decided");
    const re = await call(`/review/${ref}/claim`, {});
    eq("C-11c 放行后可以重新认领", re.ok, true);
    check("C-11c 重新认领拿到新 token", !!re.claim_token, re.claim_token);

    const log = await call(`/review/${ref}/log`, undefined, "GET");
    const rel = (log.entries || []).find((e) => e.action === "RELEASED:manual");
    check("C-11c 日志记录放行人", !!rel && rel.actor === "editor_01", JSON.stringify(rel));
  }

  /* 43. ★★ P1：确定性失败（failed）的重试机制必须保留
   *
   * 与「结果未知」相对的另一端：failed 是「明确没发出去」（HTTP 400/401），
   * 重发不会造成重复。加固不能把这条正常重试路径一起堵死。
   */
  resetRoutes();
  {
    const env = baseEnv();
    const v = makeVault(env, ":memory:");
    env.VAULT = makeNamespace(v);
    const call = (p, body, method) => vaultCall(v, p, body, method);
    const ref = await seedDirect(v, "eeee55556666");
    await call(`/review/${ref}/decide`, { action: "approve", operator_id: "111" });

    const c1 = await call(`/review/${ref}/claim`, {});
    eq("C-11d 首次认领成功", c1.ok, true);
    // 确定性失败：明确知道没发出去
    const f1 = await call(`/review/${ref}/finish`,
      { claim_token: c1.claim_token, outcome: "failed", error: "HTTP 400 格式错误" });
    eq("C-11d 确定性失败可以回写", f1.ok, true);
    eq("C-11d 状态为 failed", f1.status, "failed");

    // failed 允许重新认领（这是合理重试，不是重复投递）
    const c2 = await call(`/review/${ref}/claim`, {});
    eq("C-11d failed 可以重新认领", c2.ok, true);
    check("C-11d 重试拿到新 token", !!c2.claim_token, c2.claim_token);
    check("C-11d 新 token 与旧 token 不同",
      c2.claim_token !== c1.claim_token, c2.claim_token);

    const st = await call(`/review/${ref}/state`, undefined, "GET");
    eq("C-11d attempts 已累加", st.attempts, 2);
  }

  /* 44. ★★ P1：needs_manual 与 failed 的语义必须互不污染 */
  resetRoutes();
  {
    const env = baseEnv();
    const v = makeVault(env, ":memory:");
    env.VAULT = makeNamespace(v);
    const call = (p, body, method) => vaultCall(v, p, body, method);

    // failed：可以重试
    const a = await seedDirect(v, "eeee66667777");
    await call(`/review/${a}/decide`, { action: "approve", operator_id: "111" });
    const ca = await call(`/review/${a}/claim`, {});
    await call(`/review/${a}/finish`,
      { claim_token: ca.claim_token, outcome: "failed", error: "HTTP 400" });
    const ca2 = await call(`/review/${a}/claim`, {});
    eq("C-11e failed 可重试", ca2.ok, true);

    // needs_manual：不可重试
    const b = await seedDirect(v, "eeee77778888");
    await call(`/review/${b}/decide`, { action: "approve", operator_id: "111" });
    await call(`/review/${b}/claim`, {});
    expireLease(v, b);
    await call(`/review/${b}/claim`, {});
    const cb = await call(`/review/${b}/claim`, {});
    eq("C-11e needs_manual 不可重试", cb.ok, false);
    eq("C-11e needs_manual 明确标记", cb.needs_manual, true);
    check("C-11e failed 的响应不带 needs_manual",
      !ca2.needs_manual && ca2.ok === true, JSON.stringify(ca2));
  }

  /* 45. ★★ P1：跨实例（重启）后过期租约同样不能自动重认领 */
  resetRoutes();
  {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "vault-"));
    const dbPath = path.join(dir, "vault.db");
    const env1 = baseEnv();
    const v1 = makeVault(env1, dbPath);
    env1.VAULT = makeNamespace(v1);
    const ref = await seedDirect(v1, "eeee88889999");
    await vaultCall(v1, `/review/${ref}/decide`, { action: "approve", operator_id: "111" });
    const c1 = await vaultCall(v1, `/review/${ref}/claim`, {});
    eq("C-11f 实例一认领成功", c1.ok, true);
    expireLease(v1, ref);

    // 「重启」：同一份持久化存储上新建 DO 实例
    const env2 = baseEnv();
    const v2 = makeVault(env2, dbPath);
    env2.VAULT = makeNamespace(v2);
    const c2 = await vaultCall(v2, `/review/${ref}/claim`, {});
    eq("C-11f 重启后不能自动重新认领", c2.ok, false);
    eq("C-11f 重启后标记 needs_manual", c2.needs_manual, true);
    const st = await vaultCall(v2, `/review/${ref}/state`, undefined, "GET");
    eq("C-11f 重启后状态是 needs_manual", st.status, "needs_manual");
  }
}

/* ========================= 主流程 ========================= */
try {
  await testWebhook();
  await testWatchdog();
  await testVault();
} catch (err) {
  check("harness", false, "测试脚本自身异常：" + (err && err.stack ? err.stack : err));
}

Date.now = REAL_NOW;

const failed = results.filter((r) => !r.ok);
if (process.argv.includes("--json")) {
  console.log(JSON.stringify({ total: results.length, failed: failed.length, results }, null, 2));
} else {
  for (const r of results) {
    console.log((r.ok ? "  PASS " : "  FAIL ") + r.name + (r.detail ? "  ← " + r.detail : ""));
  }
  console.log(`\n${results.length - failed.length}/${results.length} passed`);
}
process.exit(failed.length === 0 ? 0 : 1);
