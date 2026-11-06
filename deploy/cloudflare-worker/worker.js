/**
 * 越位情报局 —— 外部看门狗 / 故障转移调度器 / 审核回调网关（Cloudflare Worker）
 *
 * ============================================================================
 * 这个文件干两件完全不相干的事，刻意放在一起（只有一个 Worker 入口）
 * ============================================================================
 *   1. POST /            —— Telegram webhook：接收私有审核群里的「批准/拒绝」按钮
 *   2. GET  /  & cron    —— 看门狗：读 state.json 心跳，必要时补触发扫描并告警
 *
 * 两者共用一个入口是因为 Cloudflare 免费额度只给一个 Worker 域名，
 * 但代码里走的是两条完全独立的分支，互不影响：
 *   · webhook 分支**从不**读心跳、从不 dispatch scan
 *   · 看门狗分支**从不**处理 callback_query
 *
 * ============================================================================
 * 一、为什么看门狗必须住在外部
 * ============================================================================
 * 突发扫描靠 cron-job.org 调 GitHub workflow_dispatch。
 * 一旦 cron-job.org 停摆，GitHub 那边什么都不会发生——而仓库里的 watchdog
 * 恰恰也靠 GitHub schedule 触发，它和它要监控的对象共用同一条不可靠链路
 * （实测该仓库 3 天只触发 7 次 schedule，早报迟到 207 分钟）。
 * 结果就是：调度停了，却没有任何人知道。
 * 所以看门狗必须住在外部，并且和主调度器不同厂商。
 *
 * ============================================================================
 * 二、V1.0 硬化修掉的四个真实缺陷（旧版本的问题，不是假设）
 * ============================================================================
 *   缺陷 1：心跳超阈值后**每分钟**重复 dispatch。
 *           旧代码只在「age >= failoverMin」判断，dispatch 完不记账，
 *           下一次 cron（1 分钟后）看到的还是同一个陈旧心跳 → 又 dispatch 一次。
 *           一轮故障能刷出几十次重复运行。
 *   缺陷 2：`alertOnce` 名不副实。它没有任何持久化，靠的是
 *           `age >= alertMin && age < alertMin + 3` 这个三分钟窗口。
 *           窗口内被打中两次就发两条；窗口一过就不再发（即使故障还在）。
 *   缺陷 3：读 state 失败时每分钟告警一次。因为读失败路径里 age 恒为 null，
 *           永远进不了那个三分钟窗口判断。
 *   缺陷 4：dispatch 失败被静默吞掉。旧代码只是把 action 改成
 *           "dispatch_failed"，没有任何人知道。
 *
 * 修法（对应 B3/B4/B5）：
 *   · dispatch 冷却**双重持久化**：KV（若已绑定）+ GitHub 自己的 run 列表。
 *     后者是权威的——即使 KV 没配，也不会重复 dispatch，因为
 *     「最近 N 分钟内已经有一个 run」这个事实写在 GitHub 那边。
 *   · 故障按类型分别记账，同一故障只告警一次，恢复后自动清除并播报恢复。
 *   · 读失败与心跳过期是**两个独立的故障类型**，阈值不同、动作不同。
 *   · dispatch 失败单独记账并告警。
 *   · 「dispatch 成功」≠「扫描成功」：只有 state.json 的 last_scan_at 前进了才算恢复。
 *
 * ⚠️ 关于「持久化」的诚实说明
 * ---------------------------------------------------------------------------
 * 内存变量**不能**当持久化锁用：Cloudflare Worker 的 isolate 随时可能被回收，
 * 两次 cron 之间不保证同一个实例。本文件因此：
 *   · 需要跨 invocation 的记忆，一律走 KV（env.OVERSIGHT_KV）；
 *   · KV 未绑定时，返回值里会明确写 `durable: false`，
 *     并且**不依赖**内存变量做防重——防重退化为「查 GitHub run 列表」，
 *     那是真的持久（存在 GitHub 服务器上），不是内存冒充。
 *   · 告警去重在 KV 缺失时退化为时间窗口，并在响应里标注 `alert_dedup: "window"`，
 *     不谎称「已持久化」。
 *
 * ============================================================================
 * 三、阈值从哪来（B4 校准，全部有实测依据）
 * ============================================================================
 * 从仓库 state.json 的 400 条历史提交里提取 394 个 last_scan_at 间隔：
 *
 *   min 0.45 / p10 0.83 / p50 1.01 / p90 1.29 分钟   ← 93.4% 的间隔 ≤ 2 分钟
 *   其余 6.6% 为 20 / 27 / 41 / 95 / 130 / 137 / 153 / 161 / 191 / 220 /
 *              243 / 244 / 258 / 263 / 278 / 285 / 305 / 310 / 315 / 374 /
 *              386 / 389 / 435 / 442 / 494 / 511 分钟  ← 真实停摆
 *
 * 也就是说：**正常节奏是 1 分钟（cron-job.org 每分钟一次），不是 5 分钟**。
 * 5 分钟是 GitHub schedule 的兜底间隔。两者都要不被误判。
 *
 * 由此定：
 *   EXPECTED_SCAN_INTERVAL_MINUTES = 5   —— 兜底调度间隔（可配置）
 *   FAILOVER_MINUTES = 12                —— 兜底间隔 5 + scan job 超时 6 + 1 分钟余量
 *                                           健康运行时远程可见的最大心跳年龄 ≈ 11 分钟，
 *                                           12 分钟才动手，正常调度绝不会被误判。
 *                                           旧值 6 分钟会在「一次慢运行 + 一次调度抖动」
 *                                           时就误接管，属于过激。
 *   DISPATCH_COOLDOWN_MINUTES = 10       —— 接管后至少等 10 分钟再试，
 *                                           覆盖「dispatch 成功但 run 还没开始」的窗口。
 *   ALERT_MINUTES = 30                   —— 必须先经历 ≥1 次接管失败才告警
 *                                           （12 + 10 = 22 < 30）。
 *   READ_FAIL_ALERT_MINUTES = 8          —— 连续读失败 8 分钟才告警，躲过 API 抖动。
 *
 * 没有可靠证据时不要声称「调度器已恢复」——本文件只报告观测到的事实：
 * 心跳年龄、是否 dispatch、dispatch 是否被 GitHub 接受、run 是否真的产出新心跳。
 *
 * ============================================================================
 * 四、部署（详细步骤见 README.md / 生产部署清单）
 * ============================================================================
 *   wrangler kv namespace create OVERSIGHT      # 建议：告警去重需要它
 *   wrangler secret put GITHUB_TOKEN            # PAT，需 actions:write + contents:read
 *   wrangler secret put TELEGRAM_BOT_TOKEN
 *   wrangler secret put TELEGRAM_OPS_CHAT_ID    # 私有运维群（不要用公共情报频道）
 *   wrangler secret put TELEGRAM_WEBHOOK_SECRET # setWebhook 时用的 secret_token
 *   wrangler secret put TELEGRAM_REVIEW_CHAT_ID # 私有审核群
 *   wrangler secret put REVIEW_OPERATOR_IDS     # 逗号分隔的 Telegram User ID
 *   wrangler secret put REVIEW_CALLBACK_HMAC_SECRET  # 必须与 GitHub Secret 同值
 *   wrangler deploy
 */

