/**
 * 越位情报局 —— 外部看门狗 / 故障转移调度器 / 审核回调网关（Cloudflare Worker）
 *
 * ============================================================================
 * 这个文件干三件事，刻意放在同一个 Worker 入口（免费额度只给一个域名）
 * ============================================================================
 *   1. POST /                 —— Telegram webhook：接收私有审核群的「批准/拒绝」按钮
 *   2. GET  /                 —— 看门狗：读 state.json 心跳，必要时补触发扫描并告警
 *   3. /internal/*            —— 给 GitHub Actions 的私有内部接口（服务密钥认证）
 *
 * 三条分支互不越界：
 *   · webhook 分支从不读心跳、从不 dispatch scan
 *   · 看门狗分支从不处理 callback_query
 *   · 内部接口只服务 Actions，不对外
 *
 * ============================================================================
 * 一、P0：审核资料不进公开仓库
 * ============================================================================
 * 本仓库是 Public，state.json 被 git 跟踪并被每轮扫描提交。
 * 所以「待审事件的标题/摘要/证据」一旦写进 state.json 就等于公开发布。
 *
 * 数据边界（本文件是执行者）：
 *
 *   Telegram 按钮 → 本 Worker → Durable Object（SQLite，存完整 payload）
 *                              → workflow_dispatch（只带 opaque review_id）
 *                              → Actions 用服务密钥回取 payload
 *                              → 投递 Telegram → 回写投递结果
 *
 * **workflow_dispatch 的 inputs 只有 {mode, review_id}。**
 * 不带正文、不带证据、不带审核人、不带 HMAC、不带任何可复用令牌。
 * Actions 需要完整 payload 时，必须用 GitHub Secret 里的 REVIEW_SERVICE_KEY
 * 调 /internal/review/<id>/claim 换取，且 claim 是一次性的。
 *
 * ============================================================================
 * 二、为什么看门狗必须住在外部
 * ============================================================================
 * 突发扫描靠 cron-job.org 调 GitHub workflow_dispatch。它一旦停摆，GitHub 那边
 * 什么都不会发生——而仓库里的 watchdog 也靠 GitHub schedule 触发，共用同一条
 * 不可靠链路（实测该仓库 3 天只触发 7 次 schedule，早报迟到 207 分钟）。
 * 结果就是：调度停了，却没有任何人知道。
 *
 * ============================================================================
 * 三、V1.0 硬化修掉的四个真实缺陷
 * ============================================================================
 *   缺陷 1：心跳超阈值后**每分钟**重复 dispatch（dispatch 完不记账）。
 *   缺陷 2：`alertOnce` 名不副实——没有任何持久化，靠
 *           `age >= alertMin && age < alertMin + 3` 的三分钟窗口。
 *   缺陷 3：读 state 失败时每分钟告警一次（age 恒为 null，进不了窗口判断）。
 *   缺陷 4：dispatch 失败被静默吞掉。
 *
 * 修法：所有跨 invocation 的状态（dispatch 冷却、故障告警名额、故障首次发现
 * 时间）一律放进 **SQLite Durable Object**，用条件 UPDATE 做原子迁移。
 * 不再有「内存变量冒充持久化锁」——DO 未绑定时直接**拒绝接管**并如实上报，
 * 而不是用一个假锁继续跑。
 *
 * ============================================================================
 * 四、阈值从哪来（实测，不是拍的）
 * ============================================================================
 * 从仓库 state.json 的 400 条历史提交里提取 394 个 last_scan_at 间隔：
 *   p10 0.83 / p50 1.01 / p90 1.29 分钟  ← 93.4% 的间隔 ≤ 2 分钟
 *   其余 6.6% 为 20 / 27 / 41 / 95 / 130 … 511 分钟  ← 真实停摆
 * 也就是说正常节奏是 1 分钟（cron-job.org），5 分钟是 GitHub schedule 的兜底。
 *
 *   EXPECTED_SCAN_INTERVAL_MINUTES = 5   兜底间隔（可配置）
 *   FAILOVER_MINUTES = 12                兜底 5 + scan 超时 6 + 1 分钟余量
 *   DISPATCH_COOLDOWN_MINUTES = 10       接管后至少等 10 分钟再试
 *   ALERT_MINUTES = 30                   必须先经历 ≥1 次接管失败才告警
 *   READ_FAIL_ALERT_MINUTES = 8          连续读失败 8 分钟才告警
 *
 * 「dispatch 成功」≠「扫描成功」。只有 state.json 的 last_scan_at 前进了才算恢复。
 *
 * ============================================================================
 * 五、部署要点（详见 README.md 与交付报告里的配置清单）
 * ============================================================================
 *   wrangler secret put GITHUB_TOKEN              PAT：actions:write + contents:read
 *   wrangler secret put TELEGRAM_BOT_TOKEN
 *   wrangler secret put TELEGRAM_OPS_CHAT_ID      私有运维群（不要用公共情报频道）
 *   wrangler secret put TELEGRAM_WEBHOOK_SECRET   setWebhook 的 secret_token
 *   wrangler secret put TELEGRAM_REVIEW_CHAT_ID   私有审核群
 *   wrangler secret put REVIEW_OPERATOR_IDS       逗号分隔的 Telegram User ID
 *   wrangler secret put REVIEW_CALLBACK_HMAC_SECRET   与 GitHub Secret 同值
 *   wrangler secret put INTERNAL_SERVICE_KEY      与 GitHub Secret REVIEW_SERVICE_KEY 同值
 *   wrangler deploy
 *
 * Durable Object 绑定与 SQLite 迁移见 wrangler.toml（new_sqlite_classes）。
 */

