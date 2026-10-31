"""焦点战裁判情报系统（V2.0 §4）。

## 这个模块的三条硬纪律

1. **不假设「任命固定提前一周公布」。** 免费源能拿到的只是「此刻 fixtures.csv 里
   已署名了哪些场次」。所以本模块做的是**实测审计**：每轮记录「距开球还有多少天时
   第一次看到署名」，长期累积成真实分布（`audit_lead_time`）。在积累够之前，
   报告里只会写「本次观测覆盖 N 场，其中 M 场已署名」，不写任何规律性断言。

2. **不能把「黄牌多」当成「裁判有问题」。** 统计输出里每一条都带
   `sample_size` / `can_conclude` / `interpretation_warning`。样本不足时
   `can_conclude=False`，只出原始计数，不出结论。

3. **不能把球迷评论升级成官方确认错误。** 争议记录必须带 A–E 证据等级，
   只有 A/B/C 能进 `facts`，D/E 只能进 `leads`（线索），且推送文案里
   不得把 D 级写成「误判」。

## 数据来源与能力边界（实测）

`football-data.co.uk` 的赛季结果 CSV 提供：**主裁姓名** + 双方黄牌/红牌数。
它**不提供**：助理裁判、第四官员、VAR、AVAR。

因此本模块的 `role` 字段虽然区分 5 种角色，但当前只有 `Referee` 有数据；
其余角色一律记 `appointment_status="not_available_from_source"`，
**绝不用「VAR 大概是某某」这种推测填坑**。这条边界写进每份报告的
`information_gaps`，让读报告的人知道「我们不知道什么」。
"""

import datetime
import re

import focus_config as cfg

# =============================================================================
# 角色与状态枚举
# =============================================================================
ROLE_REFEREE = "Referee"
ROLE_ASSISTANT = "Assistant Referee"
ROLE_FOURTH = "Fourth Official"
ROLE_VAR = "VAR"
ROLE_AVAR = "AVAR"

ROLES = (ROLE_REFEREE, ROLE_ASSISTANT, ROLE_FOURTH, ROLE_VAR, ROLE_AVAR)

APPT_CONFIRMED = "confirmed"
APPT_PENDING = "pending"
APPT_UNAVAILABLE = "not_available_from_source"

# 任命记录字段（§4 要求）
APPOINTMENT_FIELDS = (
    "referee_id", "referee_name", "match_id", "competition",
    "appointment_date", "role", "appointment_status",
    "source_id", "source_url", "published_at", "first_seen_at", "verified_at",
)

SOURCE_APPOINTMENT = "football_data_fixtures"
SOURCE_STATS = "football_data_results"
SOURCE_URL_FIXTURES = "https://www.football-data.co.uk/fixtures.csv"


# =============================================================================
# 一、裁判身份归一化
# =============================================================================
def _slug(name):
    s = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    return s or "unknown_referee"


def _name_tokens(name):
    """把 'S Attwell' / 'Stuart Attwell' / 'S. Attwell' 拆成 (首字母, 姓)。"""
    raw = (name or "").replace(".", " ").strip()
    parts = [p for p in re.split(r"\s+", raw) if p]
    if not parts:
        return None, None
    if len(parts) == 1:
        return None, parts[0].lower()
    return parts[0][0].lower(), parts[-1].lower()


_WATCH_INDEX = None


def _watch_index():
    """建「姓氏+首字母」→ 观察名单条目的索引。

    只按姓氏匹配是危险的（英超有两个 Brooks 的可能性存在），所以必须
    「首字母 + 姓氏」双匹配；只有姓氏时退化为姓氏匹配但标记 low_confidence。
    """
    global _WATCH_INDEX
    if _WATCH_INDEX is None:
        idx = {}
        for w in cfg.WATCH_REFEREES:
            for alias in w["csv_names"] + w["aliases"] + (w["display_name"],):
                ini, sur = _name_tokens(alias)
                idx[(ini, sur)] = w
        _WATCH_INDEX = idx
    return _WATCH_INDEX