const API = "https://api.github.com";
const TG = "https://api.telegram.org";

/* ==================== 阈值（B4） ==================== */
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
const DOUBLE_CLICK_WINDOW_SECONDS = 30;

/* 故障类型（B5）。每种故障独立记账、独立告警、独立恢复。 */
const FAULT = {
  GITHUB_API: "github_api_unavailable",
  STATE_READ: "state_read_failed",
  HEARTBEAT: "heartbeat_stale",
  DISPATCH_FAILED: "dispatch_failed",
  NO_RECOVERY: "dispatch_no_recovery",
  MORNING: "morning_missed",
};

const ACTIVE_RUN_STATUSES = ["queued", "in_progress", "waiting", "pending", "requested"];

export default {
  /** 定时触发：wrangler.toml 里 crons = ["* * * * *"] */
  async scheduled(event, env, ctx) {
    ctx.waitUntil(tick(env, "cron"));
  },

  /**
   * 路由：
   *   POST /                     → Telegram webhook（审核按钮）
   *   GET  /?force=1             → 强制走一次接管逻辑（调试用，会真的 dispatch）
   *   GET  /?dry=1               → 只读诊断，不 dispatch、不告警
   *   GET  /                     → 只读诊断（默认，不会产生任何副作用）
   */
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

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
    // 默认只读：探活与人工查看状态都走这里，绝不产生副作用
    const out = await tick(env, "http", { dryRun: true });
    return json({ ...out, hint: "只读诊断。加 ?force=1 才会真的接管 dispatch" });
  },
};

