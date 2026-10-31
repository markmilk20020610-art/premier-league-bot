"""状态持久化与投递状态机（阶段 2.1 / 2.2）

历史问题
--------
旧 `save_state()` 直接 open(path,'w') 覆盖写：
  · 写到一半被 kill → state.json 变成半截 JSON，下次 load 直接抛异常退回空状态，
    去重记录全丢 → 同一批新闻被重复推送；
  · 写失败只 print 一行 [ERROR] 就继续，`state_updated` 照样是 true，
    于是「Telegram 已发 + 状态没存」这种最坏情况被伪装成成功。

现在改为：临时文件 → fsync → os.replace 原子替换 → 回读校验 → 失败抛异常。
任何调用方都必须显式处理 StateError，不允许静默吞掉。
"""

import json
import os
import tempfile
import hashlib
import datetime

import redact

SCHEMA_VERSION = 3

# ==================== 投递状态机 ====================
DISCOVERED = "DISCOVERED"
VERIFIED = "VERIFIED"
PENDING_DELIVERY = "PENDING_DELIVERY"
DELIVERED = "DELIVERED"
COMMITTED = "COMMITTED"
DELIVERY_FAILED = "DELIVERY_FAILED"
# V1.2 任务 9：把「失败」拆成两种性质完全不同的失败。
#   RETRYABLE_FAILURE  确定没送达（429 / 5xx / 连接失败），按退避重试
#   DELIVERY_UNCERTAIN 请求已经发出但没等到响应（读超时），可能已经送达
# 混为一谈的后果：把「可能已送达」当成「确定失败」重发 → 用户收到重复推送；
# 或者把「确定失败」当成「可能已送达」不再重试 → 新闻永远发不出去。
RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
DELIVERY_UNCERTAIN = "DELIVERY_UNCERTAIN"

# 允许的状态迁移。COMMITTED 是终态。
_TRANSITIONS = {
    DISCOVERED: {VERIFIED, DELIVERY_FAILED},
    VERIFIED: {PENDING_DELIVERY, DELIVERY_FAILED},
    PENDING_DELIVERY: {DELIVERED, RETRYABLE_FAILURE, DELIVERY_UNCERTAIN, DELIVERY_FAILED},
    # 可重试失败：下一轮重新进入投递队列；重试期间也可能退化成「结果未知」
    RETRYABLE_FAILURE: {PENDING_DELIVERY, DELIVERY_UNCERTAIN, DELIVERY_FAILED},
    # 结果未知：只有在拿到确定性结论（补到 message_id / 人工确认失败）时才离开
    DELIVERY_UNCERTAIN: {DELIVERED, DELIVERY_FAILED},
    DELIVERY_FAILED: {PENDING_DELIVERY, DELIVERED},
    DELIVERED: {COMMITTED},
    COMMITTED: set(),
}

# ==================== 分段投递（V1.2 任务 11） ====================
# 一条长消息会被切成多段发送。每段是一条独立记录，任何一段成功都不许被重发，
# 部分成功也不许在汇总里被说成「全部成功」。
CHUNK_DELIVERED = "DELIVERED"
CHUNK_RETRYABLE = "RETRYABLE_FAILURE"
CHUNK_UNCERTAIN = "DELIVERY_UNCERTAIN"
CHUNK_FAILED = "DELIVERY_FAILED"
CHUNK_NOT_ATTEMPTED = "NOT_ATTEMPTED"
CHUNK_STATUSES = {CHUNK_DELIVERED, CHUNK_RETRYABLE, CHUNK_UNCERTAIN,
                  CHUNK_FAILED, CHUNK_NOT_ATTEMPTED}

# 重试退避（分钟）。按 event["attempts"] 取，用完后一直取最后一个。
RETRY_BACKOFF_MINUTES = (15, 60, 180)

# 同一个事件累计投递这么多次仍失败就放弃，避免 403 这类确定性错误每轮重刷。
MAX_DELIVERY_ATTEMPTS = 3


