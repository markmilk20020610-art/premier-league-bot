"""焦点战首发阵容极速监控（V2.0 §5）。

## 这个模块的边界（先写清楚，免得以后自己骗自己）

它**不预测首发**。它做的事只有三件：
  1. 在 T-120 / T-90 / T-75 三个窗口按不同频率去**读**官方/媒体页面；
  2. 从读到的文本里**解析**出 11 人 + 阵型，并做九项校验；
  3. 校验通过才允许标 `Official Lineup Confirmed`，否则最多标 `Reported`
     或只推「官方原始链接 + 待核验」。

## 「早于虎扑/懂球帝」怎么证明

不能靠嘴说。`SPEED_RECORD_FIELDS` 强制每次推送都记三个真实时间戳：

    official_published_at   官方页面/公告上的公布时间（能从页面取到就取，取不到留空）
    first_detected_at       我方第一次检测到该版本的时刻（本地时钟，UTC）
    pushed_at               实际发出 Telegram 的时刻

只有这三个都在，才能算「领先 X 秒」。缺任何一个就记 `speed_unproven`，
**不允许在文案里写「全网最快」**。这是本模块唯一的营销口径纪律。

## 为什么不能 30 秒无脑轮询

`next_interval()` 把频率交给三个约束：当前阶段、连续失败次数、配置上限。
连续失败会指数退避，避免把官方站点打到风控（真被封了，监控就彻底瞎了，
比慢 30 秒糟糕得多）。
"""

import datetime
import hashlib
import re

import focus_config as cfg

# =============================================================================
# 常量
# =============================================================================
STATUS = cfg.LINEUP_STATUS
ST_UNKNOWN = "unknown"
ST_NOT_PUBLISHED = "not_published"
ST_PREDICTED = "predicted"
ST_REPORTED = "reported"
ST_UNCONFIRMED = "unconfirmed"
ST_CONFIRMED = "official_confirmed"

PHASE_IDLE = "idle"
PHASE_WARMUP = "warmup"
PHASE_INTENSIFY = "intensify"
PHASE_CRITICAL = "critical"
PHASE_POST = "post"

# 推送动作（七步流程的产出）
ACT_PUBLISH_CONFIRMED = "publish_confirmed"
ACT_PUBLISH_REPORTED = "publish_reported"
ACT_PUBLISH_LINK_ONLY = "publish_link_only"
ACT_SKIP = "skip"

SPEED_RECORD_FIELDS = (
    "match_id", "team", "official_published_at", "first_detected_at", "pushed_at",
)

# 九项校验的编号与名称（顺序即输出顺序）
NINE_VALIDATIONS = (
    ("V1", "首发人数必须为 11"),
    ("V2", "阵型格式合法且与分组人数自洽"),
    ("V3", "球员姓名格式合法"),
    ("V4", "无重复球员"),
    ("V5", "球员归属该队（需阵容名单，缺失时降级为未核验）"),
    ("V6", "至少包含 1 名门将"),
    ("V7", "与对手阵容不重叠"),
    ("V8", "来源层级足以支撑所标状态"),
    ("V9", "与已确认版本不冲突"),
)

POSITION_TOKENS = frozenset({
    "gk", "goalkeeper", "df", "defender", "mf", "midfielder", "fw", "forward",
    "sub", "subs", "substitute", "substitutes", "bench", "manager", "coach",
    "captain", "c",
})

_HEADER_RE = re.compile(
    r"(?:starting\s*(?:xi|line[- ]?up|eleven)|confirmed\s*(?:xi|line[- ]?up)|"
    r"team\s*news|首发(?:阵容|名单)?)\s*[:：]?",
    re.I)

_STOP_RE = re.compile(r"(?:substitutes?|bench|替补|subs\s*:)", re.I)


# =============================================================================
# 一、时间窗口与轮询频率
# =============================================================================
def _parse_utc(s):
    if isinstance(s, datetime.datetime):
        return s
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.datetime.strptime(str(s).strip(), fmt)
        except (ValueError, TypeError):
            continue
    return None


