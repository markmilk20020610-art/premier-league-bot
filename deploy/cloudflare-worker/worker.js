/**
 * 越位情报局 —— 外部看门狗 + 故障转移调度器（Cloudflare Worker）
 *
 * ============================================================================
 * 为什么需要这个东西
 * ============================================================================
 * 现状：突发扫描靠 cron-job.org 调 GitHub workflow_dispatch。
 * 问题：cron-job.org 一旦停摆，GitHub 那边**什么都不会发生**——
 *       而仓库里的 watchdog 恰恰也是靠 GitHub 的 schedule 触发的，
 *       它和它要监控的对象共用同一条不可靠的链路（实测 GitHub schedule
 *       在这个仓库 3 天只触发 7 次，早报迟到 207 分钟）。
 *       结果就是：调度停了，却没有任何人知道。
 *
 * 所以看门狗必须住在**外部**，并且和主调度器**不同厂商**。
 * 这个 Worker 每 1 分钟醒一次，读 state.json 的 last_scan_at：
 *
 *   心跳新鲜（< FAILOVER_MINUTES） → 什么都不做（主调度器正常）
 *   心跳过期（>= FAILOVER_MINUTES） → 主动 dispatch 一次，把流水线拉起来
 *   心跳严重过期（>= ALERT_MINUTES）→ 额外发一条 Telegram 告警
 *
 * 为什么是「按需 dispatch」而不是「每分钟都 dispatch」：
 * 后者会让每轮跑两次（浪费且无意义）。按心跳缺口判断，天然实现主备切换，
 * 而且主调度器恢复后 Worker 会自动闭嘴——不需要任何人工干预。
 *
 * ============================================================================
 * 幂等性说明（为什么多重调度不会重复推送）
 * ============================================================================
 * 即使 cron-job.org 与 Worker 同时触发，也不会重复发新闻：
 *   · workflow 级 concurrency 会把两个 run 串行化；
 *   · bot.py 的 state 里有 processed_urls / news_pool / events 去重，
 *     第二轮会发现「没有新内容」，直接 0 推送退出。
 * 这是本项目早就有的事实，不是本文件新增的假设。
 *
 * ============================================================================
 * 部署（约 5 分钟，免费额度足够）
 * ============================================================================
 *   npm i -g wrangler && wrangler login
 *   wrangler kv:namespace create OVERSIGHT          # 可选，用于告警去重
 *   wrangler secret put GITHUB_TOKEN                # 需要 actions:write 权限的 PAT
 *   wrangler secret put TELEGRAM_BOT_TOKEN
 *   wrangler secret put TELEGRAM_CHAT_ID
 *   wrangler deploy
 * 详细步骤见同目录 README.md。
 */

const API = "https://api.github.com";

/** 主调度器每 1 分钟一次；超过这个分钟数没有心跳，就认为它挂了 */
const DEFAULT_FAILOVER_MINUTES = 6;
/** 超过这个分钟数没有心跳，就额外发 Telegram 告警 */
const DEFAULT_ALERT_MINUTES = 20;

export default {
  /** 定时触发：wrangler.toml 里 crons = ["* * * * *"] */
  async scheduled(event, env, ctx) {
    ctx.waitUntil(tick(env, "cron"));
  },

  /** 手动/探活请求：GET / 返回 JSON 状态，GET /?force=1 强制 dispatch */
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.searchParams.get("force") === "1") {
      const out = await tick(env, "manual-force");
      return json({ ...out, forced: true });
    }
    const out = await tick(env, "http", { dryRun: true });
    return json(out);
  },
};

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj, null, 2), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