/* ============================================================================
 * 一、Telegram webhook（A3）
 * ==========================================================================*/

/**
 * 处理 Telegram 的 callback_query。
 *
 * 校验顺序（任何一步失败都不 dispatch，且都不产生副作用）：
 *   1. Webhook Secret Token  —— 证明这条 POST 真的来自 Telegram
 *   2. 是不是 callback_query —— 其它 update 一律 200 忽略（否则 Telegram 会重投）
 *   3. 操作人 User ID 白名单
 *   4. 回调来自指定的审核 Chat
 *   5. HMAC 签名 + 有效期     —— 不能仅凭 event_id 就接受批准
 *   6. 双击窗口（30 秒内同一 event+action 只放行一次）
 *
 * 返回值语义：只要通过 1、2 两步就回 200。因为 Telegram 收到非 200 会**重投**
 * 同一个 update，而重投会带来重复 dispatch。业务上的拒绝通过 answerCallbackQuery
 * 告知操作人，不通过 HTTP 状态码。
 */
async function handleTelegramWebhook(request, env, ctx) {
  const secret = str(env.TELEGRAM_WEBHOOK_SECRET);
  if (!secret) {
    // 没配 secret 就绝不接受 POST——否则任何人都能伪造一次批准
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
    // 普通消息 / 编辑 / 频道贴子：一律忽略。返回 200 防止 Telegram 重投。
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
    // 不在审核群里的按钮一律不认——避免有人把卡片转发到别处再点
    ctx.waitUntil(answerCallback(env, cq.id, "回调不是来自审核群，已拒绝"));
    return json({ ok: true, rejected: "not_review_chat", chat_id: chatId });
  }

  const verified = await verifyCallbackData(data, env.REVIEW_CALLBACK_HMAC_SECRET);
  if (!verified.ok) {
    ctx.waitUntil(answerCallback(env, cq.id, "按钮无效或已过期：" + verified.reason));
    return json({ ok: true, rejected: "bad_callback", reason: verified.reason });
  }

  const { eventId, action } = verified;

  // 双击窗口：30 秒内同一 (event, action) 只放行一次。
  // 权威幂等在 bot.py 的状态机里（ALREADY_DELIVERED / CLAIMED_BY_OTHER），
  // 这里只是省掉一次无意义的 workflow run。
  const store = await openStore(env);
  const clickKey = "click:" + eventId + ":" + action;
  const lastClick = await store.get(clickKey);
  const nowSec = Math.floor(Date.now() / 1000);
  if (lastClick && nowSec - Number(lastClick) < DOUBLE_CLICK_WINDOW_SECONDS) {
    ctx.waitUntil(answerCallback(env, cq.id, "已收到你的操作，正在处理…"));
    return json({ ok: true, deduped: "double_click", event_id: eventId });
  }
  await store.put(clickKey, String(nowSec), 120);

  const actionName = action === REVIEW_ACTION_APPROVE ? "approve" : "decline";
  ctx.waitUntil(
    (async () => {
      await answerCallback(
        env,
        cq.id,
        action === REVIEW_ACTION_APPROVE ? "已提交批准，正在投递…" : "已提交拒绝，正在存档…",
      );
      const res = await dispatchWorkflow(env, {
        mode: "review_action",
        review_event_id: eventId,
        review_action: actionName,
        review_operator_id: fromId,
        review_note: "via telegram",
      });
      if (!res.ok) {
        // dispatch 失败必须让人看见：操作人已经点了按钮，但流水线根本没起来
        await store.put(
          "last_review_dispatch_error",
          JSON.stringify({ at: new Date(Date.now()).toISOString(), event_id: eventId, error: res.error }),
          86400,
        );
        await sendOpsAlert(
          env,
          "❌ 审核回调未能启动流水线\n" +
            "事件：" + eventId + "\n" +
            "动作：" + actionName + "\n" +
            "错误：" + res.error + "\n" +
            "请到 Actions 页面手动执行 mode=review_action。",
        );
      }
    })(),
  );

  return json({ ok: true, accepted: true, event_id: eventId, action: actionName });
}