def phase_of(kickoff_utc, now):
    """当前处于哪个监控阶段。"""
    kick = _parse_utc(kickoff_utc)
    if kick is None:
        return PHASE_IDLE
    p = cfg.LINEUP_POLICY
    minutes = (kick - now).total_seconds() / 60
    if minutes < -p["post_publish_recheck_minutes"]:
        return PHASE_IDLE
    if minutes < 0:
        return PHASE_POST
    if minutes <= p["critical_minutes"]:
        return PHASE_CRITICAL
    if minutes <= p["intensify_minutes"]:
        return PHASE_INTENSIFY
    if minutes <= p["warmup_minutes"]:
        return PHASE_WARMUP
    return PHASE_IDLE


def next_interval(kickoff_utc, now, consecutive_failures=0):
    """下一次该等多久再轮询（秒）。

    返回 (seconds, phase, reason)。连续失败会指数退避——**这是防封的核心**：
    被官方站点风控之后，30 秒轮询只会让封禁持续更久。
    """
    p = cfg.LINEUP_POLICY
    phase = phase_of(kickoff_utc, now)
    base = p["interval_seconds"][phase]
    if consecutive_failures <= 0:
        return base, phase, "正常节拍"

    b = p["backoff"]
    mult = b["multiplier"] ** min(consecutive_failures,
                                 b["max_consecutive_failures"])
    seconds = min(base * mult, b["max_interval_seconds"])
    return seconds, phase, (
        f"连续失败 {consecutive_failures} 次，退避 ×{mult}"
        f"（上限 {b['max_interval_seconds']}s）")


def is_monitoring_window(kickoff_utc, now):
    return phase_of(kickoff_utc, now) in (
        PHASE_WARMUP, PHASE_INTENSIFY, PHASE_CRITICAL, PHASE_POST)


# =============================================================================
# 二、解析（只做文本解析，不做「预测」）
# =============================================================================
def _clean_name(raw):
    n = re.sub(r"\s+", " ", (raw or "").strip(" .,;:·-—"))
    n = re.sub(r"\((?:c|captain)\)$", "", n, flags=re.I).strip()
    n = re.sub(r"\s+(?:GK|DF|MF|FW)$", "", n, flags=re.I).strip()
    return n


def parse_starting_xi(text):
    """从一段文本里抽首发 11 人 + 阵型。

    支持的形态（实测于俱乐部官网 / BBC 的 team news 文案）：

        Starting XI: Raya; Timber, Saliba, Gabriel, Calafiori;
                     Rice, Merino, Odegaard; Saka, Havertz, Martinelli
        Substitutes: ...

    返回 dict：
        ok / players / formation / groups / reason

    **解析失败一律返回 ok=False**，绝不返回「大概是这样」的名单。
    """
    if not text or not isinstance(text, str):
        return {"ok": False, "players": [], "formation": "", "groups": [],
                "reason": "无文本"}

    # 中文文案用的是全角分隔符（；、，），不统一成半角会整段被当成一个"球员"。
    text = (text.replace("；", ";").replace("、", ",")
                .replace("，", ",").replace("：", ":"))

    # 取**最后一个**标题：真实文案常见 "Arsenal team news\nStarting XI: ..."，
    # 若取第一个（team news）会把 "Starting XI: Raya" 当成一名球员。
    matches = list(_HEADER_RE.finditer(text))
    if not matches:
        return {"ok": False, "players": [], "formation": "", "groups": [],
                "reason": "未找到首发段落标题"}
    m = matches[-1]
    tail = text[m.end():]
    # 兜底：标题里可能还残留一段（例如 "Starting XI:" 前面还有 "Confirmed"）
    tail = _HEADER_RE.sub("", tail, count=1)
    stop = _STOP_RE.search(tail)
    segment = tail[:stop.start()] if stop else tail
    segment = segment.strip(" \n\r\t:：-—|")

    if not segment:
        return {"ok": False, "players": [], "formation": "", "groups": [],
                "reason": "首发段落为空"}

    # 优先按分号分组（官网写法）；没有分号时按逗号平铺
    raw_groups = [g for g in re.split(r"[;|]", segment) if g.strip()]
    groups = []
    for g in raw_groups:
        names = [_clean_name(x) for x in re.split(r",|\band\b", g)]
        names = [n for n in names if n]
        if names:
            groups.append(names)

    if not groups:
        return {"ok": False, "players": [], "formation": "", "groups": [],
                "reason": "分组后没有球员"}

    players = [n for g in groups for n in g]
    formation = ""
    if len(groups) >= 2:
        # 第一组是门将，其余组的人数拼成阵型
        formation = "-".join(str(len(g)) for g in groups[1:])
    return {"ok": True, "players": players, "formation": formation,
            "groups": groups, "reason": ""}