class StateError(RuntimeError):
    """state.json 读写失败。必须向上冒泡，不允许被 except: pass 吃掉。"""


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ==================== 原子读写 ====================
def _default_state():
    return {
        "schema_version": SCHEMA_VERSION,
        "processed_urls": [],
        "news_pool": [],
        "last_morning_report_date": "",
        "last_summary_sent_at": "",
        "events": {},
    }


def ensure_shape(state):
    """补齐缺失字段，兼容旧版 state.json（只有 processed_urls 两三个键）。"""
    if not isinstance(state, dict):
        raise StateError("state.json 根节点不是 JSON 对象")
    base = _default_state()
    for key, value in base.items():
        if key not in state or state[key] is None:
            state[key] = value
    if not isinstance(state.get("processed_urls"), list):
        state["processed_urls"] = []
    if not isinstance(state.get("news_pool"), list):
        state["news_pool"] = []
    if not isinstance(state.get("events"), dict):
        state["events"] = {}
    state["schema_version"] = SCHEMA_VERSION
    return state


def load_state(path="state.json"):
    """读取状态。文件不存在返回默认状态；文件损坏抛 StateError（不再静默退回空）。"""
    if not os.path.exists(path):
        return _default_state()
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception as e:
        raise StateError(f"读取 {path} 失败: {type(e).__name__}: {e}") from e
    return ensure_shape(state)


def save_state_atomic(state, path="state.json"):
    """原子写入 + 回读校验。任何一步失败都抛 StateError。"""
    try:
        payload = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True)
    except Exception as e:
        raise StateError(f"状态无法序列化为 JSON: {type(e).__name__}: {e}") from e

    directory = os.path.dirname(os.path.abspath(path)) or "."
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)      # 同分区内原子替换
        tmp_path = None
    except Exception as e:
        raise StateError(f"写入 {path} 失败: {type(e).__name__}: {e}") from e
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    # 回读校验：确认落盘内容与内存状态一致，避免「以为存了其实没存」
    try:
        with open(path, "r", encoding="utf-8") as f:
            back = json.load(f)
    except Exception as e:
        raise StateError(f"写入后回读失败: {type(e).__name__}: {e}") from e
    if json.dumps(back, ensure_ascii=False, sort_keys=True) != json.dumps(state, ensure_ascii=False, sort_keys=True):
        raise StateError("回读校验失败：落盘内容与内存状态不一致")


# ==================== 事件与投递状态机 ====================
def _sha1(text):
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()


def event_fingerprint(title, url=""):
    """事件指纹：标题归一化后取哈希；标题过短时退回 URL。"""
    text = (title or "").strip()
    if " - " in text:
        text = text.rsplit(" - ", 1)[0]
    norm = "".join(ch for ch in text.lower() if ch.isalnum())
    if len(norm) < 12:
        norm = (url or "").strip().lower()
    return _sha1(norm)[:16]


def event_id_for(evidence_url, title=""):
    """事件 ID：优先用证据 URL 的哈希，没有 URL 时用标题指纹。"""
    key = (evidence_url or "").strip().lower()
    if not key:
        key = event_fingerprint(title)
    return _sha1(key)[:12]


def _prune_events(state, keep=200):
    """事件表按 updated_at 保留最近 keep 条，避免 state.json 无限膨胀。

    V1.2 任务 9 补充：未完结的事件（结果未知 / 待重试）不允许被裁掉，
    否则「等人工确认」的那条会随第 201 条新闻一起消失，等于静默丢弃。
    """
    events = state.get("events") or {}
    if len(events) <= keep:
        return
    pinned = {
        eid for eid, e in events.items()
        if e.get("delivery_status") in (RETRYABLE_FAILURE, DELIVERY_UNCERTAIN, PENDING_DELIVERY)
    }
    ordered = sorted(events.items(), key=lambda kv: kv[1].get("updated_at", ""), reverse=True)
    kept, dropped = {}, 0
    budget = keep
    for eid, event in ordered:
        if eid in pinned:
            kept[eid] = event
            continue
        if budget > 0:
            kept[eid] = event
            budget -= 1
        else:
            dropped += 1
    state["events"] = kept
    if dropped:
        print(f"[INFO] 事件表裁剪：保留 {len(kept)} 条（含 {len(pinned)} 条未完结），"
              f"丢弃 {dropped} 条已完结历史")