import { Vault } from "./review_vault.js";

// Cloudflare 要求 Durable Object 的类从入口模块导出
export { Vault };

const API = "https://api.github.com";
const TG = "https://api.telegram.org";

/* ==================== 阈值（B4 校准） ==================== */
const DEFAULT_EXPECTED_SCAN_INTERVAL_MINUTES = 5;
const DEFAULT_FAILOVER_MINUTES = 12;
const DEFAULT_DISPATCH_COOLDOWN_MINUTES = 10;
const DEFAULT_ALERT_MINUTES = 30;
const DEFAULT_READ_FAIL_ALERT_MINUTES = 8;
const DEFAULT_MORNING_GRACE_MINUTES = 180; // 必须与 scripts/ensure_morning_running.py 一致

/* 审核回调 */
const REVIEW_CALLBACK_VERSION = "v1";
const REVIEW_ACTION_APPROVE = "a";
const REVIEW_ACTION_DECLINE = "d";

/* 故障类型（B5）。每种故障独立记账、独立告警、独立恢复。 */
const FAULT = {
  GITHUB_API: "github_api_unavailable",
  STATE_READ: "state_read_failed",
  HEARTBEAT: "heartbeat_stale",
  DISPATCH_FAILED: "dispatch_failed",
  NO_RECOVERY: "dispatch_no_recovery",
  MORNING: "morning_missed",
  DURABLE: "durable_object_unavailable",
};

const ACTIVE_RUN_STATUSES = ["queued", "in_progress", "waiting", "pending", "requested"];

export default {
  /** 定时触发：wrangler.toml 里 crons = ["* * * * *"] */
  async scheduled(event, env, ctx) {
    ctx.waitUntil(tick(env, "cron"));
  },

  /**
   * 路由：
   *   POST /                      → Telegram webhook（审核按钮）
   *   POST/GET /internal/*        → GitHub Actions 私有内部接口
   *   GET  /?force=1              → 强制走一次接管逻辑（调试用，会真的 dispatch）
   *   GET  /?dry=1                → 只读诊断（默认行为，无副作用）
   */
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (url.pathname.startsWith("/internal/")) {
      return handleInternal(request, env, url);
    }

    if (request.method === "POST") {
      return handleTelegramWebhook(request, env, ctx);
    }
    if (request.method !== "GET" && request.method !== "HEAD") {
      return json({ ok: false, error: "method_not_allowed" }, 405);
    }

    if (url.searchParams.get("force") === "1") {
      const out = await tick(env, "manual-force", { dryRun: false });
      return json({ ...out, forced: true });
    }
    if (url.searchParams.get("dry") === "1") {
      const out = await tick(env, "http-dry", { dryRun: true });
      return json(out);
    }
    const out = await tick(env, "http", { dryRun: true });
    return json({ ...out, hint: "只读诊断。加 ?force=1 才会真的接管 dispatch" });
  },
};

/* ============================================================================
 * 一、Durable Object 客户端
 * ==========================================================================*/

/**
 * 所有跨 invocation 的状态都经由这一个 DO 实例。
 *
 * 为什么是单实例（idFromName("vault")）：审核记录的「同一 event_id 只允许一条
 * 有效记录」是一个**跨记录**约束，分散到多个 DO 就退化成最终一致。
 * 单实例的代价是串行，但量级是每分钟个位数请求，完全够用。
 */
