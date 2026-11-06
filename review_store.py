"""私有审核存储客户端（V1.1 P0）。

## 为什么必须存在这一层

本仓库是 **Public**，`state.json` 被 git 跟踪，`scanner.yml` 每轮都会提交它。
所以 `state.json` 里的每一个字节都是公开的。

而 E4 待审事件的本质是「未公开报道、单一来源、尚未核实」——
把它的标题、事实摘要、原始链接、审核意见、审核人身份写进 `state.json`，
等于把「我们手上有一条未核实的独家」连同证据一起公开。

因此职责被切成两半：

    公开 state.json   —— 只存 opaque `review_ref` + 状态（state_store.record_review_stub）
    私有审核存储       —— 存完整 payload、审核人、操作日志、一次性标记

## 两种后端，同一套语义

  · `HttpReviewStore`  —— 生产用。走 Cloudflare Worker 的 /internal/review/* 私有接口，
                          真正落盘在 SQLite-backed Durable Object 里。
  · `LocalReviewStore` —— 本地模拟与测试用。同一份 SQL、同一套原子迁移，
                          落在本地 SQLite 文件里。

两者必须给出**完全相同**的判定结果，否则「本地测试全绿、上线就崩」。
`tests/test_review_privacy_v11.py` 里有专门的契约测试比对两边的 SQL 关键字。

## 原子性从哪来

不靠「先读后写 + 加锁」，靠**条件 UPDATE + changes() 计数**：

    UPDATE review SET status='decided' WHERE review_ref=? AND status='pending'

这条语句本身就是原子的。`changes()==1` 才算抢到，否则说明别人先动手了。
SQLite 在 Durable Object 里是单实例串行的，在本地是文件级事务，
两边都不存在「两个请求同时读到 pending 再各自写成功」的窗口。
"""

import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request


# ==================== 状态 ====================
PENDING = "pending"          # 已入队，等人点按钮
DECIDED = "decided"          # 有人点了按钮（批准或拒绝），但动作还没执行
CLAIMED = "claimed"          # 执行方已一次性认领，正在投递
DELIVERED = "delivered"      # 已真正发到公开频道
DECLINED = "declined"        # 人工拒绝并存档，终态
NEEDS_MANUAL = "needs_manual"  # 投递结果未知，需人工核对，不自动重发
FAILED = "failed"            # 确定性失败，可重新认领重试
EXITED = "exited"            # 第二来源到达，自动退出审核，终态

TERMINAL = frozenset({DELIVERED, DECLINED, EXITED})

ACTIONS = ("approve", "decline")

CLAIM_TTL_SECONDS = 900


class ReviewStoreError(RuntimeError):
    """私有审核存储不可用或返回错误。绝不允许被静默吞掉。"""


class ReviewConflict(ReviewStoreError):
    """状态冲突：已被他人认领、已决定、或已处于终态。"""


class ReviewNeedsManual(ReviewStoreError):
    """投递结果未知，必须人工核对，**绝不自动重发**。

    与 `ReviewConflict` 的区别是「谁的错」：
      · ReviewConflict   —— 这次操作本来就不该发生（已决、已被认领、已终态）
      · ReviewNeedsManual —— 上一位执行者的结果不可知（租约过期、发送超时），
                            再发一次可能造成重复公开推送

    调用方必须把它当成「停下来等人」，而不是「重试一下试试」。
    """


LEASE_EXPIRED_ERROR = (
    "认领租约已过期：上一位执行者的投递结果未知（消息可能已经发出），"
    "已转入人工核对，不会自动重发")


