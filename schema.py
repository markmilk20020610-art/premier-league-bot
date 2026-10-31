"""模型输出严格校验（阶段 1.3）

任何一项不通过，主程序都不得自动推送。
这里的定位是「守门员」：宁可少推一条，也不允许把半成品或伪造字段送进 Telegram。
"""

import re
from urllib.parse import urlparse

import evidence as ev

# ==================== 评级枚举 ====================
RATING_LABELS = {
    "黑标": "🐦‍⬛【官方严谨·黑标警示】",
    "Tier 1": "✨【金哨认证·绝对真理】(Tier 1)",
    "Tier 2": "⚡【闪电突袭·跟队独家】(Tier 2)",
    "Tier 3": "💛【吃瓜狂欢·黄牌警告】(Tier 3)",
}

# 兼容模型输出的各种写法：Tier1 / tier 1 / (Tier 1) / 金哨 / 吃瓜 …
_RATING_PATTERNS = (
    ("黑标", re.compile(r"黑标|black\s*label", re.I)),
    ("Tier 1", re.compile(r"tier\s*1|金哨", re.I)),
    ("Tier 2", re.compile(r"tier\s*2|闪电", re.I)),
    ("Tier 3", re.compile(r"tier\s*3|吃瓜|黄牌", re.I)),
)


def normalize_rating(value):
    """把任意写法的评级归一成 key；无法识别返回 None。"""
    text = str(value or "")
    for key, pattern in _RATING_PATTERNS:
        if pattern.search(text):
            return key
    return None


def _is_http_url(value):
    try:
        p = urlparse(str(value or ""))
    except Exception:
        return False
    return p.scheme in ("http", "https") and bool(p.netloc)


def _nonempty_str(value, min_len=1):
    return isinstance(value, str) and len(value.strip()) >= min_len


# ==================== 突发扫描 Schema ====================
SCAN_REQUIRED_STRINGS = (
    ("event_title", 2),
    ("fact_summary", 8),
    ("draft_a", 8),
    ("draft_b", 8),
    ("draft_c", 8),
    ("cover_blueprint", 2),
    ("image_prompt", 4),
    ("banner_overlay_text", 1),
)


def validate_scan(data):
    """校验单条突发新闻输出。

    返回 (ok, errors, normalized)。normalized 里 evidence_sources / evidence_status
    保持程序计算值，模型自报的同名字段在校验前已被 evidence 模块覆盖。
    """
    errors = []

    if not isinstance(data, dict):
        return False, ["输出不是 JSON 对象"], {}

    if not isinstance(data.get("has_valid_news"), bool):
        errors.append("has_valid_news 必须是布尔值")
        return False, errors, data

    if data["has_valid_news"] is False:
        return True, [], data

    for field, min_len in SCAN_REQUIRED_STRINGS:
        if not _nonempty_str(data.get(field), min_len):
            errors.append(f"{field} 缺失或过短（要求 ≥{min_len} 字）")

    if not _is_http_url(data.get("evidence_url")):
        errors.append("evidence_url 不是合法的 http(s) 链接")

    if not isinstance(data.get("evidence_sources"), list) or not data["evidence_sources"]:
        errors.append("evidence_sources 必须是非空列表（由程序计算）")

    if data.get("evidence_status") not in ev.ALL_STATUSES:
        errors.append(f"evidence_status 不合法：{data.get('evidence_status')!r}")

    rating_key = normalize_rating(data.get("var_rating"))
    if rating_key is None:
        errors.append(f"var_rating 不在允许枚举内：{data.get('var_rating')!r}")
    else:
        data["var_rating_key"] = rating_key

    if not isinstance(data.get("evidence_sources_count"), int):
        errors.append("evidence_sources_count 必须是整数（由程序计算）")

    return (len(errors) == 0), errors, data


# ==================== 早报 Schema ====================
# V1.2 任务 6：每个 top_event 必须能追溯到程序侧的已验证事件。
# 缺任何一项都说明模型在自己造事件，直接拒绝整篇早报。
SUMMARY_EVENT_FIELDS = (
    ("event_id", 1),        # 必须来自程序给出的 verified event_id 列表
    ("title", 2),
    ("one_line", 2),
    ("evidence_status", 1),  # 程序覆盖，模型无权自报
    ("attribution", 1),      # 来源名称 + 等级
    ("published_at", 1),     # 原始发布时间
)


def validate_summary(data, allowed_event_ids=None):
    """校验每日汇总输出。

    `allowed_event_ids` 是程序算出的「已验证事件」集合（V1.2 任务 5/6）。
    传入后，任何不在集合内的 event_id 都会被判为不合格——
    这是防止模型「自己加一条新闻进早报」的最后一道门。
    """
    errors = []

    if not isinstance(data, dict):
        return False, ["输出不是 JSON 对象"], {}

    if not isinstance(data.get("has_valid_news"), bool):
        errors.append("has_valid_news 必须是布尔值")
        return False, errors, data

    if data["has_valid_news"] is False:
        return True, [], data

    for field, min_len in (
        ("note_title", 2),
        ("hook", 4),
        ("body", 60),
        ("tags", 2),
        ("fact_check_note", 2),
        ("cover_blueprint", 2),
        ("image_prompt", 4),
        ("banner_overlay_text", 1),
    ):
        if not _nonempty_str(data.get(field), min_len):
            errors.append(f"{field} 缺失或过短（要求 ≥{min_len} 字）")

    events = data.get("top_events")
    if not isinstance(events, list) or not events:
        errors.append("top_events 必须是非空列表")
        return (len(errors) == 0), errors, data

    allowed = set(allowed_event_ids) if allowed_event_ids is not None else None
    for idx, item in enumerate(events, 1):
        if not isinstance(item, dict):
            errors.append(f"top_events 第 {idx} 项不是对象")
            continue
        for field, min_len in SUMMARY_EVENT_FIELDS:
            if not _nonempty_str(item.get(field), min_len):
                errors.append(f"top_events 第 {idx} 项缺少 {field}")

        status = item.get("evidence_status")
        if status and status not in ev.ALL_STATUSES:
            errors.append(f"top_events 第 {idx} 项 evidence_status 不合法：{status!r}")

        urls = item.get("source_urls")
        if not isinstance(urls, list) or not urls:
            errors.append(f"top_events 第 {idx} 项 source_urls 必须是非空列表")
        else:
            for u in urls:
                if not _is_http_url(u):
                    errors.append(f"top_events 第 {idx} 项 source_urls 含非法链接：{u!r}")
                    break

        eid = item.get("event_id")
        if allowed is not None and _nonempty_str(eid, 1) and eid not in allowed:
            errors.append(f"top_events 第 {idx} 项 event_id={eid!r} 不在已验证事件集合内")

    return (len(errors) == 0), errors, data


def describe_errors(errors, limit=4):
    """把错误列表压成一行，方便写进 [SKIP] 日志与 Actions 注解。"""
    if not errors:
        return ""
    head = "; ".join(errors[:limit])
    if len(errors) > limit:
        head += f"; …共 {len(errors)} 项"
    return head