function vault(env) {
  const ns = env.VAULT;
  if (!ns || typeof ns.idFromName !== "function") return null;
  const stub = ns.get(ns.idFromName("vault"));
  const key = str(env.INTERNAL_SERVICE_KEY);
  return {
    async call(path, method = "POST", body) {
      const res = await stub.fetch("https://vault.internal" + path, {
        method,
        headers: {
          "content-type": "application/json",
          "X-Service-Key": key,
        },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      const data = await res.json().catch(() => ({}));
      return { httpStatus: res.status, ...data };
    },
  };
}

/* ============================================================================
 * 二、内部接口（GitHub Actions 专用）
 * ==========================================================================*/

/**
 * 代理到 Durable Object。
 *
 * 认证：`X-Service-Key` 必须等于 Worker secret `INTERNAL_SERVICE_KEY`，
 * 与 GitHub Secret `REVIEW_SERVICE_KEY` 同值。
 *
 * 这条通道是 Actions 拿到完整 payload 的**唯一**途径。
 * 正文、证据、审核人、操作日志全部只在这里流动，不进 git、不进日志。
 */
async function handleInternal(request, env, url) {
  const expected = str(env.INTERNAL_SERVICE_KEY);
  if (!expected) {
    return json({ ok: false, error: "internal_service_key_not_configured" }, 503);
  }
  const got = request.headers.get("X-Service-Key") || "";
  if (!safeEqual(got, expected)) {
    return json({ ok: false, error: "bad_service_key" }, 401);
  }

  const v = vault(env);
  if (!v) {
    // 没有 DO 绑定 = 没有私有审核存储 = 拒绝一切审核操作。
    // 绝不退回到「把 payload 写进 state.json」那条老路。
    return json({ ok: false, error: "durable_object_not_bound" }, 503);
  }

  // /internal/review/... → DO /review/...
  // /internal/ops/...    → DO /ops/...
  const path = url.pathname.replace(/^\/internal/, "");
  let body;
  if (request.method === "POST") {
    try {
      body = await request.json();
    } catch (err) {
      return json({ ok: false, error: "bad_json" }, 400);
    }
  }
  const out = await v.call(path + (url.search || ""), request.method, body);
  const status = out.httpStatus || 200;
  delete out.httpStatus;
  return json(out, status);
}

/* ============================================================================
 * 三、Telegram webhook（A3）
 * ==========================================================================*/

/**
 * 处理 Telegram 的 callback_query。
 *
 * 校验顺序（任何一步失败都不 dispatch，且都不产生副作用）：
 *   1. Webhook Secret Token   —— 证明这条 POST 真的来自 Telegram
 *   2. 是不是 callback_query  —— 其它 update 一律 200 忽略（否则 Telegram 会重投）
 *   3. 操作人 User ID 白名单
 *   4. 回调来自指定的审核 Chat
 *   5. HMAC 签名 + 有效期      —— 不能仅凭 review_ref 就接受批准
 *   6. **DO 里的原子状态迁移** —— 真正的幂等权威：重复点击、旧按钮重放、
 *                                 两人同时点，都只有第一次能改变状态
 *
 * 只要通过 1、2 两步就回 200：Telegram 收到非 200 会重投同一个 update，
 * 重投会带来重复 dispatch。业务拒绝通过 answerCallbackQuery 告知操作人。
 */
async function handleTelegramWebhook(request, env, ctx) {
  const secret = str(env.TELEGRAM_WEBHOOK_SECRET);
  if (!secret) {
    return json({ ok: false, error: "webhook_secret_not_configured" }, 503);
  }
  const got = request.headers.get("X-Telegram-Bot-Api-Secret-Token") || "";
  if (!safeEqual(got, secret)) {
    return json({ ok: false, error: "bad_webhook_secret" }, 401);
  }

  let update;
  try {
    update = await request.json();
  } catch (err) {
    return json({ ok: false, error: "bad_json" }, 400);
  }

  const cq = update && update.callback_query;
  if (!cq || !cq.id) {
    return json({ ok: true, ignored: "not_callback_query" });
  }

  const fromId = String((cq.from && cq.from.id) != null ? cq.from.id : "");
  const chatId = String(
    (cq.message && cq.message.chat && cq.message.chat.id) != null
      ? cq.message.chat.id
      : "",
  );
  const data = typeof cq.data === "string" ? cq.data : "";

  const operators = parseIdSet(env.REVIEW_OPERATOR_IDS);
  if (operators.size === 0) {
    ctx.waitUntil(answerCallback(env, cq.id, "审核人白名单未配置，操作被拒绝"));
    return json({ ok: true, rejected: "operators_not_configured" });
  }
  if (!operators.has(fromId)) {
    ctx.waitUntil(answerCallback(env, cq.id, "无权操作：你不在审核人白名单"));
    return json({ ok: true, rejected: "operator_not_allowed", from_id: fromId });
  }

  const reviewChat = str(env.TELEGRAM_REVIEW_CHAT_ID);
  if (!reviewChat || chatId !== reviewChat) {
    ctx.waitUntil(answerCallback(env, cq.id, "回调不是来自审核群，已拒绝"));
    return json({ ok: true, rejected: "not_review_chat", chat_id: chatId });
  }

  const verified = await verifyCallbackData(data, env.REVIEW_CALLBACK_HMAC_SECRET);
  if (!verified.ok) {
    ctx.waitUntil(answerCallback(env, cq.id, "按钮无效或已过期：" + verified.reason));
    return json({ ok: true, rejected: "bad_callback", reason: verified.reason });
  }

  const { reviewRef, action } = verified;
  const v = vault(env);
  if (!v) {
    ctx.waitUntil(answerCallback(env, cq.id, "私有审核存储不可用，操作未生效"));
    return json({ ok: false, error: "durable_object_not_bound" }, 503);
  }

  // ---- 原子登记审核决定 ----
  // 这一步同时承担三件事：确认记录存在、确认仍在待审、以及**一次性**。
  // 重复点击 / 旧按钮重放 / 两人同时点，都会在这里被 changes()==0 挡掉。
  const decided = await v.call("/review/" + reviewRef + "/decide", "POST", {
    action: action === REVIEW_ACTION_APPROVE ? "approve" : "decline",
    operator_id: fromId,
    note: "via telegram",
  });
  if (decided.ok !== true) {
    const msg = String(decided.error || "");
    const text = decided.conflict
      ? "该事件已处理过（" + msg + "）"
      : "操作未生效：" + msg;
    ctx.waitUntil(answerCallback(env, cq.id, text.slice(0, 190)));
    return json({ ok: true, rejected: "already_handled", review_ref: reviewRef,
                  reason: msg });
  }

  const actionName = action === REVIEW_ACTION_APPROVE ? "approve" : "decline";

  ctx.waitUntil(
    (async () => {
      await answerCallback(
        env,
        cq.id,
        action === REVIEW_ACTION_APPROVE ? "已提交批准，正在投递…" : "已提交拒绝，正在存档…",
      );
      // ⚠️ inputs 只有两个字段：mode 与 opaque review_id。
      // 正文、证据、审核人、HMAC、任何可复用令牌都**不**出现在这里。
      const res = await dispatchWorkflow(env, {
        mode: "review_action",
        review_id: reviewRef,
      });
      if (!res.ok) {
        await v.call("/ops/fault", "POST", { kind: "review_dispatch_failed", action: "touch" });
        await sendOpsAlert(
          env,
          "❌ 审核回调未能启动流水线\n" +
            "审核句柄：" + reviewRef + "\n" +
            "动作：" + actionName + "\n" +
            "错误：" + res.error + "\n" +
            "请到 Actions 页面手动执行 mode=review_action。",
        );
      }
    })(),
  );

  return json({ ok: true, accepted: true, review_ref: reviewRef, action: actionName });
}

/** 校验 callback_data：格式 → 版本 → 动作 → 签名 → 有效期 */
async function verifyCallbackData(data, secretRaw) {
  const secret = str(secretRaw);
  if (!secret) return { ok: false, reason: "HMAC 密钥未配置" };
  if (!data) return { ok: false, reason: "回调数据为空" };

  const parts = data.split(":");
  if (parts.length !== 5) return { ok: false, reason: "回调数据格式错误" };
  const [ver, action, reviewRef, expS, sig] = parts;

  if (ver !== REVIEW_CALLBACK_VERSION) return { ok: false, reason: "版本不支持" };
  if (action !== REVIEW_ACTION_APPROVE && action !== REVIEW_ACTION_DECLINE) {
    return { ok: false, reason: "未知动作" };
  }
  if (!/^[0-9a-f]{8,32}$/i.test(reviewRef)) return { ok: false, reason: "审核句柄格式非法" };
  const exp = Number(expS);
  if (!Number.isFinite(exp) || exp <= 0) return { ok: false, reason: "有效期字段非法" };

  const want = await hmacSig(secret, ver + "|" + action + "|" + reviewRef + "|" + exp);
  if (!safeEqual(want, sig)) return { ok: false, reason: "签名校验失败" };
  if (Math.floor(Date.now() / 1000) > exp) return { ok: false, reason: "按钮已过期（重放被拒）" };

  return { ok: true, action, reviewRef };
}

/**
 * HMAC-SHA256 hex，取前 16 位。
 * 必须与 bot.py 的 `_review_callback_sig` 逐字节一致：
 *   hmac.new(key, "v1|a|<review_ref>|<exp>", sha256).hexdigest()[:16]
 */
async function hmacSig(secret, message) {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const buf = await crypto.subtle.sign("HMAC", key, enc.encode(message));
  let hex = "";
  for (const b of new Uint8Array(buf)) hex += b.toString(16).padStart(2, "0");
  return hex.slice(0, 16);
}

/** 常数时间字符串比较（避免时序侧信道） */
function safeEqual(a, b) {
  const x = String(a == null ? "" : a);
  const y = String(b == null ? "" : b);
  if (x.length !== y.length) return false;
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= x.charCodeAt(i) ^ y.charCodeAt(i);
  return diff === 0;
}

function parseIdSet(raw) {
  return new Set(
    String(raw == null ? "" : raw)
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean),
  );
}

/** 立即回应 callback query，去掉按钮上的转圈动画 */
async function answerCallback(env, callbackQueryId, text) {
  if (!env.TELEGRAM_BOT_TOKEN) return false;
  try {
    const res = await fetch(
      TG + "/bot" + env.TELEGRAM_BOT_TOKEN + "/answerCallbackQuery",
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ callback_query_id: callbackQueryId, text, show_alert: false }),
      },
    );
    return res.ok;
  } catch (err) {
    return false;
  }
}