# ==================== SQL（与 review_vault.js 必须逐句对应） ====================
SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS review (
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
    )
    """,
    # 同一 event_id 只允许存在一条「还有效」的审核记录。
    # 终态（delivered/declined/exited）不占位，允许同一事件日后重新送审。
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_review_active
        ON review(event_id)
        WHERE status IN ('pending','decided','claimed','failed','needs_manual')
    """,
    """
    CREATE TABLE IF NOT EXISTS review_log (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        review_ref TEXT NOT NULL,
        at         TEXT NOT NULL,
        action     TEXT NOT NULL,
        actor      TEXT,
        detail     TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_review_log_ref ON review_log(review_ref)",
)

SQL_INSERT = """
INSERT INTO review (review_ref, event_id, status, payload, created_at, updated_at)
VALUES (?, ?, 'pending', ?, ?, ?)
"""

SQL_FIND_ACTIVE = """
SELECT review_ref, status FROM review
WHERE event_id = ? AND status IN ('pending','decided','claimed','failed','needs_manual')
"""

SQL_DECIDE = """
UPDATE review
SET status='decided', action=?, operator_id=?, note=?, updated_at=?
WHERE review_ref=? AND status='pending'
"""

SQL_CLAIM = """
UPDATE review
SET status='claimed', claim_token=?, claim_at=?, claim_expires_at=?,
    attempts=attempts+1, updated_at=?
WHERE review_ref=? AND status IN ('decided','failed')
"""

# 租约过期 = 上一位执行者「可能已经发出去了」。这里**只能**转人工，
# 绝不能让它落回可认领状态。
SQL_REAP_EXPIRED = """
UPDATE review
SET status='needs_manual', claim_token=NULL, last_error=?, updated_at=?
WHERE review_ref=? AND status='claimed'
  AND claim_expires_at IS NOT NULL AND claim_expires_at < ?