def record_event(state, evidence_url, title, evidence_status, delivery_status=DISCOVERED,
                 event_id=None, article_title=""):
    """创建或更新事件记录，返回事件字典。

    `event_id` 允许调用方指定：同一件新闻被多家媒体报道时，它们各自的 URL 会算出
    不同的事件 ID，于是同一件事在 state 里占了三四条记录（V1.2 任务 14）。
    由调用方先做一次「是不是同一件事」的判断，再把已有事件 ID 传进来复用。

    `article_title` 存的是**原始报道标题**（英文），与 `title`（模型改写后的事件标题）
    分开保存：事件级去重要拿它和新的英文候选标题比对，拿中文标题去比是对不上的。
    """
    events = state.setdefault("events", {})
    eid = event_id or event_id_for(evidence_url, title)
    now = _now_iso()
    event = events.get(eid)
    if event is None:
        event = {
            "event_id": eid,
            "event_fingerprint": event_fingerprint(title, evidence_url),
            "evidence_url": evidence_url or "",
            "article_title": (article_title or "")[:200],
            "evidence_status": evidence_status,
            "delivery_status": delivery_status,
            "telegram_message_id": None,
            "created_at": now,
            "updated_at": now,
            "attempts": 0,
            "last_error": "",
            "next_retry_at": "",
            "chunks": [],
            "pending_text": "",
            "title": (title or "")[:160],
        }
        events[eid] = event
        _prune_events(state)
    else:
        event["evidence_status"] = evidence_status
        event["updated_at"] = now
        if title:
            event["title"] = title[:160]
        if article_title:
            event["article_title"] = article_title[:200]
        if not event.get("evidence_url") and evidence_url:
            event["evidence_url"] = evidence_url
    return event


def transition(event, new_status, **fields):
    """执行一次状态迁移，非法迁移抛 StateError。"""
    current = event.get("delivery_status", DISCOVERED)
    if new_status not in _TRANSITIONS.get(current, set()):
        raise StateError(f"非法状态迁移: {current} -> {new_status}（event_id={event.get('event_id')}）")
    event["delivery_status"] = new_status
    event["updated_at"] = _now_iso()
    for key, value in fields.items():
        event[key] = value
    return event


def begin_delivery(event):
    """进入 PENDING_DELIVERY 时就把 attempts +1。

    这样即使进程在「已发送、未落盘」之间被杀，下次启动也能凭 attempts>0
    识别出「结果未知」，从而不自动重发，避免重复推送。
    """
    event["attempts"] = int(event.get("attempts", 0)) + 1
    return transition(event, PENDING_DELIVERY)


def mark_delivered(event, message_id):
    return transition(event, DELIVERED, telegram_message_id=message_id,
                      last_error="", next_retry_at="", pending_text="")


def mark_failed(event, error):
    """确定性失败（4xx / 被拉黑 / HTML 语法错误）：重试也不会成功。"""
    return transition(event, DELIVERY_FAILED, last_error=redact.sanitize(error)[:300],
                      pending_text="")


def retry_delay_minutes(attempts):
    """按已尝试次数取退避时长；超出表长就固定用最后一档。"""
    idx = max(1, int(attempts or 1)) - 1
    idx = min(idx, len(RETRY_BACKOFF_MINUTES) - 1)
    return RETRY_BACKOFF_MINUTES[idx]