/* ============================================================================
 * 四、看门狗主流程（B3 / B4 / B5）
 * ==========================================================================*/

async function tick(env, trigger, opts = {}) {
  const dryRun = opts.dryRun === true;
  const repo = str(env.REPO) || "markmilk20020610-art/premier-league-bot";
  const workflow = str(env.WORKFLOW_FILE) || "scanner.yml";
  const expected = num(env.EXPECTED_SCAN_INTERVAL_MINUTES, DEFAULT_EXPECTED_SCAN_INTERVAL_MINUTES);
  const failoverMin = num(env.FAILOVER_MINUTES, DEFAULT_FAILOVER_MINUTES);
  const cooldownMin = num(env.DISPATCH_COOLDOWN_MINUTES, DEFAULT_DISPATCH_COOLDOWN_MINUTES);
  const alertMin = num(env.ALERT_MINUTES, DEFAULT_ALERT_MINUTES);
  const readFailAlertMin = num(env.READ_FAIL_ALERT_MINUTES, DEFAULT_READ_FAIL_ALERT_MINUTES);
  const morningGrace = num(env.MORNING_GRACE_MINUTES, DEFAULT_MORNING_GRACE_MINUTES);

  const v = vault(env);

  const result = {
    trigger,
    dry_run: dryRun,
    repo,
    checked_at: new Date(Date.now()).toISOString(),
    durable: !!v,
    store: v ? "durable_object_sqlite" : "unbound",
    thresholds: {
      expected_scan_interval_minutes: expected,
      failover_minutes: failoverMin,
      dispatch_cooldown_minutes: cooldownMin,
      alert_minutes: alertMin,
      read_fail_alert_minutes: readFailAlertMin,
      morning_grace_minutes: morningGrace,
    },
    last_scan_at: null,
    age_minutes: null,
    action: "none",
    dispatched: false,
    faults: [],
    alerted: [],
    recovered: [],
    errors: [],
  };

  /* ---------------- 1. 读心跳 ---------------- */
  let heartbeat;
  try {
    heartbeat = await readHeartbeat(env, repo);
    result.last_scan_at = heartbeat.last_scan_at;
    result.age_minutes = round1(heartbeat.age_minutes);
  } catch (err) {
    // 读失败 ≠ 流水线停了。可能是 token 过期、API 抖动、或 state.json 被写坏。
    // 此时**绝不 dispatch**：盲目接管只会在故障期制造更多噪声和重复运行。
    const kind = err && err.kind === "network" ? FAULT.GITHUB_API : FAULT.STATE_READ;
    result.faults.push(kind);
    result.errors.push(kind + ": " + (err && err.message ? err.message : String(err)));
    if (!dryRun) {
      const f = await faultTouch(v, kind);
      result.fault_age_minutes = f.age_minutes;
      if (f.age_minutes >= readFailAlertMin) {
        const a = await alertFault(env, v, kind, {
          title:
            kind === FAULT.GITHUB_API
              ? "GitHub API 不可用，看门狗读不到心跳"
              : "state.json 读取失败，看门狗读不到心跳",
          detail:
            (err && err.message ? err.message : String(err)) +
            "\n已连续 " + Math.round(f.age_minutes) + " 分钟。",
          action: "未接管：读不到心跳时盲目 dispatch 会制造重复运行，等下一轮重试",
        });
        if (a.sent) result.alerted.push(kind);
      }
    }
    result.action = "read_failed";
    return result;
  }

  const age = result.age_minutes;
  if (age === null || !Number.isFinite(age)) {
    result.action = "skip_unknown_age";
    return result;
  }

  /* ---------------- 2. 读 GitHub run 列表（第二道防重复闸） ---------------- */
  let runs = [];
  let runsError = null;
  try {
    runs = await listRuns(env, repo, workflow);
  } catch (err) {
    runsError = err && err.message ? err.message : String(err);
    result.errors.push("list_runs: " + runsError);
  }

  const activeRuns = runs.filter((r) => ACTIVE_RUN_STATUSES.includes(String(r.status || "")));
  const cooldownMs = cooldownMin * 60000;
  const recentDispatch = runs.find((r) => {
    const t = Date.parse(r.created_at || "");
    return Number.isFinite(t) && Date.now() - t < cooldownMs;
  });
  result.active_runs = activeRuns.length;
  result.recent_dispatch_run = recentDispatch ? recentDispatch.id : null;

  /* ---------------- 3. 健康：清故障 + 播报恢复 ---------------- */
  if (age < failoverMin) {
    result.action = "healthy";
    const cleared = await clearFaults(v, dryRun, [
      FAULT.HEARTBEAT,
      FAULT.STATE_READ,
      FAULT.GITHUB_API,
      FAULT.DISPATCH_FAILED,
      FAULT.NO_RECOVERY,
      FAULT.DURABLE,
    ]);
    result.recovered = cleared;
    if (cleared.length && !dryRun) {
      await sendOpsAlert(
        env,
        "✅ 调度已恢复\n" +
          "最近一次扫描：" + heartbeat.last_scan_at + "\n" +
          "心跳年龄：" + Math.round(age) + " 分钟\n" +
          "解除的故障：" + cleared.join("、"),
      );
    }
    await checkMorning(env, v, heartbeat, morningGrace, dryRun, result);
    return result;
  }

  /* ---------------- 4. 心跳过期 → 判断要不要接管 ---------------- */
  if (!dryRun) {
    const f = await faultTouch(v, FAULT.HEARTBEAT);
    result.fault_age_minutes = f.age_minutes;
  }

  // 4a. 没有 DO 就没有持久化锁 → 拒绝接管。
  //     旧实现会在这种情况下继续每分钟 dispatch（因为它根本没记住上次 dispatch 过）。
  if (!v) {
    result.action = "skip_no_durable_lock";
    result.note = "Durable Object 未绑定：没有持久化冷却锁，拒绝接管以免造成 dispatch 风暴";
    return result;
  }

  // 4b. 已经有人在跑 → 不重复 dispatch
  if (activeRuns.length > 0) {
    result.action = "skip_run_in_flight";
    result.note = "已有 " + activeRuns.length + " 条 run 排队/运行中，等它出结果";
  } else if (runsError) {
    // 4c. 拿不到 run 列表 → 不知道有没有人在跑 → 不动
    result.action = "skip_no_run_list";
    result.note = "无法确认是否已有 run，为避免重复 dispatch 本轮不接管";
  } else if (recentDispatch) {
    // 4d. GitHub 侧看到冷却窗口内刚有 run → 不重复 dispatch
    result.action = "skip_cooldown";
    result.note =
      "冷却中：最近一次 run 创建于 " +
      Math.round((Date.now() - Date.parse(recentDispatch.created_at)) / 60000) +
      " 分钟前（cooldown=" + cooldownMin + " 分钟）";
    await noteNoRecovery(env, v, dryRun, result, recentDispatch, age);
  } else {
    // 4e. 该接管了 —— 但必须先原子抢占冷却锁
    if (dryRun) {
      const peek = await v.call("/ops/cooldown", "POST", {
        now_ms: Date.now(), cooldown_ms: cooldownMs, mark: false,
      });
      if (peek.blocked) {
        result.action = "skip_cooldown";
        result.note = "冷却中（DO 锁）：剩余 " +
          Math.round((peek.remaining_ms || 0) / 60000) + " 分钟";
      } else {
        result.action = "would_dispatch";
        result.note = "心跳 " + Math.round(age) + " 分钟未更新，将补触发一次扫描";
      }
    } else {
      // 关键一步：mark=true 是原子的「检查并占用」。
      // 两个并发 cron 不可能同时拿到 blocked=false —— DO 单实例串行。
      const claim = await v.call("/ops/cooldown", "POST", {
        now_ms: Date.now(), cooldown_ms: cooldownMs, mark: true,
      });
      if (claim.blocked) {
        result.action = "skip_cooldown";
        result.note = "冷却中（DO 锁）：另一轮刚刚接管过";
      } else {
        const res = await dispatchWorkflow(env, { mode: "scan" });
        result.dispatched = res.ok;
        if (res.ok) {
          result.action = "dispatched";
        } else {
          result.action = "dispatch_failed";
          result.errors.push("dispatch: " + res.error);
          result.faults.push(FAULT.DISPATCH_FAILED);
          const a = await alertFault(env, v, FAULT.DISPATCH_FAILED, {
            title: "补触发扫描失败",
            detail: "GitHub 拒绝了 workflow_dispatch：\n" + res.error,
            action: "未接管成功，下一轮会重试；若持续失败请检查 PAT 是否过期或权限是否够 actions:write",
          });
          if (a.sent) result.alerted.push(FAULT.DISPATCH_FAILED);
        }
      }
    }
  }

  /* ---------------- 5. 严重过期 → 告警（同一故障只发一次） ---------------- */
  if (age >= alertMin) {
    result.faults.push(FAULT.HEARTBEAT);
    if (!dryRun) {
      const a = await alertFault(env, v, FAULT.HEARTBEAT, {
        title: "调度中断",
        detail:
          "心跳已 " + Math.round(age) + " 分钟未更新（阈值 " + alertMin + " 分钟）。\n" +
          "最近一次扫描：" + heartbeat.last_scan_at + "\n" +
          "本轮动作：" + result.action + (result.note ? "（" + result.note + "）" : ""),
        action: result.dispatched
          ? "已由外部看门狗补触发一次扫描，等待 state.json 心跳前进"
          : "看门狗未能接管，请检查 cron-job.org 与 PAT",
      });
      if (a.sent) result.alerted.push(FAULT.HEARTBEAT);
    }
  }

  await checkMorning(env, v, heartbeat, morningGrace, dryRun, result);
  return result;
}