"""

# 人工核对后的显式放行：needs_manual → decided，重新进入可认领。
SQL_RELEASE = """
UPDATE review
SET status='decided', last_error=?, updated_at=?
WHERE review_ref=? AND status='needs_manual'
"""

SQL_FINISH_CLAIMED = """
UPDATE review
SET status=?, message_ids=?, last_error=?, claim_token=NULL, updated_at=?
WHERE review_ref=? AND status='claimed' AND claim_token=?
"""

SQL_EXIT = """
UPDATE review SET status='exited', last_error=?, updated_at=?
WHERE review_ref=? AND status IN ('pending','decided')
"""

SQL_GET = "SELECT * FROM review WHERE review_ref = ?"

SQL_GET_PAYLOAD = "SELECT payload FROM review WHERE review_ref = ?"

SQL_LOG_INSERT = "INSERT INTO review_log (review_ref, at, action, actor, detail) VALUES (?,?,?,?,?)"

SQL_LOG_LIST = "SELECT at, action, actor, detail FROM review_log WHERE review_ref=? ORDER BY id"


def _now_iso():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _iso_in(seconds):
    import datetime
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(seconds=seconds)).isoformat()


def new_review_ref():
    """opaque 句柄。不可猜测，不含任何新闻内容。

    必须是 **8 字节 / 16 位十六进制**，与 `review_vault.js` 的 `randomRef()` 一致。
    理由不是「短一点好看」，是 Telegram 对 `callback_data` 有 64 字节硬上限：

        "v1:a:<ref16>:<exp10>:<sig16>"  = 2+1+1+1+16+1+10+1+16 = 49 字节

    如果这里生成 32 位，拼出来就是 65 字节，按钮直接发不出去（400）——
    而本地后端生成 32 位、生产 Worker 生成 16 位，意味着「本地全绿、上线就崩」。
    两边必须给出完全相同的句柄长度。
    """
    return secrets.token_hex(8)


def redact_payload_for_log(payload):
    """日志里只允许出现「长度与结构」，不允许出现正文。

    这个函数存在的唯一目的，是让「Actions 日志泄露 payload」这件事
    在代码层面变成不可能——想打印 payload 时只能拿到这个摘要。
    """
    payload = payload or {}
    return {
        "title_len": len(str(payload.get("title") or "")),
        "summary_len": len(str(payload.get("fact_summary") or "")),
        "articles": len(payload.get("articles") or []),
        "importance": payload.get("importance") or "",
        "event_id": payload.get("event_id") or "",
    }


# ==================== 本地 SQLite 后端 ====================
class LocalReviewStore:
    """本地模拟后端：与 Durable Object 同一份 SQL、同一套原子迁移。

    用途只有两个：本地开发调试、自动化测试（含并发测试）。
    生产绝不使用它——它落在本地磁盘上，不是一个受控的私有服务。
    """

    def __init__(self, db_path=":memory:"):
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=15)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        for stmt in SCHEMA:
            self._conn.execute(stmt)
        self._conn.commit()

    # ---------- 内部 ----------
    def _tx(self):
        """BEGIN IMMEDIATE：抢写锁，保证「读-判-写」整体原子。"""
        return _Tx(self._conn, self._lock)

    def _log(self, ref, action, actor=None, detail=""):
        self._conn.execute(SQL_LOG_INSERT, (ref, _now_iso(), action, actor or "", detail or ""))
        self._conn.commit()

    # ---------- 接口 ----------
    def enqueue(self, payload):
        event_id = (payload or {}).get("event_id") or ""
        if not event_id:
            raise ReviewStoreError("payload 缺少 event_id，拒绝入队")
        with self._tx():
            row = self._conn.execute(SQL_FIND_ACTIVE, (event_id,)).fetchone()
            if row:
                # 幂等：同一事件已在审，返回已有句柄，不再开第二条
                return {"review_ref": row["review_ref"], "status": row["status"],
                        "duplicate": True}
            ref = new_review_ref()
            now = _now_iso()
            self._conn.execute(SQL_INSERT, (ref, event_id, json.dumps(payload, ensure_ascii=False), now, now))
        self._log(ref, "ENQUEUED", "system", "event_id=" + event_id)
        return {"review_ref": ref, "status": PENDING, "duplicate": False}

    def get(self, review_ref):
        row = self._conn.execute(SQL_GET, (review_ref,)).fetchone()
        if not row:
            raise ReviewConflict("审核记录不存在: {}".format(review_ref))
        return {
            "review_ref": row["review_ref"],
            "event_id": row["event_id"],
            "status": row["status"],
            "action": row["action"] or "",
            "operator_id": row["operator_id"] or "",
            "attempts": row["attempts"],
            "message_ids": json.loads(row["message_ids"] or "[]"),
            "last_error": row["last_error"] or "",
        }

    def peek(self, review_ref):
        """不含 payload 的状态查询。给「只想知道能不能点」的场景用。"""
        return self.get(review_ref)

    def peek_payload(self, review_ref):
        """只读地取回完整 payload，**不认领、不改状态**。

        与 `claim` 的区别：
          claim     = 取得「有权投递」的一次性授权，会推进状态机
          peek_payload = 只是读一下内容（用于渲染卡片、重算证据、构造视图）
        把两者混为一谈会让「看一眼」也消耗掉唯一一次认领机会。
        """
        row = self._conn.execute(SQL_GET_PAYLOAD, (review_ref,)).fetchone()
        if not row:
            raise ReviewConflict("审核记录不存在: {}".format(review_ref))
        try:
            return json.loads(row["payload"] or "{}")
        except Exception:                                  # noqa: BLE001
            raise ReviewStoreError("payload 不是合法 JSON")

    def decide(self, review_ref, action, operator_id, note=""):
        """登记审核决定。原子：pending → decided，只有一个请求能成功。"""
        if action not in ACTIONS:
            raise ReviewStoreError("非法审核动作: {}".format(action))
        with self._tx():
            cur = self._conn.execute(SQL_DECIDE, (action, operator_id or "", note or "",
                                                  _now_iso(), review_ref))
            if cur.rowcount != 1:
                row = self._conn.execute(SQL_GET, (review_ref,)).fetchone()
                if not row:
                    raise ReviewConflict("审核记录不存在: {}".format(review_ref))
                raise ReviewConflict(
                    "该事件已处理过（当前状态 {}，决定 {}），重复操作被拒绝".format(
                        row["status"], row["action"] or "-"))
        self._log(review_ref, "DECIDED:" + action, operator_id, note or "")
        return {"review_ref": review_ref, "status": DECIDED, "action": action}

    def claim(self, review_ref, ttl=CLAIM_TTL_SECONDS):
        """一次性认领：decided/failed → claimed，发一次性 claim_token。

        旧按钮重放拿不到第二次 claim：status 已经不是 decided 了。

        **租约过期不再自动重新认领**（PR #1 审查 P1）。旧 SQL 里有一句
        `OR (status='claimed' AND claim_expires_at < ?)`，语义是「上一任超时
        了，换你上」。可投递不是一个能回滚的动作：

            Telegram 发送成功  →  进程被杀  →  租约过期  →  另一个 job 认领
                                                                  ↓
                                                            重复公开推送

        发送与 finish 回写不可能构成一个原子事务，所以「租约过期」这个事实
        只能说明**结果未知**，不能说明**没发出去**。现在的处理：
          · 过期 → 本方法先把它原子地转成 needs_manual（落 last_error）
          · 然后抛 ReviewNeedsManual，绝不返回可用的 claim_token
          · 人工核对后调用 release() 显式放行，才回到 decided 可认领

        确定性失败（failed）的重试机制原样保留：那类失败是「明确没发出去」，
        重发不会造成重复。
        """
        token = secrets.token_hex(16)
        now = _now_iso()
        expired = False
        claimed_ok = False
        row = None
        with self._tx():
            # 第一步：过期租约 → needs_manual。与认领在同一个事务里，
            # 避免「两个人同时看到过期、一个转人工一个抢走」。
            reaped = self._conn.execute(
                SQL_REAP_EXPIRED, (LEASE_EXPIRED_ERROR, now, review_ref, now))
            if reaped.rowcount == 1:
                expired = True
                row = self._conn.execute(SQL_GET, (review_ref,)).fetchone()
            else:
                cur = self._conn.execute(
                    SQL_CLAIM, (token, now, _iso_in(ttl), now, review_ref))
                claimed_ok = cur.rowcount == 1
                row = self._conn.execute(SQL_GET, (review_ref,)).fetchone()

        # 事务已提交（含 needs_manual 那一步），下面才允许抛异常。
        # 反过来在 with 里抛，_Tx 会回滚——人工核查状态就白写了。
        if expired:
            self._log(review_ref, "LEASE_EXPIRED", "system", LEASE_EXPIRED_ERROR)
            raise ReviewNeedsManual(LEASE_EXPIRED_ERROR)
        if not claimed_ok:
            if not row:
                raise ReviewConflict("审核记录不存在: {}".format(review_ref))
            # needs_manual 必须继续报 needs_manual，不能退化成普通冲突——
            # 否则调用方会把「等人核对」误读成「有人正在发」，从而另找路径重试。
            # 这一分支与 review_vault.js 的 claim() 逐句对应。
            if row["status"] == NEEDS_MANUAL:
                raise ReviewNeedsManual(
                    "该记录处于人工核对状态（needs_manual），"
                    "必须由人工放行后才能重新投递")
            raise ReviewConflict(
                "无法认领（当前状态 {}），可能已被另一名审核人或另一个 job 接手".format(
                    row["status"]))

        self._log(review_ref, "CLAIMED", "action-run", "token=" + token[:8] + "…")
        return {
            "review_ref": review_ref,
            "event_id": row["event_id"],
            "action": row["action"],
            "payload": json.loads(row["payload"]),
            "claim_token": token,
            "attempts": row["attempts"],
        }

    def release(self, review_ref, operator_id, note=""):
        """人工核对后显式放行：needs_manual → decided。

        这是「过期认领」唯一的复活通道，必须有人签名。没有它，needs_manual
        就是死档；有了它，复活这件事在日志里留得下 operator 与理由。
        """
        if not (operator_id or "").strip():
            raise ReviewStoreError("release 必须带 operator_id（放行必须有人负责）")
        detail = "{}: {}".format(operator_id, (note or "")[:200])
        with self._tx():
            cur = self._conn.execute(SQL_RELEASE, (detail, _now_iso(), review_ref))
            if cur.rowcount != 1:
                row = self._conn.execute(SQL_GET, (review_ref,)).fetchone()
                if not row:
                    raise ReviewConflict("审核记录不存在: {}".format(review_ref))
                raise ReviewConflict(
                    "该记录不在 needs_manual 状态（当前 {}），无需放行".format(
                        row["status"]))
        self._log(review_ref, "RELEASED:manual", operator_id, note or "")
        return {"review_ref": review_ref, "status": DECIDED}

    def finish(self, review_ref, claim_token, outcome, message_ids=None, error=""):
        """回传投递结果。**只有 Telegram 真的成功才允许 outcome=delivered。**

        一次性：finish 成功后 claim_token 被清空，同一个 token 再用必然失败。

        四种结果（delivered / needs_manual / failed / declined）走**同一条**
        条件 UPDATE：`WHERE status='claimed' AND claim_token=?`。

        早期版本给 declined 单独写了一条 `WHERE status='decided'` 的语句，
        但 `execute_review_decision` 永远是「先 claim 再 finish」——走到这里时
        状态已经是 `claimed`，那条语句永远命中 0 行，于是拒绝永远停在 claimed，
        既不终态也不可重试。多一条分支就多一个「只在某个顺序下才成立」的坑。
        """
        if outcome not in (DELIVERED, NEEDS_MANUAL, FAILED, DECLINED):
            raise ReviewStoreError("非法投递结果: {}".format(outcome))
        mids = json.dumps(list(message_ids or []), ensure_ascii=False)
        with self._tx():
            cur = self._conn.execute(SQL_FINISH_CLAIMED,
                                     (outcome, mids, (error or "")[:300], _now_iso(),
                                      review_ref, claim_token or ""))
            if cur.rowcount != 1:
                row = self._conn.execute(SQL_GET, (review_ref,)).fetchone()
                if not row:
                    raise ReviewConflict("审核记录不存在: {}".format(review_ref))
                raise ReviewConflict(
                    "结果无法登记（当前状态 {}）：claim_token 已失效或已被他人使用".format(
                        row["status"]))
        self._log(review_ref, "FINISH:" + outcome, "action-run", (error or "")[:120])
        return {"review_ref": review_ref, "status": outcome, "message_ids": message_ids or []}

    def exit_review(self, review_ref, reason=""):
        """第二来源到达 → 自动退出审核（不再需要人工判断）。"""
        with self._tx():
            cur = self._conn.execute(SQL_EXIT, ((reason or "")[:300], _now_iso(), review_ref))
            if cur.rowcount != 1:
                raise ReviewConflict("该记录已不在待审状态，无法自动退出")
        self._log(review_ref, "EXITED", "system", reason or "")
        return {"review_ref": review_ref, "status": EXITED}

    def log(self, review_ref):
        rows = self._conn.execute(SQL_LOG_LIST, (review_ref,)).fetchall()
        return [{"at": r["at"], "action": r["action"], "actor": r["actor"],
                 "detail": r["detail"]} for r in rows]

    def close(self):
        try:
            self._conn.close()
        except Exception:                                  # noqa: BLE001
            pass


class _Tx:
    """BEGIN IMMEDIATE 上下文。所有写操作都必须包在这里。"""

    def __init__(self, conn, lock):
        self.conn = conn
        self.lock = lock

    def __enter__(self):
        self.lock.acquire()
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.lock.release()
        return False


# ==================== HTTP 后端（生产） ====================
class HttpReviewStore:
    """生产后端：Cloudflare Worker /internal/review/*，落盘在 SQLite Durable Object。

    认证用 `X-Service-Key`（GitHub Secret `REVIEW_SERVICE_KEY` 与 Worker
    secret `INTERNAL_SERVICE_KEY` 同值）。这条通道只走服务器到服务器，
    审核人身份、HMAC、正文都不会出现在 workflow_dispatch inputs 里。
    """

    def __init__(self, base_url, service_key, timeout=25, transport=None):
        self.base_url = (base_url or "").rstrip("/")
        self.service_key = service_key or ""
        self.timeout = timeout
        self._transport = transport or self._http

    # ---------- 传输 ----------
    def _http(self, path, method="GET", payload=None):
        url = self.base_url + path
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "X-Service-Key": self.service_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "yuewei-review-client",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:2000]
            except Exception:                              # noqa: BLE001
                pass
            # DO 的 4xx 响应体里带机器可读标记（conflict / needs_manual）。
            # 必须**在这里**就把它们交回 `_call` 统一映射：409 既可能是
            # 「别人正在发」，也可能是「租约过期、结果未知」，两者对调用方的
            # 要求完全相反（前者换条路，后者停下来等人）。一律当成
            # ReviewConflict 会把「必须人工核对」误读成「重试一下试试」。
            try:
                parsed = json.loads(detail) if detail.strip() else None
            except Exception:                              # noqa: BLE001
                parsed = None
            if isinstance(parsed, dict) and parsed.get("ok") is False:
                return parsed
            if exc.code in (404, 409):
                raise ReviewConflict("私有审核存储拒绝: HTTP {} {}".format(exc.code, detail))
            raise ReviewStoreError("私有审核存储不可用: HTTP {} {}".format(exc.code, detail))
        except Exception as exc:                           # noqa: BLE001
            raise ReviewStoreError(
                "私有审核存储不可用: {}: {}".format(type(exc).__name__, exc))
        try:
            return json.loads(body) if body.strip() else {}
        except Exception as exc:                           # noqa: BLE001
            raise ReviewStoreError("私有审核存储返回非 JSON: {}".format(exc))

    def _call(self, path, method="GET", payload=None):
        out = self._transport(path, method=method, payload=payload) or {}
        if out.get("ok") is False:
            err = out.get("error") or "unknown"
            # 顺序有意义：needs_manual 也是 conflict，但语义完全不同——
            # 「结果未知、必须人工核对」不能退化成「重试一下」。
            if out.get("needs_manual"):
                raise ReviewNeedsManual(err)
            if out.get("conflict"):
                raise ReviewConflict(err)
            raise ReviewStoreError(err)
        return out

    # ---------- 接口 ----------
    def enqueue(self, payload):
        return self._call("/internal/review/enqueue", "POST", {"payload": payload})

    def get(self, review_ref):
        return self._call("/internal/review/{}/state".format(review_ref))

    def peek(self, review_ref):
        return self.get(review_ref)

    def peek_payload(self, review_ref):
        out = self._call("/internal/review/{}/payload".format(review_ref))
        return out.get("payload") or {}

    def decide(self, review_ref, action, operator_id, note=""):
        return self._call("/internal/review/{}/decide".format(review_ref), "POST",
                          {"action": action, "operator_id": operator_id, "note": note})

    def claim(self, review_ref, ttl=CLAIM_TTL_SECONDS):
        return self._call("/internal/review/{}/claim".format(review_ref), "POST",
                          {"ttl": ttl})

    def finish(self, review_ref, claim_token, outcome, message_ids=None, error=""):
        return self._call("/internal/review/{}/finish".format(review_ref), "POST",
                          {"claim_token": claim_token, "outcome": outcome,
                           "message_ids": list(message_ids or []), "error": error})

    def exit_review(self, review_ref, reason=""):
        return self._call("/internal/review/{}/exit".format(review_ref), "POST",
                          {"reason": reason})

    def release(self, review_ref, operator_id, note=""):
        return self._call("/internal/review/{}/release".format(review_ref), "POST",
                          {"operator_id": operator_id, "note": note})

    def log(self, review_ref):
        return (self._call("/internal/review/{}/log".format(review_ref)) or {}).get("entries") or []

    def close(self):
        pass


# ==================== 工厂 ====================
def from_env(env=None):
    """按环境变量装配后端。返回 None 表示**未配置**。

    调用方必须把 None 当成「审核通道不可用」处理，绝不能退回到
    「把 payload 写进 state.json」——那正是本模块要消灭的行为。
    """
    env = env if env is not None else os.environ
    url = (env.get("REVIEW_STORE_URL") or "").strip()
    key = (env.get("REVIEW_SERVICE_KEY") or "").strip()
    if url and key:
        return HttpReviewStore(url, key)
    local = (env.get("REVIEW_STORE_LOCAL_DB") or "").strip()
    if local:
        return LocalReviewStore(local)
    return None


def describe(store):
    """给日志用的一句话描述（不含任何密钥）。"""
    if store is None:
        return "未配置（审核通道不可用，E4 事件将被挂起且不公开）"
    if isinstance(store, HttpReviewStore):
        return "私有 Durable Object（{}）".format(store.base_url)
    return "本地 SQLite（{}）".format(getattr(store, "db_path", "?"))