def mark_retryable(event, error, now=None):
    """确定没送达、但值得再试：记下 next_retry_at，下一轮重新进投递队列。"""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    delay = retry_delay_minutes(event.get("attempts", 1))
    nxt = (now + datetime.timedelta(minutes=delay)).isoformat()
    return transition(event, RETRYABLE_FAILURE,
                      last_error=redact.sanitize(error)[:300], next_retry_at=nxt)


def mark_uncertain(event, error):
    """请求已发出但结果未知：不重发，等确定性结论。"""
    return transition(event, DELIVERY_UNCERTAIN,
                      last_error=redact.sanitize(error)[:300], next_retry_at="",
                      pending_text="")


def _parse_iso(text):
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def retry_due(event, now=None):
    """RETRYABLE_FAILURE 是否已到重试时间。没有 next_retry_at 视为立刻可重试。"""
    nxt = _parse_iso(event.get("next_retry_at"))
    if nxt is None:
        return True
    return (now or datetime.datetime.now(datetime.timezone.utc)) >= nxt


def blocks_rediscovery(event, now=None):
    """这条事件的历史记录是否应当阻止同一条新闻再次进入候选池（V1.2 任务 10）。

    旧逻辑无条件把所有事件指纹塞进 known_fp —— 于是「推送失败」也被当成
    「已经发过了」，一条因 Telegram 500 没发出去的新闻会被永久去重，
    除非人工去 state.json 里删记录，否则它再也不会被推送。

    现在的判定只有一条原则：**只有真的送达过（或正在送、或结果未知不敢重发），
    才算「处理过了」**。失败的要重新排队。
    """
    status = event.get("delivery_status")
    if status in (COMMITTED, DELIVERED, PENDING_DELIVERY, DELIVERY_UNCERTAIN):
        return True
    if status == RETRYABLE_FAILURE:
        return not retry_due(event, now)
    if status == DELIVERY_FAILED:
        # 确定性失败给有限次机会（凭证修好后仍然值得发），超过上限才彻底放弃
        return int(event.get("attempts", 0)) >= MAX_DELIVERY_ATTEMPTS
    return False


# ==================== 分段投递记录（V1.2 任务 11） ====================
def normalize_chunks(records):
    """把发送侧返回的分段结果规整成可持久化的结构。"""
    out = []
    for rec in records or []:
        if not isinstance(rec, dict):
            continue
        status = rec.get("delivery_status")
        out.append({
            "chunk_index": int(rec.get("chunk_index") or 0),
            "chunk_total": int(rec.get("chunk_total") or 0),
            "chunk_hash": str(rec.get("chunk_hash") or ""),
            "delivery_status": status if status in CHUNK_STATUSES else CHUNK_FAILED,
            "telegram_message_id": rec.get("telegram_message_id"),
            "attempts": int(rec.get("attempts") or 0),
            "last_error": redact.sanitize(rec.get("last_error") or "")[:300],
        })
    out.sort(key=lambda r: r["chunk_index"])
    return out


def record_chunks(event, records):
    """把本次分段结果写进事件。已送达段一律保留，绝不被新结果覆盖成未送达。"""
    merged = {r["chunk_index"]: r for r in normalize_chunks(event.get("chunks"))}
    for rec in normalize_chunks(records):
        old = merged.get(rec["chunk_index"])
        if old and old.get("delivery_status") == CHUNK_DELIVERED and old.get("telegram_message_id"):
            continue          # 已经确认送达的段不允许被覆盖
        merged[rec["chunk_index"]] = rec
    event["chunks"] = [merged[k] for k in sorted(merged)]
    event["updated_at"] = _now_iso()
    return event["chunks"]