/**
 * 「dispatch 成功」≠「扫描成功」。
 * 判定只看 state.json：last_scan_at 没前进，就是没恢复。
 */
async function noteNoRecovery(env, v, dryRun, result, run, age) {
  if (dryRun || !v) return;
  const status = String(run.status || "");
  const concluded = ["completed", "failure", "cancelled", "timed_out", "success"].includes(status);
  if (!concluded) return; // 还在跑，给它时间
  result.faults.push(FAULT.NO_RECOVERY);
  const a = await alertFault(env, v, FAULT.NO_RECOVERY, {
    title: "已补触发扫描，但心跳仍未恢复",
    detail:
      "补触发的 run #" + run.id + " 状态 " + status +
      "，但 state.json 的 last_scan_at 仍停在 " + Math.round(age) + " 分钟前。\n" +
      "这说明 dispatch 成功了、扫描却没能推进心跳（可能 bot.py 提前失败，或 state 提交失败）。",
    action: "看门狗继续按冷却周期重试；请到 Actions 查看该 run 的日志",
  });
  if (a.sent) result.alerted.push(FAULT.NO_RECOVERY);
}

/**
 * 早报是否超补偿窗口仍未送达（B5 第六类故障）。
 * 补偿窗口与 scripts/ensure_morning_running.py 的 RETRY_BAND_MINUTES 一致（180）。
 * 这里只观测并告警，不补发——补发是 scan job 里那个脚本的职责，两处都补会发两条。
 */