/** 主流程：读心跳 → 判断 → 必要时 dispatch / 告警 */
async function tick(env, trigger, opts = {}) {
  const repo = env.REPO || "markmilk20020610-art/premier-league-bot";
  const workflow = env.WORKFLOW_FILE || "scanner.yml";
  const failoverMin = num(env.FAILOVER_MINUTES, DEFAULT_FAILOVER_MINUTES);
  const alertMin = num(env.ALERT_MINUTES, DEFAULT_ALERT_MINUTES);

  const result = {
    trigger,
    repo,
    checked_at: new Date().toISOString(),
    last_scan_at: null,
    age_minutes: null,
    action: "none",
    alerted: false,
    errors: [],
  };

  // ---- 1. 读心跳 ----
  let heartbeat;
  try {
    heartbeat = await readHeartbeat(env, repo);
    result.last_scan_at = heartbeat.last_scan_at;
    result.age_minutes = heartbeat.age_minutes;
  } catch (err) {
    result.errors.push(`read_state: ${err.message}`);
    // 读不到心跳 ≠ 流水线停了（可能是 token 过期/接口抖动）。
    // 此时**不 dispatch**，只告警，避免在故障时制造更多噪声。
    if (!opts.dryRun) {
      result.alerted = await alertOnce(env, `看门狗读不到 state.json：${err.message}`);
    }
    return result;
  }

  const age = result.age_minutes;
  if (age === null) {
    result.action = "skip_unknown_age";
    return result;
  }

  // ---- 2. 主调度器正常，收工 ----
  if (age < failoverMin) {
    result.action = "healthy";
    return result;
  }

  // ---- 3. 心跳过期 → 接管 ----
  if (opts.dryRun) {
    result.action = age >= alertMin ? "would_dispatch_and_alert" : "would_dispatch";
    return result;
  }

  try {
    await dispatch(env, repo, workflow);
    result.action = "dispatched";
  } catch (err) {
    result.errors.push(`dispatch: ${err.message}`);
    result.action = "dispatch_failed";
  }

  // ---- 4. 严重过期 → 告警（只在刚跨过阈值时发一次，避免每分钟刷屏）----
  if (age >= alertMin && age < alertMin + 3) {
    result.alerted = await alertOnce(
      env,
      `⚠️ 调度中断约 ${Math.round(age)} 分钟\n` +
        `最近一次扫描：${heartbeat.last_scan_at}\n` +
        `已由外部看门狗（Cloudflare Worker）自动接管并补触发。\n` +
        `请检查 cron-job.org 是否停摆。`,
    );
  }

  return result;
}

/** 读 state.json 的 last_scan_at 并算出距今多少分钟 */
async function readHeartbeat(env, repo) {
  const res = await fetch(`${API}/repos/${repo}/contents/state.json`, {
    headers: {
      authorization: `Bearer ${env.GITHUB_TOKEN}`,
      accept: "application/vnd.github.raw",
      "user-agent": "yuewei-oversight",
    },
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
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
  if (Number.isNaN(ts)) throw new Error(`last_scan_at 无法解析：${raw}`);
  return {
    last_scan_at: raw,
    age_minutes: (Date.now() - ts) / 60000,
  };
}

/** 触发一次扫描（mode=scan，与 cron-job.org 的做法一致） */
async function dispatch(env, repo, workflow) {
  const res = await fetch(
    `${API}/repos/${repo}/actions/workflows/${workflow}/dispatches`,
    {
      method: "POST",
      headers: {
        authorization: `Bearer ${env.GITHUB_TOKEN}`,
        accept: "application/vnd.github+json",
        "content-type": "application/json",
        "user-agent": "yuewei-oversight",
      },
      body: JSON.stringify({ ref: "main", inputs: { mode: "scan" } }),
    },
  );
  // 204 = 成功（GitHub 的 dispatch 接口不返回 body）
  if (res.status !== 204 && !res.ok) {
    throw new Error(`HTTP ${res.status} ${await res.text()}`);
  }
}

/** 发 Telegram 告警；没配 token 就静默跳过（不阻断主流程） */
async function alertOnce(env, text) {
  if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) return false;
  try {
    const res = await fetch(
      `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          chat_id: env.TELEGRAM_CHAT_ID,
          text,
          disable_web_page_preview: true,
        }),
      },
    );
    return res.ok;
  } catch (err) {
    return false;
  }
}

function num(v, dflt) {
  const n = Number(v);
  return Number.isFinite(n) && n > 0 ? n : dflt;
}