def players_hash(players):
    """阵容指纹：用于判断「是不是同一版」。顺序不同视为同一版。"""
    norm = sorted(p.strip().lower() for p in players if p and p.strip())
    return hashlib.sha256("|".join(norm).encode("utf-8")).hexdigest()[:16]


# =============================================================================
# 三、九项校验
# =============================================================================
def _formation_ok(formation, groups):
    if not formation:
        return False, "无阵型"
    parts = formation.split("-")
    if not all(p.isdigit() for p in parts):
        return False, f"阵型格式非法：{formation}"
    if len(parts) < 2:
        return False, f"阵型段数过少：{formation}"
    if sum(int(p) for p in parts) != 10:
        return False, f"阵型人数合计 {sum(int(p) for p in parts)} ≠ 10（不含门将）"
    if groups and [len(g) for g in groups[1:]] != [int(p) for p in parts]:
        return False, "阵型与实际分组人数不一致"
    return True, ""


def validate_lineup(record, opponent=None, squad=None, previous_confirmed=None):
    """九项校验。返回问题列表（空 = 全部通过）。

    每个问题都是 dict：{"code","name","level","detail"}
    level=block 表示「不得标官方确认」，warn 表示「可发布但需标注不确定」。
    """
    problems = []

    def add(code, level, detail):
        name = dict(NINE_VALIDATIONS)[code]
        problems.append({"code": code, "name": name,
                         "level": level, "detail": detail})

    players = record.get("players") or []
    formation = record.get("formation") or ""
    groups = record.get("groups") or []

    # V1 人数
    if len(players) != 11:
        add("V1", "block", f"首发 {len(players)} 人，应为 11 人")

    # V2 阵型
    ok, why = _formation_ok(formation, groups)
    if not ok:
        add("V2", "block" if len(players) == 11 else "warn", why)

    # V3 姓名格式
    bad = [p for p in players
           if not p or len(p) > 40 or re.search(r"\d", p)
           or p.strip().lower() in POSITION_TOKENS]
    if bad:
        add("V3", "block", f"疑似非球员姓名：{bad[:3]}")

    # V4 重复
    lowered = [p.strip().lower() for p in players]
    dups = sorted({p for p in lowered if lowered.count(p) > 1})
    if dups:
        add("V4", "block", f"重复球员：{dups}")

    # V5 归属
    if squad:
        squad_l = {s.strip().lower() for s in squad}
        outsiders = [p for p in players if p.strip().lower() not in squad_l]
        if outsiders:
            add("V5", "block", f"不在该队名单内：{outsiders[:3]}")
    else:
        add("V5", "warn", "无该队阵容名单，球员归属未核验")

    # V6 门将
    if groups and len(groups[0]) != 1:
        add("V6", "warn", "第一组不是单人，无法确认门将位置")
    elif not groups:
        add("V6", "warn", "无分组信息，无法确认门将")

    # V7 与对手不重叠
    if opponent:
        opp = {p.strip().lower() for p in (opponent.get("players") or [])}
        overlap = [p for p in players if p.strip().lower() in opp]
        if overlap:
            add("V7", "block", f"同一球员同时出现在两队：{overlap[:3]}")

    # V8 来源层级
    tier = record.get("source_tier")
    status = record.get("status")
    if status == ST_CONFIRMED and tier != 1:
        add("V8", "block", f"来源层级 {tier} 不足以支撑官方确认")
    if status == ST_CONFIRMED and not record.get("source_url"):
        add("V8", "block", "标为官方确认却没有来源链接")

    # V9 与已确认版本冲突
    if previous_confirmed and previous_confirmed.get("players"):
        if players_hash(previous_confirmed["players"]) != players_hash(players):
            add("V9", "block",
                "与已确认版本不一致；官方更正需人工确认后才能覆盖")

    return problems