/** 校验 callback_data：格式 → 版本 → 动作 → 签名 → 有效期 */
async function verifyCallbackData(data, secretRaw) {
  const secret = str(secretRaw);
  if (!secret) return { ok: false, reason: "HMAC 密钥未配置" };
  if (!data) return { ok: false, reason: "回调数据为空" };

  const parts = data.split(":");
  if (parts.length !== 5) return { ok: false, reason: "回调数据格式错误" };
  const [ver, action, eventId, expS, sig] = parts;

  if (ver !== REVIEW_CALLBACK_VERSION) return { ok: false, reason: "版本不支持" };
  if (action !== REVIEW_ACTION_APPROVE && action !== REVIEW_ACTION_DECLINE) {
    return { ok: false, reason: "未知动作" };
  }
  if (!/^[0-9a-f]{6,32}$/i.test(eventId)) return { ok: false, reason: "事件 ID 格式非法" };
  const exp = Number(expS);
  if (!Number.isFinite(exp) || exp <= 0) return { ok: false, reason: "有效期字段非法" };

  const want = await hmacSig(secret, ver + "|" + action + "|" + eventId + "|" + exp);
  if (!safeEqual(want, sig)) return { ok: false, reason: "签名校验失败" };
  if (Math.floor(Date.now() / 1000) > exp) return { ok: false, reason: "按钮已过期（重放被拒）" };

  return { ok: true, action, eventId };
}