def delivery_summary(event):
    """分段投递汇总，用于日志与告警。部分成功必须能被看见。"""
    chunks = normalize_chunks(event.get("chunks"))
    delivered = [c for c in chunks if c["delivery_status"] == CHUNK_DELIVERED]
    pending = [c for c in chunks if c["delivery_status"] in (CHUNK_RETRYABLE, CHUNK_UNCERTAIN)]
    failed = [c for c in chunks if c["delivery_status"] == CHUNK_FAILED]
    skipped = [c for c in chunks if c["delivery_status"] == CHUNK_NOT_ATTEMPTED]
    return {
        "total": len(chunks),
        "delivered_indexes": [c["chunk_index"] for c in delivered],
        "delivered_message_ids": [c["telegram_message_id"] for c in delivered],
        "pending_indexes": [c["chunk_index"] for c in pending],
        "failed_indexes": [c["chunk_index"] for c in failed],
        "not_attempted_indexes": [c["chunk_index"] for c in skipped],
        "all_delivered": bool(chunks) and len(delivered) == len(chunks),
        "partial": bool(delivered) and len(delivered) < len(chunks),
    }


def recovery_actions(state):
    """扫描上次运行遗留的中间态，返回需要恢复的动作列表。

    这是「Telegram 已发成功、但 GitHub 状态提交失败」的兜底：
    下次启动看到 DELIVERED 但没有 COMMITTED，只补提交、绝不重发。

    返回：[{"event_id", "action", "detail"}]
      · action="COMMIT_ONLY"      已确认送达，只需把状态推到 COMMITTED
      · action="UNCERTAIN"        卡在 PENDING_DELIVERY，发送结果未知，需人工/不自动重发
      · action="RETRY"            可重试失败且已到退避时间，下一轮自动重投
      · action="WAIT"             可重试失败但还没到退避时间
      · action="UNCERTAIN_REPORT" 结果未知，进独立恢复队列，只报告不重发
    """
    actions = []
    for eid, event in (state.get("events") or {}).items():
        status = event.get("delivery_status")
        if status == DELIVERED:
            actions.append({
                "event_id": eid,
                "action": "COMMIT_ONLY",
                "detail": f"消息已送达（message_id={event.get('telegram_message_id')}），仅需补提交状态",
            })
        elif status == PENDING_DELIVERY and int(event.get("attempts", 0)) > 0:
            actions.append({
                "event_id": eid,
                "action": "UNCERTAIN",
                "detail": "上次发送中断，请求可能已到达 Telegram；为避免重复推送不自动重发",
            })
        elif status == RETRYABLE_FAILURE:
            if retry_due(event):
                actions.append({
                    "event_id": eid,
                    "action": "RETRY",
                    "detail": f"可重试失败（第 {event.get('attempts', 0)} 次），已到退避时间，本轮重新投递",
                })
            else:
                actions.append({
                    "event_id": eid,
                    "action": "WAIT",
                    "detail": f"可重试失败，退避到 {event.get('next_retry_at')} 再投",
                })
        elif status == DELIVERY_UNCERTAIN:
            actions.append({
                "event_id": eid,
                "action": "UNCERTAIN_REPORT",
                "detail": "上次请求已发出但结果未知，已进入独立恢复队列，不自动重发",
            })
    return actions


def apply_recovery(state):
    """执行可自动完成的恢复动作。

    UNCERTAIN（卡在 PENDING_DELIVERY）过去被直接标成 DELIVERY_FAILED，
    等于把一个「可能已经送达」的消息谎报成「确定没送达」——下一轮如果按
    失败重投，用户就会收到重复推送。现在改成 DELIVERY_UNCERTAIN。
    """
    done = []
    for item in recovery_actions(state):
        event = state["events"][item["event_id"]]
        if item["action"] == "COMMIT_ONLY":
            transition(event, COMMITTED)
            done.append(item["event_id"])
        elif item["action"] == "UNCERTAIN":
            mark_uncertain(event, "上次发送中断，送达结果未知；为避免重复推送不自动重发")
            done.append(item["event_id"])
    return done


def uncertain_queue(state):
    """独立恢复队列：结果未知、需要人工确认的事件。"""
    return [
        event for event in (state.get("events") or {}).values()
        if event.get("delivery_status") == DELIVERY_UNCERTAIN
    ]