async function checkMorning(env, v, heartbeat, graceMin, dryRun, result) {
  const lastDate = heartbeat.last_morning_report_date || "";
  const cn = beijingNow();
  const today = cn.date;
  if (lastDate === today) {
    await clearFaults(v, dryRun, [FAULT.MORNING]);
    return;
  }
  const driftMin = cn.minutesSince(6, 0);
  result.morning = { last_date: lastDate, today, drift_minutes: Math.round(driftMin) };
  if (driftMin <= graceMin) return; // 还在补偿窗口内
  result.faults.push(FAULT.MORNING);
  if (dryRun || !v) return;
  const a = await alertFault(env, v, FAULT.MORNING, {
    title: "今日早报未送达，且已超出补偿窗口",
    detail:
      "今天（" + today + "）的早报没有发出。\n" +
      "补偿窗口为 06:00 起 " + graceMin + " 分钟，现已超时 " +
      Math.round(driftMin - graceMin) + " 分钟。\n" +
      "state.json 里 last_morning_report_date = " + (lastDate || "(空)"),
    action: "需要人工确认：到 Actions 手动执行 mode=morning（勾选 force 可忽略日期锁）",
  });
  if (a.sent) result.alerted.push(FAULT.MORNING);
}

/* ============================================================================
 * 五、故障记账（全部走 DO，条件 UPDATE 保证原子）
 * ==========================================================================*/