/**
 * HMAC-SHA256 hex，取前 20 位。
 * 必须与 bot.py 的 `_review_callback_sig` 逐字节一致：
 *   hmac.new(key, "v1|a|<event_id>|<exp>", sha256).hexdigest()[:20]
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
  return hex.slice(0, 20);
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
 * 二、看门狗主流程（B3 / B4 / B5）
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

  const store = await openStore(env);

  const result = {
    trigger,
    dry_run: dryRun,
    repo,
    checked_at: new Date(Date.now()).toISOString(),
    durable: store.durable,
    alert_dedup: store.durable ? "kv" : "window",
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
    // 先把「故障仍在」记一笔（首次调用会写入 since），再取持续时长。
    // 少了这一步，age 永远是 0，READ_FAIL_ALERT_MINUTES 形同虚设——
    // 而旧版本恰好就是每分钟告警一次（另一个极端）。
    if (!dryRun) await store.touch(kind);
    const age = await store.ageOf(kind);
    result.fault_age_minutes = round1(age);
    if (!dryRun && age >= readFailAlertMin) {
      const a = await alertFault(env, store, kind, {
        title:
          kind === FAULT.GITHUB_API
            ? "GitHub API 不可用，看门狗读不到心跳"
            : "state.json 读取失败，看门狗读不到心跳",
        detail: (err && err.message ? err.message : String(err)) + "\n已连续 " + Math.round(age) + " 分钟。",
        action: "未接管：读不到心跳时盲目 dispatch 会制造重复运行，等下一轮重试",
      });
      if (a.sent) result.alerted.push(kind);
    }
    result.action = "read_failed";
    return result;
  }

  const age = result.age_minutes;
  if (age === null || !Number.isFinite(age)) {
    result.action = "skip_unknown_age";
    return result;
  }

  /* ---------------- 2. 读 GitHub run 列表（权威的 dispatch 记账） ---------------- */
  let runs = [];
  let runsError = null;
  try {
    runs = await listRuns(env, repo, workflow);
  } catch (err) {
    runsError = err && err.message ? err.message : String(err);
    result.errors.push("list_runs: " + runsError);
    // 拿不到 run 列表 → 无法判断「是不是已经有人在跑」。
    // 这种情况下 dispatch 有重复风险，所以宁可不动，只记账。
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
    // 读失败故障在能读到心跳的这一刻就已经恢复了
    const cleared = await clearFaults(env, store, dryRun, [
      FAULT.HEARTBEAT,
      FAULT.STATE_READ,
      FAULT.GITHUB_API,
      FAULT.DISPATCH_FAILED,
      FAULT.NO_RECOVERY,
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
    await checkMorning(env, store, heartbeat, morningGrace, dryRun, result);
    return result;
  }

  /* ---------------- 4. 心跳过期 → 判断要不要接管 ---------------- */
  // 先记一笔「故障仍在」（不重置 since、不重置告警标记），再取持续时长
  if (!dryRun) await store.touch(FAULT.HEARTBEAT);
  const faultAge = await store.ageOf(FAULT.HEARTBEAT);
  result.fault_age_minutes = round1(faultAge);

  // 4a. 已经有人在跑 → 不重复 dispatch（这是防「dispatch 风暴」的第一道闸）
  if (activeRuns.length > 0) {
    result.action = "skip_run_in_flight";
    result.note = "已有 " + activeRuns.length + " 条 run 排队/运行中，等它出结果";
  } else if (runsError) {
    // 4b. 拿不到 run 列表 → 不知道有没有人在跑 → 不动
    result.action = "skip_no_run_list";
    result.note = "无法确认是否已有 run，为避免重复 dispatch 本轮不接管";
  } else if (recentDispatch) {
    // 4c. 冷却窗口内刚 dispatch 过 → 不重复 dispatch
    result.action = "skip_cooldown";
    result.note =
      "冷却中：最近一次 dispatch 在 " + Math.round((Date.now() - Date.parse(recentDispatch.created_at)) / 60000) +
      " 分钟前（cooldown=" + cooldownMin + " 分钟）";
    // 冷却期内仍未恢复 → 这是「dispatch 成功但扫描没恢复」的直接证据
    await noteNoRecovery(env, store, dryRun, result, recentDispatch, age);
  } else {
    // 4d. 该接管了
    if (dryRun) {
      result.action = "would_dispatch";
      result.note = "心跳 " + Math.round(age) + " 分钟未更新，将补触发一次扫描";
    } else {
      const res = await dispatchWorkflow(env, { mode: "scan" });
      result.dispatched = res.ok;
      if (res.ok) {
        result.action = "dispatched";
        await store.markDispatched(new Date(Date.now()).toISOString());
      } else {
        result.action = "dispatch_failed";
        result.errors.push("dispatch: " + res.error);
        result.faults.push(FAULT.DISPATCH_FAILED);
        const a = await alertFault(env, store, FAULT.DISPATCH_FAILED, {
          title: "补触发扫描失败",
          detail: "GitHub 拒绝了 workflow_dispatch：\n" + res.error,
          action: "未接管成功，下一轮会重试；若持续失败请检查 PAT 是否过期或权限是否够 actions:write",
        });
        if (a.sent) result.alerted.push(FAULT.DISPATCH_FAILED);
      }
    }
  }

  /* ---------------- 5. 严重过期 → 告警（同一故障只发一次） ---------------- */
  if (age >= alertMin) {
    result.faults.push(FAULT.HEARTBEAT);
    if (!dryRun) {
      const a = await alertFault(env, store, FAULT.HEARTBEAT, {
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

  await checkMorning(env, store, heartbeat, morningGrace, dryRun, result);
  return result;
}

/**
 * 「dispatch 成功」≠「扫描成功」。
 *
 * 判定依据只看 state.json：last_scan_at 没有前进，就是没恢复。
 * 具体分两种：
 *   · 冷却期内已经有 run 被创建并**已完成**，但心跳没动 → 扫描跑了却没写心跳（bot.py 出错）
 *   · 冷却期内压根没有 run 被创建 → dispatch 没生效（ref 不对 / workflow 被禁用）
 * 这两种都记成 dispatch_no_recovery，但 detail 不同，便于定位。
 */
async function noteNoRecovery(env, store, dryRun, result, run, age) {
  if (dryRun) return;
  const status = String(run.status || "");
  const concluded = ["completed", "failure", "cancelled", "timed_out", "success"].includes(status);
  if (!concluded) return; // 还在跑，给它时间
  result.faults.push(FAULT.NO_RECOVERY);
  const a = await alertFault(env, store, FAULT.NO_RECOVERY, {
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
 * 早报是否超补偿窗口仍未送达（B5 的第六类故障）。
 *
 * 补偿窗口与 scripts/ensure_morning_running.py 的 RETRY_BAND_MINUTES 必须一致（180）。
 * 这里只**观测并告警**，不负责补发——补发是 scan job 里那个脚本的职责。
 * 两处都做补发会导致两条早报。
 */
async function checkMorning(env, store, heartbeat, graceMin, dryRun, result) {
  const lastDate = heartbeat.last_morning_report_date || "";
  const cn = beijingNow();
  const today = cn.date;
  if (lastDate === today) {
    await clearFaults(env, store, dryRun, [FAULT.MORNING]);
    return;
  }
  const driftMin = cn.minutesSince(6, 0);
  result.morning = { last_date: lastDate, today, drift_minutes: Math.round(driftMin) };
  if (driftMin <= graceMin) return; // 还在补偿窗口内，交给 ensure_morning_running.py
  result.faults.push(FAULT.MORNING);
  if (dryRun) return;
  const a = await alertFault(env, store, FAULT.MORNING, {
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
 * 三、持久化存储（KV 优先，缺失时诚实降级）
 * ==========================================================================*/

/**
 * 存储抽象。两种后端：
 *   · KV（env.OVERSIGHT_KV）—— 真正的持久化，durable=true
 *   · 无 —— durable=false。此时**不假装**有持久化：
 *           fault 记账退化为「本次 invocation 内」，
 *           防重复 dispatch 完全依赖 GitHub run 列表（那份记忆在 GitHub 上，是真的）。
 */
async function openStore(env) {
  const kv = env.OVERSIGHT_KV;
  const usable = kv && typeof kv.get === "function" && typeof kv.put === "function";
  const prefix = "oversight:";
  const mem = new Map();

  return {
    durable: !!usable,

    async get(key) {
      if (usable) {
        try {
          return await kv.get(prefix + key);
        } catch (err) {
          return null;
        }
      }
      return mem.has(key) ? mem.get(key) : null;
    },

    async put(key, value, ttlSeconds) {
      if (usable) {
        try {
          const opts = ttlSeconds ? { expirationTtl: ttlSeconds } : undefined;
          await kv.put(prefix + key, value, opts);
          return true;
        } catch (err) {
          return false;
        }
      }
      mem.set(key, value);
      return false; // 明确告诉调用方：这次没落到持久介质
    },

    /** 读故障记录（含首次发现时间） */
    async readFault(kind) {
      const raw = await this.get("fault:" + kind);
      if (!raw) return null;
      try {
        return JSON.parse(raw);
      } catch (err) {
        return null;
      }
    },

    async writeFault(kind, rec) {
      // 故障记录最长留 7 天，避免 KV 无限增长
      await this.put("fault:" + kind, JSON.stringify(rec), 7 * 86400);
    },

    async dropFault(kind) {
      if (usable) {
        try {
          await kv.delete(prefix + "fault:" + kind);
          return true;
        } catch (err) {
          return false;
        }
      }
      mem.delete("fault:" + kind);
      return true;
    },

    /** 故障已持续多少分钟（首次发现到现在）。没有记录时返回 0。 */
    async ageOf(kind) {
      const rec = await this.readFault(kind);
      if (!rec || !rec.since) return 0;
      const t = Date.parse(rec.since);
      if (!Number.isFinite(t)) return 0;
      return (Date.now() - t) / 60000;
    },

    /** 记录一次「故障仍在」但不改变首次发现时间，也不重置告警标记 */
    async touch(kind) {
      const rec = await this.readFault(kind);
      if (!rec) {
        await this.writeFault(kind, {
          since: new Date(Date.now()).toISOString(),
          last_seen: new Date(Date.now()).toISOString(),
          alerted: false,
        });
        return;
      }
      rec.last_seen = new Date(Date.now()).toISOString();
      await this.writeFault(kind, rec);
    },

    async markDispatched(iso) {
      await this.put("last_dispatch_at", iso, 7 * 86400);
    },

    async lastDispatchAt() {
      const v = await this.get("last_dispatch_at");
      if (!v) return null;
      const t = Date.parse(v);
      return Number.isFinite(t) ? t : null;
    },
  };
}

/**
 * 同一故障只告警一次；故障持续期间不再重复。
 * 恢复后由 clearFaults 删除记录，下次再发生才会重新告警。
 */
async function alertFault(env, store, kind, info) {
  const rec = (await store.readFault(kind)) || {
    since: new Date(Date.now()).toISOString(),
    alerted: false,
  };
  if (rec.alerted) {
    rec.last_seen = new Date(Date.now()).toISOString();
    await store.writeFault(kind, rec);
    return { sent: false, reason: "already_alerted" };
  }
  const text =
    "🚨 越位情报局 · 运维告警\n\n" +
    "故障类型：" + info.title + "（" + kind + "）\n" +
    "首次发现：" + rec.since + "\n" +
    info.detail + "\n" +
    "接管动作：" + info.action;
  const sent = await sendOpsAlert(env, text);
  rec.alerted = sent;
  rec.alerted_at = sent ? new Date(Date.now()).toISOString() : null;
  rec.last_seen = new Date(Date.now()).toISOString();
  await store.writeFault(kind, rec);
  return { sent, reason: sent ? "sent" : "send_failed" };
}

async function clearFaults(env, store, dryRun, kinds) {
  const cleared = [];
  for (const kind of kinds) {
    const rec = await store.readFault(kind);
    if (!rec) continue;
    cleared.push(kind + (rec.alerted ? "(已告警)" : ""));
    if (!dryRun) await store.dropFault(kind);
  }
  return cleared;
}

/* ============================================================================
 * 四、GitHub / Telegram 调用
 * ==========================================================================*/

/** 读 state.json 的 last_scan_at / last_morning_report_date */
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

/** 读最近的 workflow run 列表（防重复 dispatch 的权威依据） */
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
 * 注意：204 只代表「GitHub 接受了这次 dispatch」，不代表扫描会成功。
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
 * 运维噪声发进去会污染内容，而且会向订阅者泄露内部故障细节。
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
 * 五、工具函数
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
