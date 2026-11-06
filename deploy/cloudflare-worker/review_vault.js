/**
 * 私有审核金库 + 调度锁（Cloudflare SQLite-backed Durable Object）
 *
 * ============================================================================
 * 为什么必须是 Durable Object，而不是 KV / 内存变量 / 公开 state.json
 * ============================================================================
 * 三样东西都需要**强一致 + 原子**：
 *
 *   1. 一条 E4 事件的审核决定：两个审核人同时点「批准」和「拒绝」，
 *      必须只有一个生效。KV 是最终一致的，两个写入都可能「成功」，
 *      最后谁赢取决于复制顺序——这不叫锁。
 *   2. 一次性认领令牌：`claim` 必须只成功一次，否则同一条新闻会被
 *      两个 job 各投递一遍到公开频道。
 *   3. 调度冷却与告警去重：Worker 随时可能被回收，内存变量跨 invocation
 *      不保证存在。用内存变量冒充持久化锁，等于没有锁。
 *
 * Durable Object 的两个特性正好对上：
 *   · **单实例串行**：同一个 DO 实例上的请求天然串行，不存在两个请求
 *     同时读到 pending 再各自写成功的窗口；
 *   · **SQLite 事务**：条件 UPDATE + changes() 计数给出可验证的原子迁移。
 *
 * 存储后端用 SQLite（`new_sqlite_classes`），这是 Workers Free 计划可用的
 * 那一种；键值存储后端需要付费计划。具体可用性核查见 README 与交付报告。
 *
 * ============================================================================
 * 表结构
 * ============================================================================
 *   review        —— 一条待审事件的完整资料与生命周期（含 payload 正文）
 *   review_log    —— 追加式操作日志（谁、何时、做了什么）
 *   ops_kv        —— 调度冷却等标量状态
 *   ops_fault     —— 故障周期记账（首次发现、是否已告警）
 *
 * `review.payload` 是**唯一**保存新闻正文的地方。它不进 git、不进
 * workflow_dispatch inputs、不进 Actions 日志。
 */

/* ==================== SQL（与 review_store.py 逐句对应） ==================== */
export const SCHEMA = [
  `CREATE TABLE IF NOT EXISTS review (
      review_ref       TEXT PRIMARY KEY,
      event_id         TEXT NOT NULL,
      status           TEXT NOT NULL,
      action           TEXT,
      payload          TEXT NOT NULL,
      operator_id      TEXT,
      note             TEXT,
      claim_token      TEXT,
      claim_at         TEXT,
      claim_expires_at TEXT,
      message_ids      TEXT,
      attempts         INTEGER NOT NULL DEFAULT 0,
      last_error       TEXT,
      created_at       TEXT NOT NULL,
      updated_at       TEXT NOT NULL
  )`,
  // 同一 event_id 只允许一条「还有效」的审核记录；终态不占位。
  `CREATE UNIQUE INDEX IF NOT EXISTS ux_review_active
      ON review(event_id)
      WHERE status IN ('pending','decided','claimed','failed','needs_manual')`,
  `CREATE TABLE IF NOT EXISTS review_log (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      review_ref TEXT NOT NULL,
      at         TEXT NOT NULL,
      action     TEXT NOT NULL,
      actor      TEXT,
      detail     TEXT
  )`,
  `CREATE INDEX IF NOT EXISTS ix_review_log_ref ON review_log(review_ref)`,
  `CREATE TABLE IF NOT EXISTS ops_kv (
      k   TEXT PRIMARY KEY,
      v   TEXT NOT NULL,
      at  TEXT NOT NULL
  )`,
  `CREATE TABLE IF NOT EXISTS ops_fault (
      kind       TEXT PRIMARY KEY,
      since      TEXT NOT NULL,
      last_seen  TEXT NOT NULL,
      alerted    INTEGER NOT NULL DEFAULT 0,
      alerted_at TEXT,
      attempts   INTEGER NOT NULL DEFAULT 0
  )`,
];