def decide_status(source_tier, parse_ok, problems):
    """由来源层级 + 解析结果 + 校验结论决定状态。

    只有「一级来源 + 解析成功 + 无 block 问题」才能到 official_confirmed。
    其余情况最多 reported（三级媒体）/ unconfirmed（四级聚合）。
    """
    blocking = [p for p in problems if p["level"] == "block"]
    if not parse_ok:
        return ST_UNCONFIRMED
    if source_tier == 1 and not blocking:
        return ST_CONFIRMED
    if source_tier in (1, 2, 3) and not blocking:
        return ST_REPORTED
    if source_tier == 3:
        return ST_REPORTED if not blocking else ST_UNCONFIRMED
    return ST_UNCONFIRMED


def decide_action(status, problems, parse_ok, official_url=""):
    """决定推送动作。解析失败或存在阻断问题时**不自动发名单**。"""
    blocking = [p for p in problems if p["level"] == "block"]
    if not parse_ok or blocking:
        return ACT_PUBLISH_LINK_ONLY if official_url else ACT_SKIP
    if status == ST_CONFIRMED:
        return ACT_PUBLISH_CONFIRMED
    if status == ST_REPORTED:
        return ACT_PUBLISH_REPORTED
    return ACT_SKIP


# =============================================================================
# 四、七步推送流程
# =============================================================================
FLOW_STEPS = (
    ("detect", "检测到候选首发"),
    ("parse", "解析 11 人与阵型"),
    ("validate", "九项校验"),
    ("decide", "状态判定"),
    ("dedupe", "去重与冲突检查"),
    ("push", "推送（或仅推官方链接）"),
    ("record", "记录三个时间戳"),
)


def run_push_flow(match, team, text, source_tier, source_id, source_url,
                  now, opponent=None, squad=None, previous_confirmed=None,
                  official_published_at="", already_pushed_hash="",
                  competitors_seen_at=""):
    """执行七步流程，返回结构化结果（含每个时间戳）。

    `competitors_seen_at` 是**可选**的对手媒体出现时间。没有它就只报
    「我方检测时间」，绝不写「我们更快」。
    """
    steps = []

    def step(key, status, detail):
        steps.append({"step": key, "name": dict(FLOW_STEPS)[key],
                      "status": status, "detail": detail})

    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    # 1 检测
    step("detect", "done" if text else "skipped",
         f"来源 {source_id}（tier {source_tier}）" if text else "无内容")

    # 2 解析
    parsed = parse_starting_xi(text)
    step("parse", "done" if parsed["ok"] else "failed", parsed["reason"] or
         f"{len(parsed['players'])} 人，阵型 {parsed['formation']}")

    # 3 校验
    record = {
        "match_id": match.get("match_id", ""),
        "team": team,
        "players": parsed["players"],
        "formation": parsed["formation"],
        "groups": parsed["groups"],
        "source_tier": source_tier,
        "source_id": source_id,
        "source_url": source_url,
        "status": "",           # 下面判定
        "raw_hash": players_hash(parsed["players"]) if parsed["ok"] else "",
    }
    problems = validate_lineup(record, opponent=opponent, squad=squad,
                               previous_confirmed=previous_confirmed)
    blocking = [p for p in problems if p["level"] == "block"]
    step("validate", "done" if not blocking else "failed",
         f"{len(problems)} 项问题（阻断 {len(blocking)}）："
         + ("；".join(f"{p['code']} {p['detail']}" for p in problems[:3])
            if problems else "全部通过"))

    # 4 状态
    status = decide_status(source_tier, parsed["ok"], problems)
    record["status"] = status
    step("decide", "done", f"{status}（{STATUS[status]}）")

    # 5 去重
    same_as_before = bool(record["raw_hash"]) and record["raw_hash"] == already_pushed_hash
    step("dedupe", "skipped" if same_as_before else "done",
         "与已推送版本相同，跳过" if same_as_before else "新版本")

    # 6 推送
    action = ACT_SKIP if same_as_before else decide_action(
        status, problems, parsed["ok"], official_url=source_url)
    step("push", "done" if action != ACT_SKIP else "skipped", action)

    # 7 时间戳
    speed = {
        "match_id": record["match_id"],
        "team": team,
        "official_published_at": official_published_at or "",
        "first_detected_at": now_iso,
        "pushed_at": now_iso if action in (ACT_PUBLISH_CONFIRMED,
                                           ACT_PUBLISH_REPORTED) else "",
    }
    proof = speed_proof(speed, competitors_seen_at)
    step("record", "done" if proof["proven"] else "partial", proof["note"])

    return {
        "match_id": record["match_id"],
        "team": team,
        "record": record,
        "problems": problems,
        "status": status,
        "action": action,
        "steps": steps,
        "speed": speed,
        "speed_proof": proof,
        "message": render_lineup_message(record, problems, action, proof),
    }