def normalize_referee(name):
    """裁判姓名 → (referee_id, display_name, on_watchlist, confidence)。

    · 命中观察名单（首字母 + 姓氏，或登记过的别名）→ 用名单里的 id 与正式全名
    · 未命中 → 用姓名的 slug 作为 id，confidence="exact_name"

    ⚠️ 刻意**不做**「仅姓氏」的模糊兜底：英超同一赛季可能出现同姓裁判，
    按姓氏硬匹配会把 A 的执法数据挂到 B 头上，而这类错误在推送里看起来
    完全正常，是最难被发现的一种。宁可不匹配（confidence=exact_name、
    on_watchlist=False），也不冒这个风险。
    """
    ini, sur = _name_tokens(name)
    if not sur:
        return "unknown_referee", (name or "").strip(), False, "unknown"
    hit = _watch_index().get((ini, sur))
    if hit:
        return hit["referee_id"], hit["display_name"], True, "watchlist"
    return _slug(name), (name or "").strip(), False, "exact_name"


def is_watch_referee(name):
    _, _, on_list, _ = normalize_referee(name)
    return on_list


# =============================================================================
# 二、裁判执法数据库（含样本量与统计口径）
# =============================================================================
def build_referee_stats(result_rows, seasons=None):
    """从结果行聚合裁判统计。

    返回 {referee_id: stats}。每条 stats 都带：
      · sample_size / can_conclude / interpretation_warning
      · date_range（真实首末日期，不是「最近 N 轮」这种模糊说法）
      · role —— 明确写死 Referee，因为 CSV 的 Referee 列就是主裁
      · metric_note —— 原样来自 focus_config.REFEREE_STATS_POLICY
    """
    policy = cfg.REFEREE_STATS_POLICY
    min_n = policy["min_matches_for_claim"]
    out = {}

    for r in result_rows:
        raw = (r.get("referee_raw") or "").strip()
        if not raw:
            continue
        rid, display, on_list, conf = normalize_referee(raw)
        s = out.setdefault(rid, {
            "referee_id": rid,
            "referee_name": display,
            "on_watchlist": on_list,
            "name_confidence": conf,
            "role": ROLE_REFEREE,
            "sample_size": 0,
            "yellow_total": 0,
            "red_total": 0,
            "dates": [],
            "seasons": set(),
            "source_ids": set(),
            "matches": [],
        })
        s["sample_size"] += 1
        s["dates"].append(r["kickoff_utc"])
        if r.get("season"):
            s["seasons"].add(r["season"])
        s["source_ids"].add(r.get("source_id", SOURCE_STATS))
        yh, ya = r.get("yellow_home"), r.get("yellow_away")
        rh, ra = r.get("red_home"), r.get("red_away")
        if None not in (yh, ya):
            s["yellow_total"] += yh + ya
        if None not in (rh, ra):
            s["red_total"] += rh + ra
        s["matches"].append({
            "match_id": r.get("match_id"),
            "kickoff_utc": r["kickoff_utc"].strftime("%Y-%m-%d"),
            "home": r.get("home_team"),
            "away": r.get("away_team"),
        })

    # 联赛整体均值，用于「和同僚比」的描述性对照（不是结论）
    played = [s for s in out.values() if s["sample_size"]]
    total_y = sum(s["yellow_total"] for s in played)
    total_m = sum(s["sample_size"] for s in played)
    league_avg = (total_y / total_m) if total_m else 0.0

    for s in out.values():
        n = s["sample_size"]
        s["yellow_per_match"] = round(s["yellow_total"] / n, 2) if n else 0.0
        s["red_per_match"] = round(s["red_total"] / n, 2) if n else 0.0
        s["date_range"] = (
            min(s["dates"]).strftime("%Y-%m-%d"),
            max(s["dates"]).strftime("%Y-%m-%d"),
        )
        s["seasons"] = sorted(s["seasons"])
        s["source_ids"] = sorted(s["source_ids"])
        s["league_avg_yellow"] = round(league_avg, 2)
        s["vs_league_avg"] = (round(s["yellow_per_match"] - league_avg, 2)
                              if n else 0.0)
        s["can_conclude"] = n >= min_n
        s["metric_note"] = policy["metric_note"]
        # 这一句必须跟着数字一起出现在任何展示里
        s["interpretation_warning"] = (
            "黄牌多不等于裁判有问题：场均牌数同时受球队风格、比赛重要性、"
            "德比强度、联赛整体尺度影响。"
            + ("" if s["can_conclude"] else
               f" 当前样本仅 {n} 场（门槛 {min_n} 场），**只能作为观察线索，"
               f"不得据此下任何结论**。")
        )
        s.pop("dates", None)
        s.pop("matches", None)
    return out