export const SQL = {
  insertReview: `INSERT INTO review (review_ref, event_id, status, payload, created_at, updated_at)
                 VALUES (?, ?, 'pending', ?, ?, ?)`,
  findActive: `SELECT review_ref, status FROM review
               WHERE event_id = ? AND status IN ('pending','decided','claimed','failed','needs_manual')`,
  getReview: `SELECT * FROM review WHERE review_ref = ?`,
  getPayload: `SELECT payload FROM review WHERE review_ref = ?`,
  decide: `UPDATE review
           SET status='decided', action=?, operator_id=?, note=?, updated_at=?
           WHERE review_ref=? AND status='pending'`,
  claim: `UPDATE review
          SET status='claimed', claim_token=?, claim_at=?, claim_expires_at=?,
              attempts=attempts+1, updated_at=?
          WHERE review_ref=? AND (
              status IN ('decided','failed')
              OR (status='claimed' AND claim_expires_at IS NOT NULL AND claim_expires_at < ?)
          )`,
  finishClaimed: `UPDATE review
                  SET status=?, message_ids=?, last_error=?, claim_token=NULL, updated_at=?
                  WHERE review_ref=? AND status='claimed' AND claim_token=?`,
  exitReview: `UPDATE review SET status='exited', last_error=?, updated_at=?
               WHERE review_ref=? AND status IN ('pending','decided')`,
  logInsert: `INSERT INTO review_log (review_ref, at, action, actor, detail) VALUES (?,?,?,?,?)`,
  logList: `SELECT at, action, actor, detail FROM review_log WHERE review_ref=? ORDER BY id`,
  kvGet: `SELECT v, at FROM ops_kv WHERE k=?`,
  kvPut: `INSERT INTO ops_kv (k, v, at) VALUES (?,?,?)
          ON CONFLICT(k) DO UPDATE SET v=excluded.v, at=excluded.at`,
  faultGet: `SELECT * FROM ops_fault WHERE kind=?`,
  faultTouch: `INSERT INTO ops_fault (kind, since, last_seen, alerted, attempts)
               VALUES (?,?,?,0,0)
               ON CONFLICT(kind) DO UPDATE SET last_seen=excluded.last_seen`,
  faultClaimAlert: `UPDATE ops_fault SET alerted=1, alerted_at=?, last_seen=?, attempts=attempts+1
                    WHERE kind=? AND alerted=0`,
  faultReleaseAlert: `UPDATE ops_fault SET alerted=0, alerted_at=NULL WHERE kind=?`,
  faultClear: `DELETE FROM ops_fault WHERE kind=?`,
  faultList: `SELECT kind, since, last_seen, alerted, alerted_at, attempts FROM ops_fault`,
};

const ACTIONS = ["approve", "decline"];
const CLAIM_TTL_SECONDS = 900;
const VALID_OUTCOMES = ["delivered", "needs_manual", "failed", "declined"];

function nowIso(ms) {
  return new Date(ms == null ? Date.now() : ms).toISOString();
}

function isoIn(seconds, ms) {
  return new Date((ms == null ? Date.now() : ms) + seconds * 1000).toISOString();
}

/**
 * 生成 opaque 句柄：16 位十六进制（8 字节随机）。
 *
 * 为什么不是 32 位：review_ref 要放进 Telegram 按钮的 callback_data，
 * 而 Telegram 对这个字段有 **64 字节硬上限**。
 * "v1:a:<ref>:<exp>:<sig16>" 在 ref=16 时正好 49 字节，留有余量。
 * 8 字节随机 + HMAC 签名绑定，伪造一个能通过验签的 ref 是不可行的。
 */