async function faultTouch(v, kind) {
  if (!v) return { age_minutes: 0, since: "" };
  const out = await v.call("/ops/fault", "POST", { kind, action: "touch" });
  const ageMs = Number(out.age_ms);
  return {
    age_minutes: Number.isFinite(ageMs) ? ageMs / 60000 : 0,
    since: out.since || "",
    alerted: !!out.alerted,
  };
}

/**
 * 同一故障只告警一次；故障持续期间不再重复。
 * 名额由 DO 的 `claim_alert` 原子抢占——这是「每分钟刷屏」被真正修掉的地方。
 */
async function alertFault(env, v, kind, info) {
  if (!v) return { sent: false, reason: "no_durable_store" };
  const claim = await v.call("/ops/fault", "POST", { kind, action: "claim_alert" });
  if (claim.should_alert !== true) {
    return { sent: false, reason: "already_alerted" };
  }
  const text =
    "🚨 越位情报局 · 运维告警\n\n" +
    "故障类型：" + info.title + "（" + kind + "）\n" +
    "首次发现：" + (claim.since || "") + "\n" +
    info.detail + "\n" +
    "接管动作：" + info.action;
  const sent = await sendOpsAlert(env, text);
  if (!sent) {
    // 抢到名额却没发出去 → 把名额还回去，否则这次故障会被永久静音
    await v.call("/ops/fault", "POST", { kind, action: "release_alert" });
  }
  return { sent, reason: sent ? "sent" : "send_failed" };
}

async function clearFaults(v, dryRun, kinds) {
  const cleared = [];
  if (!v) return cleared;
  const all = await v.call("/ops/faults", "GET");
  const byKind = new Map((all.faults || []).map((f) => [f.kind, f]));
  for (const kind of kinds) {
    const rec = byKind.get(kind);
    if (!rec) continue;
    cleared.push(kind + (rec.alerted ? "(已告警)" : ""));
    if (!dryRun) await v.call("/ops/fault", "POST", { kind, action: "clear" });
  }
  return cleared;
}

/* ============================================================================
 * 六、GitHub / Telegram 调用
 * ==========================================================================*/