def referee_brief(stats, max_recent=3):
    """把 stats 压成一段可直接进推送的描述（结论受 can_conclude 约束）。"""
    if not stats:
        return {"text": "无该裁判的执法数据", "can_conclude": False, "sample_size": 0}
    head = (f"{stats['referee_name']}（{'观察名单' if stats['on_watchlist'] else '普通'}）："
            f"统计窗口 {stats['date_range'][0]} ~ {stats['date_range'][1]}，"
            f"主裁 {stats['sample_size']} 场，场均黄牌 {stats['yellow_per_match']}"
            f"（联赛均值 {stats['league_avg_yellow']}），红牌合计 {stats['red_total']}。")
    tail = ("样本充足，可作为横向对比参考。" if stats["can_conclude"]
            else f"样本仅 {stats['sample_size']} 场，不足以下结论。")
    return {
        "text": head + tail,
        "can_conclude": stats["can_conclude"],
        "sample_size": stats["sample_size"],
        "warning": stats["interpretation_warning"],
    }


# =============================================================================
# 三、任命监控 + 公布规律审计
# =============================================================================
def build_appointment(match, now, first_seen_at=None, source_url=SOURCE_URL_FIXTURES):
    """为一��比赛生成任命记录。

    拿不到裁判时返回 `appointment_status=pending`，而不是编一个名字。
    `first_seen_at` 由调用方从 state 里带入——它就是「我方首次观测到署名」的
    真实时间戳，是 §5「用真实时间戳证明速度」的基础设施。
    """
    raw = (match.get("referee_name_raw") or "").strip()
    kick = _parse_iso(match.get("kickoff_time_utc"))
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    if not raw:
        return {
            "referee_id": "",
            "referee_name": "",
            "match_id": match["match_id"],
            "competition": match.get("competition", ""),
            "appointment_date": "",
            "role": ROLE_REFEREE,
            "appointment_status": APPT_PENDING,
            "source_id": SOURCE_APPOINTMENT,
            "source_url": source_url,
            "published_at": "",          # 源里没有发布时间字段，不编造
            "first_seen_at": "",
            "verified_at": now_iso,
        }

    rid, display, _on_list, _conf = normalize_referee(raw)
    return {
        "referee_id": rid,
        "referee_name": display,
        "match_id": match["match_id"],
        "competition": match.get("competition", ""),
        "appointment_date": kick.strftime("%Y-%m-%d") if kick else "",
        "role": ROLE_REFEREE,
        "appointment_status": APPT_CONFIRMED,
        "source_id": SOURCE_APPOINTMENT,
        "source_url": source_url,
        # ⚠️ 源里**没有**「任命公告发布时间」这一列。填 published_at 等于编造，
        #    所以留空，只保留我方观测时间 first_seen_at / verified_at。
        "published_at": "",
        "first_seen_at": first_seen_at or now_iso,
        "verified_at": now_iso,
    }


def build_unavailable_roles(match, now):
    """其余四种角色的记录：明确写「源不提供」，不留空也不猜。"""
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    return [{
        "referee_id": "",
        "referee_name": "",
        "match_id": match["match_id"],
        "competition": match.get("competition", ""),
        "appointment_date": "",
        "role": role,
        "appointment_status": APPT_UNAVAILABLE,
        "source_id": SOURCE_APPOINTMENT,
        "source_url": SOURCE_URL_FIXTURES,
        "published_at": "",
        "first_seen_at": "",
        "verified_at": now_iso,
    } for role in (ROLE_ASSISTANT, ROLE_FOURTH, ROLE_VAR, ROLE_AVAR)]