# =============================================================================
# 五、速度证明
# =============================================================================
def speed_proof(speed, competitors_seen_at=""):
    """判断「领先」是否被真实时间戳证明。

    三个时间戳缺任何一个 → proven=False，文案里不许出现速度宣称。
    """
    for f in SPEED_RECORD_FIELDS:
        if f not in speed:
            return {"proven": False, "lead_seconds": None,
                    "note": f"缺少时间戳字段 {f}，不得宣称速度优势"}
    if not speed.get("first_detected_at") or not speed.get("pushed_at"):
        return {"proven": False, "lead_seconds": None,
                "note": "我方检测/推送时间缺失，不得宣称速度优势"}
    if not competitors_seen_at:
        return {"proven": False, "lead_seconds": None,
                "note": (f"我方于 {speed['first_detected_at']} 检测到并 "
                         f"{speed['pushed_at']} 推送；未采集到对手媒体的出现时间，"
                         f"因此不宣称领先")}
    ours = _parse_utc(speed["first_detected_at"])
    theirs = _parse_utc(competitors_seen_at)
    if not ours or not theirs:
        return {"proven": False, "lead_seconds": None,
                "note": "对手时间戳无法解析，不得宣称速度优势"}
    lead = (theirs - ours).total_seconds()
    return {
        "proven": lead > 0,
        "lead_seconds": int(lead),
        "note": (f"我方 {speed['first_detected_at']} 检测，对手 {competitors_seen_at} 出现，"
                 f"领先 {int(lead)} 秒" if lead > 0
                 else f"未领先（{int(lead)} 秒），不得宣称速度优势"),
    }


# =============================================================================
# 六、文案
# =============================================================================
_STATUS_LABEL = {
    ST_CONFIRMED: "✅ Official Lineup Confirmed（官方已确认）",
    ST_REPORTED: "⚠️ Reported Lineup（媒体披露，未获官方确认）",
    ST_PREDICTED: "⚠️ Predicted Lineup（预测，非事实）",
    ST_UNCONFIRMED: "❔ Unconfirmed（未核实）",
    ST_NOT_PUBLISHED: "官方尚未公布",
    ST_UNKNOWN: "未知",
}


def render_lineup_message(record, problems, action, proof):
    """渲染推送文案。

    纪律：
      · 只有 official_confirmed 才允许出现「官方确认」
      · 解析失败只发链接，不发名单
      · 没被证明的速度优势一个字都不写
    """
    head = f"【首发】{record['team']}｜{record['match_id']}"
    lines = [head]

    if action == ACT_PUBLISH_LINK_ONLY:
        lines.append("解析未通过校验，暂不发布名单。")
        lines.append(f"官方原文：{record.get('source_url') or '（无）'}")
        for p in [p for p in problems if p["level"] == "block"][:3]:
            lines.append(f" · {p['code']} {p['detail']}")
        lines.append("状态：待核验（Unconfirmed）")
        return "\n".join(lines)

    label = _STATUS_LABEL.get(record["status"], record["status"])
    lines.append(f"状态：{label}")
    if record.get("formation"):
        lines.append(f"阵型：{record['formation']}")
    lines.append("首发：" + "、".join(record.get("players") or []))
    if record.get("source_url"):
        lines.append(f"来源：{record['source_id']} {record['source_url']}")

    warns = [p for p in problems if p["level"] == "warn"]
    if warns:
        lines.append("提示：" + "；".join(f"{p['code']} {p['detail']}" for p in warns))

    # 只有 proven 才写速度
    if proof.get("proven"):
        lines.append(f"⏱ {proof['note']}")
    return "\n".join(lines)


def validate_speed_record(speed):
    problems = []
    for f in SPEED_RECORD_FIELDS:
        if f not in speed:
            problems.append(f"缺少字段 {f}")
    if speed.get("status") == ST_CONFIRMED and not speed.get("official_published_at"):
        problems.append("标为官方确认却没有官方公布时间")
    return problems
