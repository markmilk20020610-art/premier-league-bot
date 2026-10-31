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

SCHEMA_VERSION = 2

# ==================== 投递状态机 ====================
DISCOVERED = "DISCOVERED"
VERIFIED = "VERIFIED"
PENDING_DELIVERY = "PENDING_DELIVERY"
DELIVERED = "DELIVERED"
COMMITTED = "COMMITTED"
DELIVERY_FAILED = "DELIVERY_FAILED"

# 允许的状态迁移。COMMITTED 是终态。
_TRANSITIONS = {
    DISCOVERED: {VERIFIED, DELIVERY_FAILED},
    VERIFIED: {PENDING_DELIVERY, DELIVERY_FAILED},
    PENDING_DELIVERY: {DELIVERED, DELIVERY_FAILED},
    DELIVERY_FAILED: {PENDING_DELIVERY, DELIVERED},
    DELIVERED: {COMMITTED},
    COMMITTED: set(),
}


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
    """事件表按 updated_at 保留最近 keep 条，避免 state.json 无限膨胀。"""
    events = state.get("events") or {}
    if len(events) <= keep:
        return
    ordered = sorted(events.items(), key=lambda kv: kv[1].get("updated_at", ""), reverse=True)
    state["events"] = dict(ordered[:keep])


def record_event(state, evidence_url, title, evidence_status, delivery_status=DISCOVERED):
    """创建或更新事件记录，返回事件字典。"""
    events = state.setdefault("events", {})
    eid = event_id_for(evidence_url, title)
    now = _now_iso()
    event = events.get(eid)
    if event is None:
        event = {
            "event_id": eid,
            "event_fingerprint": event_fingerprint(title, evidence_url),
            "evidence_url": evidence_url or "",
            "evidence_status": evidence_status,
            "delivery_status": delivery_status,
            "telegram_message_id": None,
            "created_at": now,
            "updated_at": now,
            "attempts": 0,
            "last_error": "",
            "title": (title or "")[:160],
        }
        events[eid] = event
        _prune_events(state)
    else:
        event["evidence_status"] = evidence_status
        event["updated_at"] = now
        if title:
            event["title"] = title[:160]
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
    return transition(event, DELIVERED, telegram_message_id=message_id, last_error="")


def mark_failed(event, error):
    return transition(event, DELIVERY_FAILED, last_error=str(error)[:300])


def recovery_actions(state):
    """扫描上次运行遗留的中间态，返回需要恢复的动作列表。

    这是「Telegram 已发成功、但 GitHub 状态提交失败」的兜底：
    下次启动看到 DELIVERED 但没有 COMMITTED，只补提交、绝不重发。

    返回：[{"event_id", "action", "detail"}]
      · action="COMMIT_ONLY"  已确认送达，只需把状态推到 COMMITTED
      · action="UNCERTAIN"    卡在 PENDING_DELIVERY，发送结果未知，需人工/不自动重发
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
                "detail": "上次发送中断，送达结果未知；为避免重复推送不自动重发",
            })
    return actions


def apply_recovery(state):
    """把 DELIVERED 事件补推为 COMMITTED；UNCERTAIN 只标记失败等待下一轮人工处理。"""
    done = []
    for item in recovery_actions(state):
        event = state["events"][item["event_id"]]
        if item["action"] == "COMMIT_ONLY":
            transition(event, COMMITTED)
            done.append(item["event_id"])
        elif item["action"] == "UNCERTAIN":
            event["last_error"] = "上次发送中断，送达结果未知"
            event["delivery_status"] = DELIVERY_FAILED
            event["updated_at"] = _now_iso()
            done.append(item["event_id"])
    return done