async function readHeartbeat(env, repo) {
  let res;
  try {
    res = await fetch(API + "/repos/" + repo + "/contents/state.json", {
      headers: {
        authorization: "Bearer " + env.GITHUB_TOKEN,
        accept: "application/vnd.github.raw",
        "user-agent": "yuewei-oversight",
      },
    });
  } catch (err) {
    const e = new Error("网络错误：" + (err && err.message ? err.message : String(err)));
    e.kind = "network";
    throw e;
  }
  if (!res.ok) throw new Error("HTTP " + res.status + "（检查 PAT 是否过期/是否有 contents:read）");
  const text = await res.text();
  let state;
  try {
    state = JSON.parse(text);
  } catch (err) {
    throw new Error("state.json 不是合法 JSON");
  }
  const raw = state.last_scan_at;
  if (!raw) throw new Error("state.json 里没有 last_scan_at");
  const ts = Date.parse(raw);
  if (Number.isNaN(ts)) throw new Error("last_scan_at 无法解析：" + raw);
  return {
    last_scan_at: raw,
    age_minutes: (Date.now() - ts) / 60000,
    last_morning_report_date: state.last_morning_report_date || "",
  };
}

async function listRuns(env, repo, workflow) {
  const res = await fetch(
    API + "/repos/" + repo + "/actions/workflows/" + workflow + "/runs?per_page=30",
    {
      headers: {
        authorization: "Bearer " + env.GITHUB_TOKEN,
        accept: "application/vnd.github+json",
        "user-agent": "yuewei-oversight",
      },
    },
  );
  if (!res.ok) throw new Error("HTTP " + res.status);
  const body = await res.json();
  return body.workflow_runs || [];
}

/**
 * 触发一次 workflow。返回 {ok, error}。
 * 204 只代表「GitHub 接受了这次 dispatch」，不代表扫描会成功。
 */
async function dispatchWorkflow(env, inputs) {
  const repo = str(env.REPO) || "markmilk20020610-art/premier-league-bot";
  const workflow = str(env.WORKFLOW_FILE) || "scanner.yml";
  const ref = str(env.DISPATCH_REF) || "main";
  if (!env.GITHUB_TOKEN) return { ok: false, error: "GITHUB_TOKEN 未配置" };
  try {
    const res = await fetch(
      API + "/repos/" + repo + "/actions/workflows/" + workflow + "/dispatches",
      {
        method: "POST",
        headers: {
          authorization: "Bearer " + env.GITHUB_TOKEN,
          accept: "application/vnd.github+json",
          "content-type": "application/json",
          "user-agent": "yuewei-oversight",
        },
        body: JSON.stringify({ ref, inputs }),
      },
    );
    if (res.status === 204 || res.ok) return { ok: true, error: "" };
    const text = await res.text();
    return { ok: false, error: "HTTP " + res.status + " " + text.slice(0, 300) };
  } catch (err) {
    return { ok: false, error: "网络错误：" + (err && err.message ? err.message : String(err)) };
  }
}

/**
 * 发运维告警。**只发私有运维群**。
 *
 * 刻意不回落到 TELEGRAM_CHAT_ID：那是公共足球情报频道，
 * 运维噪声发进去会污染内容，也会向订阅者泄露内部故障细节。
 * 没配运维群时返回 false，并在响应里体现（alert_skipped），不静默假装发过了。
 */
async function sendOpsAlert(env, text) {
  const chat = str(env.TELEGRAM_OPS_CHAT_ID);
  if (!env.TELEGRAM_BOT_TOKEN || !chat) return false;
  try {
    const res = await fetch(TG + "/bot" + env.TELEGRAM_BOT_TOKEN + "/sendMessage", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        chat_id: chat,
        text,
        // 刻意不用 parse_mode：故障详情里含下划线、星号、反引号等字符，
        // Markdown 解析失败会让整条告警 400 丢掉——运维告警宁丑勿丢。
        disable_web_page_preview: true,
      }),
    });
    if (!res.ok) return false;
    const body = await res.json().catch(() => ({}));
    // Telegram 会 200 + {"ok": false}，只看 HTTP 状态码不够
    return body && body.ok === true;
  } catch (err) {
    return false;
  }
}

/* ============================================================================
 * 七、工具函数
 * ==========================================================================*/

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj, null, 2), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

function str(v) {
  return v == null ? "" : String(v).trim();
}

function num(v, dflt) {
  const n = Number(v);
  return Number.isFinite(n) && n > 0 ? n : dflt;
}

function round1(n) {
  return Math.round(n * 10) / 10;
}

/** 北京时间（UTC+8，无夏令时） */
function beijingNow() {
  const now = new Date(Date.now() + 8 * 3600 * 1000);
  const y = now.getUTCFullYear();
  const m = String(now.getUTCMonth() + 1).padStart(2, "0");
  const d = String(now.getUTCDate()).padStart(2, "0");
  const hh = now.getUTCHours();
  const mm = now.getUTCMinutes();
  return {
    date: y + "-" + m + "-" + d,
    time: String(hh).padStart(2, "0") + ":" + String(mm).padStart(2, "0"),
    minutesSince(hour, minute) {
      return hh * 60 + mm - (hour * 60 + minute);
    },
  };
}