function randomRef() {
  const bytes = new Uint8Array(8);
  crypto.getRandomValues(bytes);
  return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function jsonRes(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

/** 错误响应：`conflict: true` 让客户端区分「状态冲突」与「服务不可用」 */
function errRes(message, status = 400, conflict = false) {
  return jsonRes({ ok: false, error: message, conflict }, status);
}

/**
 * Durable Object 本体。
 *
 * 只接受 `/review/*` 与 `/ops/*` 两类内部路由；鉴权在 Worker 层完成，
 * 这里再校验一次（纵深防御：万一 Worker 路由写错，DO 自己也不会裸奔）。
 */
export class Vault {
  constructor(state, env) {
    this.state = state;
    this.env = env;
    this.sql = state.storage.sql;
    // 建表必须在处理任何请求之前完成，用 blockConcurrencyWhile 挡住并发
    this.state.blockConcurrencyWhile(async () => {
      for (const stmt of SCHEMA) this.sql.exec(stmt);
    });
  }

  async fetch(request) {
    const url = new URL(request.url);
    const path = url.pathname;

    // 纵深防御：Worker 已经校验过，这里再校验一次
    const expected = (this.env.INTERNAL_SERVICE_KEY || "").trim();
    const got = request.headers.get("X-Service-Key") || "";
    if (!expected || !timingSafeEqual(got, expected)) {
      return errRes("service key 校验失败", 401);
    }

    try {
      if (path === "/review/enqueue" && request.method === "POST") {
        return await this.enqueue(await request.json());
      }
      if (path === "/review/by-event" && request.method === "POST") {
        const body = await request.json();
        const row = this.sql.exec(SQL.findActive, body.event_id || "").toArray()[0];
        return jsonRes({ ok: true, found: !!row, review_ref: row ? row.review_ref : "",
                         status: row ? row.status : "" });
      }
      if (path === "/ops/cooldown" && request.method === "POST") {
        return await this.cooldown(await request.json());
      }
      if (path === "/ops/fault" && request.method === "POST") {
        return await this.fault(await request.json());
      }
      if (path === "/ops/faults" && request.method === "GET") {
        return jsonRes({ ok: true, faults: this.sql.exec(SQL.faultList).toArray() });
      }

      const m = path.match(/^\/review\/([0-9a-f]{8,64})\/(state|payload|decide|claim|finish|exit|log)$/);
      if (m) {
        const [, ref, op] = m;
        if (op === "state" && request.method === "GET") return this.getState(ref);
        if (op === "payload" && request.method === "GET") return this.getPayload(ref);
        // 日志是读操作。`review_store.HttpReviewStore.log()` 用的是 GET，
        // 如果这里只认 POST，生产上取日志会 405，而 Python 侧把这个异常吞掉
        // 返回空列表——表现为「审核日志永远是空的」，且不报错。
        if (op === "log" && request.method === "GET") {
          return jsonRes({ ok: true, entries: this.logEntries(ref) });
        }
        if (request.method !== "POST") return errRes("method_not_allowed", 405);
        const body = await request.json().catch(() => ({}));
        if (op === "decide") return this.decide(ref, body);
        if (op === "claim") return this.claim(ref, body);
        if (op === "finish") return this.finish(ref, body);
        if (op === "exit") return this.exitReview(ref, body);
        if (op === "log") return jsonRes({ ok: true, entries: this.logEntries(ref) });
      }
      return errRes("not_found", 404);
    } catch (err) {
      return errRes("vault_internal_error: " + (err && err.message ? err.message : String(err)), 500);
    }
  }

  /* ---------------- 审核 ---------------- */

  async enqueue(body) {
    const payload = (body && body.payload) || {};
    const eventId = String(payload.event_id || "");
    if (!eventId) return errRes("payload 缺少 event_id");

    const existing = this.sql.exec(SQL.findActive, eventId).toArray()[0];
    if (existing) {
      return jsonRes({ ok: true, review_ref: existing.review_ref,
                       status: existing.status, duplicate: true });
    }

    const ref = randomRef();
    const now = nowIso();
    try {
      this.sql.exec(SQL.insertReview, ref, eventId, JSON.stringify(payload), now, now);
    } catch (err) {
      // 唯一索引冲突 = 并发入队，返回已有的那条（幂等）
      const row = this.sql.exec(SQL.findActive, eventId).toArray()[0];
      if (row) {
        return jsonRes({ ok: true, review_ref: row.review_ref, status: row.status,
                         duplicate: true });
      }
      throw err;
    }
    this.log(ref, "ENQUEUED", "system", "event_id=" + eventId);
    return jsonRes({ ok: true, review_ref: ref, status: "pending", duplicate: false });
  }

  getState(ref) {
    const row = this.sql.exec(SQL.getReview, ref).toArray()[0];
    if (!row) return errRes("审核记录不存在", 404, true);
    return jsonRes({
      ok: true,
      review_ref: row.review_ref,
      event_id: row.event_id,
      status: row.status,
      action: row.action || "",
      operator_id: row.operator_id || "",
      attempts: row.attempts,
      message_ids: JSON.parse(row.message_ids || "[]"),
      last_error: row.last_error || "",
    });
  }

  /**
   * 只读地取回完整 payload，不认领、不改状态。
   *
   * 与 claim 的区别必须严格保持：
   *   claim   = 取得「有权投递」的一次性授权，会推进状态机
   *   payload = 只是读一下内容（渲染卡片、重算证据、构造视图）
   * 混为一谈会让「看一眼」也消耗掉唯一一次认领机会。
   */
  getPayload(ref) {
    const row = this.sql.exec(SQL.getPayload, ref).toArray()[0];
    if (!row) return errRes("审核记录不存在", 404, true);
    let payload;
    try {
      payload = JSON.parse(row.payload || "{}");
    } catch (err) {
      return errRes("payload 不是合法 JSON", 500);
    }
    return jsonRes({ ok: true, review_ref: ref, payload });
  }

  decide(ref, body) {
    const action = String(body.action || "");
    if (!ACTIONS.includes(action)) return errRes("非法审核动作: " + action);

    const cur = this.sql.exec(SQL.decide, action, String(body.operator_id || ""),
      String(body.note || ""), nowIso(), ref);
    if (cur.rowsWritten !== 1) {
      const row = this.sql.exec(SQL.getReview, ref).toArray()[0];
      if (!row) return errRes("审核记录不存在", 404, true);
      return errRes(
        "该事件已处理过（当前状态 " + row.status + "，决定 " + (row.action || "-") + "）",
        409, true);
    }
    this.log(ref, "DECIDED:" + action, String(body.operator_id || ""), String(body.note || ""));
    return jsonRes({ ok: true, review_ref: ref, status: "decided", action });
  }

  claim(ref, body) {
    const ttl = Number(body.ttl) > 0 ? Number(body.ttl) : CLAIM_TTL_SECONDS;
    const token = randomRef();
    const now = nowIso();
    const cur = this.sql.exec(SQL.claim, token, now, isoIn(ttl), now, ref, now);
    if (cur.rowsWritten !== 1) {
      const row = this.sql.exec(SQL.getReview, ref).toArray()[0];
      if (!row) return errRes("审核记录不存在", 404, true);
      return errRes(
        "无法认领（当前状态 " + row.status + "），可能已被另一名审核人或另一个 job 接手",
        409, true);
    }
    const row = this.sql.exec(SQL.getReview, ref).toArray()[0];
    this.log(ref, "CLAIMED", "action-run", "token=" + token.slice(0, 8) + "…");
    return jsonRes({
      ok: true,
      review_ref: ref,
      event_id: row.event_id,
      action: row.action,
      payload: JSON.parse(row.payload),
      claim_token: token,
      attempts: row.attempts,
    });
  }

  finish(ref, body) {
    const outcome = String(body.outcome || "");
    if (!VALID_OUTCOMES.includes(outcome)) return errRes("非法投递结果: " + outcome);
    const mids = JSON.stringify(Array.isArray(body.message_ids) ? body.message_ids : []);
    const error = String(body.error || "").slice(0, 300);

    // 四种结果走同一条条件 UPDATE。
    // 早期版本给 declined 单独写了一条 `WHERE status='decided'` 的语句，
    // 但执行方永远是「先 claim 再 finish」——走到这里状态已经是 claimed，
    // 那条语句永远命中 0 行，拒绝就永远停在 claimed：既不终态也不可重试。
    const cur = this.sql.exec(SQL.finishClaimed, outcome, mids, error, nowIso(), ref,
                              String(body.claim_token || ""));

    if (cur.rowsWritten !== 1) {
      const row = this.sql.exec(SQL.getReview, ref).toArray()[0];
      if (!row) return errRes("审核记录不存在", 404, true);
      return errRes(
        "结果无法登记（当前状态 " + row.status + "）：claim_token 已失效或已被他人使用",
        409, true);
    }
    this.log(ref, "FINISH:" + outcome, "action-run", error.slice(0, 120));
    return jsonRes({ ok: true, review_ref: ref, status: outcome,
                     message_ids: JSON.parse(mids) });
  }

  exitReview(ref, body) {
    const cur = this.sql.exec(SQL.exitReview, String(body.reason || "").slice(0, 300),
      nowIso(), ref);
    if (cur.rowsWritten !== 1) return errRes("该记录已不在待审状态，无法自动退出", 409, true);
    this.log(ref, "EXITED", "system", String(body.reason || ""));
    return jsonRes({ ok: true, review_ref: ref, status: "exited" });
  }

  log(ref, action, actor, detail) {
    this.sql.exec(SQL.logInsert, ref, nowIso(), action, actor || "", detail || "");
  }

  logEntries(ref) {
    return this.sql.exec(SQL.logList, ref).toArray();
  }

  /* ---------------- 调度锁 ---------------- */

  /**
   * 原子「检查并占用」冷却窗口。
   *
   * 这是修掉「调度风暴」的核心：旧实现每秒醒一次就 dispatch 一次，
   * 因为「上次什么时候 dispatch 过」这件事根本没被记住。
   * 这里把「读上次时间 → 判断是否在冷却内 → 占用」三步放进同一个 DO 请求，
   * DO 单实例串行，所以两个并发 cron 不可能同时拿到 blocked=false。
   */
  cooldown(body) {
    const nowMs = Number(body.now_ms) > 0 ? Number(body.now_ms) : Date.now();
    const cooldownMs = Number(body.cooldown_ms) > 0 ? Number(body.cooldown_ms) : 600000;
    // mark=false 只查询不占用，供「先看一眼再决定要不要抢」的场景使用。
    // 真正 dispatch 之前必须用 mark=true 再抢一次——那一步才是原子的。
    const mark = body.mark !== false;
    const row = this.sql.exec(SQL.kvGet, "last_dispatch_at").toArray()[0];
    const last = row ? Date.parse(row.v) : NaN;
    if (Number.isFinite(last) && nowMs - last < cooldownMs) {
      return jsonRes({ ok: true, blocked: true, last_dispatch_at: row.v,
                       remaining_ms: cooldownMs - (nowMs - last) });
    }
    if (!mark) {
      return jsonRes({ ok: true, blocked: false,
                       last_dispatch_at: row ? row.v : "", remaining_ms: 0, peek: true });
    }
    const iso = nowIso(nowMs);
    this.sql.exec(SQL.kvPut, "last_dispatch_at", iso, iso);
    return jsonRes({ ok: true, blocked: false, last_dispatch_at: iso, remaining_ms: 0 });
  }

  /**
   * 故障记账三动作：
   *   touch       —— 记「故障仍在」，不改 since、不改 alerted
   *   claim_alert —— **原子**占用本次告警名额；同一故障周期只有第一个请求拿到
   *   clear       —— 恢复后删除记录，下一个故障周期重新计数
   */
  fault(body) {
    const kind = String(body.kind || "");
    const action = String(body.action || "");
    if (!kind) return errRes("缺少 kind");

    if (action === "touch") {
      const now = nowIso();
      this.sql.exec(SQL.faultTouch, kind, now, now);
      const row = this.sql.exec(SQL.faultGet, kind).toArray()[0];
      return jsonRes({ ok: true, kind, since: row.since, alerted: !!row.alerted,
                       age_ms: Date.now() - Date.parse(row.since) });
    }

    if (action === "claim_alert") {
      // 先确保记录存在，再用条件 UPDATE 抢名额
      const now = nowIso();
      this.sql.exec(SQL.faultTouch, kind, now, now);
      const cur = this.sql.exec(SQL.faultClaimAlert, now, now, kind);
      const row = this.sql.exec(SQL.faultGet, kind).toArray()[0];
      return jsonRes({ ok: true, kind, should_alert: cur.rowsWritten === 1,
                       since: row ? row.since : now, alerted: row ? !!row.alerted : false });
    }

    if (action === "release_alert") {
      // 告警发送失败时把名额还回去，让下一轮重试。
      // 不这么做的话，「抢占成功但 Telegram 挂了」会让这次故障彻底静音。
      this.sql.exec(SQL.faultReleaseAlert, kind);
      return jsonRes({ ok: true, kind, released: true });
    }

    if (action === "clear") {
      this.sql.exec(SQL.faultClear, kind);
      return jsonRes({ ok: true, kind, cleared: true });
    }

    return errRes("未知故障动作: " + action);
  }
}

/** 常数时间比较 */
function timingSafeEqual(a, b) {
  const x = String(a == null ? "" : a);
  const y = String(b == null ? "" : b);
  if (x.length !== y.length) return false;
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= x.charCodeAt(i) ^ y.charCodeAt(i);
  return diff === 0;
}