def audit_lead_time(appointments, now):
    """审计「任命公布提前量」的实测分布。

    这是任务书「不能假设固定提前一周，必须审计实际公布规律」的落地：
    每轮把「已署名场次距开球还有多少天」记下来，长期累积成分布。
    单次观测不构成规律，所以返回里显式带 `samples` 与 `note`。
    """
    leads = []
    for a in appointments:
        if a.get("appointment_status") != APPT_CONFIRMED:
            continue
        d = _parse_iso(a.get("appointment_date"))
        if not d:
            continue
        leads.append((d.date() - now.date()).days)
    leads.sort()
    n = len(leads)
    if not n:
        return {"samples": 0, "min_days": None, "max_days": None,
                "median_days": None, "distribution": {},
                "note": "本次观测没有任何已署名的场次，无法给出提前量"}
    return {
        "samples": n,
        "min_days": leads[0],
        "max_days": leads[-1],
        "median_days": leads[n // 2],
        "distribution": {str(k): leads.count(k) for k in sorted(set(leads))},
        "note": (f"本次观测 {n} 场已署名，距开球 {leads[0]}~{leads[-1]} 天。"
                 f"这是**单次快照**，只能说明「截至此刻已公布到多远」，"
                 f"不能反推「固定提前 N 天公布」的规律。"),
    }


def _parse_iso(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(str(s).strip(), fmt)
        except ValueError:
            continue
    return None


# =============================================================================
# 四、八步流程（七位重点裁判命中时触发）
# =============================================================================
EIGHT_STEPS = (
    ("confirm_appointment", "确认任命并区分角色"),
    ("verify_identity", "核对裁判身份（同名不同人）"),
    ("pull_history", "拉取该裁判执法数据（含样本量）"),
    ("team_history", "拉取与两队的历史交手记录"),
    ("disputes", "检索争议判罚并按 A–E 分级"),
    ("dimensions", "按 12 个维度分析判罚尺度"),
    ("risk_and_gaps", "生成风险提示与信息缺口"),
    ("human_review", "人工复核并标注证据等级"),
)

STEP_DONE = "done"
STEP_PARTIAL = "partial"
STEP_INSUFFICIENT = "insufficient_data"
STEP_PENDING = "pending"


def run_eight_steps(match, appointment, stats, disputes=None):
    """执行八步流程，返回逐步状态。

    每一步都只报「我们实际有什么」，缺数据就标 insufficient_data，
    绝不用通用知识补一段看起来很像分析的文字。
    """
    disputes = disputes or []
    steps = []

    def add(key, status, detail):
        name = dict(EIGHT_STEPS)[key]
        steps.append({"step": key, "name": name, "status": status, "detail": detail})

    confirmed = appointment.get("appointment_status") == APPT_CONFIRMED
    add("confirm_appointment", STEP_DONE if confirmed else STEP_PENDING,
        f"主裁：{appointment.get('referee_name') or '未公布'}；"
        f"其余角色：源不提供（见 information_gaps）")

    conf = (stats or {}).get("name_confidence", "unknown")
    add("verify_identity",
        STEP_DONE if conf in ("watchlist", "exact_name") else STEP_PARTIAL,
        f"匹配方式：{conf}")

    if stats and stats.get("sample_size"):
        add("pull_history",
            STEP_DONE if stats.get("can_conclude") else STEP_PARTIAL,
            f"主裁 {stats['sample_size']} 场，窗口 {stats['date_range'][0]}~"
            f"{stats['date_range'][1]}，"
            + ("样本充足" if stats.get("can_conclude")
               else "样本不足以下结论，仅作线索"))
    else:
        add("pull_history", STEP_INSUFFICIENT, "无该裁判的执法记录")

    add("team_history", STEP_INSUFFICIENT,
        "免费源不含「裁判×球队」的历史交手索引，需人工或付费源补充")

    if disputes:
        graded = {d.get("grade") for d in disputes}
        add("disputes", STEP_DONE,
            f"检索到 {len(disputes)} 条争议记录，证据等级 {sorted(g for g in graded if g)}")
    else:
        add("disputes", STEP_INSUFFICIENT,
            "本次未检索到可核实的争议记录（不得据此认为「该裁判无争议」）")

    if stats and stats.get("sample_size"):
        add("dimensions", STEP_PARTIAL,
            "12 个维度中仅「牌数相关」可从结果 CSV 量化，其余需比赛事件数据")
    else:
        add("dimensions", STEP_INSUFFICIENT, "缺少可量化的执法数据")

    add("risk_and_gaps", STEP_DONE, "见 report.risk_notes 与 report.information_gaps")
    add("human_review", STEP_PENDING, "等待人工复核后再对外表述")
    return steps


# =============================================================================
# 五、证据分级
# =============================================================================
def grade_evidence(source_type):
    """来源类型 → A–E 证据等级。不允许调用方自定义等级。"""
    mapping = {
        "official_confirm": "A",   # 官方承认误判
        "official_explain": "B",   # 官方解释（未认错）
        "media_analysis": "C",     # 专业媒体分析
        "fan_comment": "D",        # 球迷争议
        "unknown": "E",
    }
    return mapping.get((source_type or "unknown").strip().lower(), "E")


def is_fact_grade(grade):
    return grade in cfg.FACT_GRADES


def split_by_grade(disputes):
    """把争议记录拆成「可作事实陈述」与「仅作线索」。

    这是「不能把球迷评论升级成官方确认错误」的执行点：
    D/E 级永远进不了 facts。
    """
    facts, leads = [], []
    for d in disputes or []:
        item = dict(d)
        item.setdefault("grade", grade_evidence(item.get("source_type")))
        (facts if is_fact_grade(item["grade"]) else leads).append(item)
    return facts, leads


# =============================================================================
# 六、每场 A–I 九项报告
# =============================================================================
NINE_ITEMS = (
    ("A", "主裁与执法团队"),
    ("B", "该裁判近期执法数据（含样本量）"),
    ("C", "与两队的历史交手记录"),
    ("D", "争议判罚记录（A–E 分级）"),
    ("E", "VAR 团队与介入倾向"),
    ("F", "判罚尺度特征（12 维度）"),
    ("G", "舆论关注点"),
    ("H", "风险提示"),
    ("I", "信息缺口与未核实项"),
)

ITEM_OK = "ok"
ITEM_INSUFFICIENT = "insufficient_data"
ITEM_UNAVAILABLE = "source_unavailable"


def build_nine_item_report(match, appointment, stats, disputes=None,
                           mentions=0, officials=None):
    """生成 A–I 九项。

    `officials` 是英超官方接口给的完整裁判组
    （`[{"role": "Referee"/"VAR"/..., "referee_name": ...}]`）。
    有它就能把 E 项（VAR 团队）从「源不提供」升级为真实名单；
    没有它时必须老实标 source_unavailable，**不许因为「通常是谁」就填一个名字**。

    ⚠️ 硬规则：任何一项没有数据支撑时，`status` 必须是 insufficient_data 或
    source_unavailable，`text` 只写「没有什么」，**不许写「可能」「大概」
    或从常识里编一段**。任务书要求「不能为凑内容编造争议」，这就是执行点。
    """
    facts, leads = split_by_grade(disputes)
    officials = officials or []
    by_role = {}
    for o in officials:
        by_role.setdefault(o["role"], []).append(o["referee_name"])
    items = {}

    name = appointment.get("referee_name") or ""
    if not name and by_role.get(ROLE_REFEREE):
        name = by_role[ROLE_REFEREE][0]
    staff = "；".join(
        f"{role} {'/'.join(names)}"
        for role, names in by_role.items() if role != ROLE_REFEREE)
    items["A"] = {
        "status": ITEM_OK if name else ITEM_INSUFFICIENT,
        "text": ((f"主裁 {name}" + ("（观察名单）" if is_watch_referee(name) else ""))
                 + (f"；{staff}" if staff else "")
                 if name else "任命尚未公布"),
        "detail": {"role": ROLE_REFEREE,
                   "appointment_status": appointment.get("appointment_status"),
                   "officials": officials},
    }

    if stats and stats.get("sample_size"):
        items["B"] = {
            "status": ITEM_OK,
            "text": referee_brief(stats)["text"],
            "detail": {k: stats[k] for k in
                       ("sample_size", "date_range", "yellow_per_match",
                        "red_total", "can_conclude", "metric_note")},
        }
    else:
        items["B"] = {"status": ITEM_INSUFFICIENT,
                      "text": "无该裁判的执法统计数据", "detail": {}}

    items["C"] = {"status": ITEM_INSUFFICIENT,
                  "text": "免费数据源不含「裁判×球队」历史交手索引",
                  "detail": {}}

    if facts:
        items["D"] = {
            "status": ITEM_OK,
            "text": f"可核实的争议记录 {len(facts)} 条（证据等级 "
                    f"{'/'.join(sorted({d['grade'] for d in facts}))}）",
            "detail": {"facts": facts, "leads": leads},
        }
    elif leads:
        items["D"] = {
            "status": ITEM_INSUFFICIENT,
            "text": f"仅有 {len(leads)} 条未达事实标准的线索（D/E 级），"
                    f"**不得作为误判陈述**",
            "detail": {"facts": [], "leads": leads},
        }
    else:
        items["D"] = {"status": ITEM_INSUFFICIENT,
                      "text": "本次未检索到可核实的争议记录",
                      "detail": {"facts": [], "leads": []}}

    var_staff = by_role.get(ROLE_VAR, []) + by_role.get(ROLE_AVAR, [])
    if var_staff:
        items["E"] = {
            "status": ITEM_OK,
            "text": (f"VAR {'/'.join(by_role.get(ROLE_VAR, [])) or '未记录'}；"
                     f"AVAR {'/'.join(by_role.get(ROLE_AVAR, [])) or '未记录'}"),
            "detail": {"VAR": by_role.get(ROLE_VAR, []),
                       "AVAR": by_role.get(ROLE_AVAR, []),
                       "source": "premierleague_official"},
        }
    else:
        items["E"] = {"status": ITEM_UNAVAILABLE,
                      "text": "本次未获取到官方 VAR/AVAR 任命，无法分析 VAR 团队",
                      "detail": {}}

    if stats and stats.get("sample_size"):
        items["F"] = {
            "status": ITEM_INSUFFICIENT,
            "text": "12 个维度中仅牌数类可量化，其余需逐场事件数据，暂不输出结论",
            "detail": {"quantifiable": ["red_card", "second_yellow", "foul_play"],
                       "not_quantifiable": [k for k, _ in cfg.OBSERVATION_DIMENSIONS
                                            if k not in
                                            ("red_card", "second_yellow", "foul_play")]},
        }
    else:
        items["F"] = {"status": ITEM_INSUFFICIENT, "text": "无可用数据", "detail": {}}

    items["G"] = {
        "status": ITEM_OK if mentions else ITEM_INSUFFICIENT,
        "text": (f"近 24h 新闻提及本场 {mentions} 次" if mentions
                 else "未采集到本场的舆论热度数据"),
        "detail": {"mentions": mentions},
    }

    risk = []
    if stats and not stats.get("can_conclude"):
        risk.append(f"样本仅 {stats['sample_size']} 场，任何「尺度偏严/偏松」的说法都不成立")
    if not name:
        risk.append("任命未公布，赛前需持续监控")
    risk.append("黄牌数受球队风格与比赛强度影响，不能反推裁判水平")
    items["H"] = {"status": ITEM_OK, "text": "；".join(risk), "detail": {"notes": risk}}

    gaps = ["裁判×球队历史交手：免费源不提供",
            "逐场判罚事件（点球/改判/越位）：免费源不提供"]
    if not var_staff:
        gaps.insert(0, "VAR/AVAR/第四官员任命：本次未获取到")
    if not facts:
        gaps.append("争议判罚：无可核实记录（不等于无争议）")
    items["I"] = {"status": ITEM_OK, "text": "；".join(gaps), "detail": {"gaps": gaps}}

    return {
        "match_id": match["match_id"],
        "title": f"{match['home_team']} vs {match['away_team']}",
        "items": items,
        "order": [k for k, _ in NINE_ITEMS],
        "labels": dict(NINE_ITEMS),
        "facts_count": len(facts),
        "leads_count": len(leads),
    }


def render_nine_item_report(report):
    """把九项渲染成纯文本（给 Telegram / 日志用）。"""
    lines = [f"【裁判情报】{report['title']}"]
    for key in report["order"]:
        item = report["items"][key]
        mark = {"ok": "✓", "insufficient_data": "—",
                "source_unavailable": "×"}.get(item["status"], "?")
        lines.append(f" {mark} {key}. {report['labels'][key]}：{item['text']}")
    if report["leads_count"]:
        lines.append(f" ⚠️ 另有 {report['leads_count']} 条 D/E 级线索未列入事实陈述")
    return "\n".join(lines)


def validate_appointment(rec):
    """校验任命记录字段完整性。"""
    problems = []
    for f in APPOINTMENT_FIELDS:
        if f not in rec:
            problems.append(f"缺少字段 {f}")
    if rec.get("role") not in ROLES:
        problems.append(f"role 非法：{rec.get('role')}")
    if rec.get("appointment_status") not in (APPT_CONFIRMED, APPT_PENDING,
                                             APPT_UNAVAILABLE):
        problems.append(f"appointment_status 非法：{rec.get('appointment_status')}")
    if rec.get("appointment_status") == APPT_CONFIRMED and not rec.get("referee_name"):
        problems.append("已确认任命却没有裁判姓名")
    return problems
