"""比赛日历与焦点战识别（V2.0 §3）。

## 这个模块解决什么

§3 要求：比赛档案要有 `match_id`/`kickoff_time_utc`/`kickoff_time_beijing`/
`focus_level`/`focus_reasons` 等字段，且**示例对阵不得写死**——焦点战必须根据
积分榜、排名、竞争关系**动态识别**。

所以这里的流程是：

    抓赛程 + 抓结果 → 自己算积分榜 → 用积分榜判定每场的焦点等级

不调 AI、不看媒体标题。分级结果可复现：同一个积分榜必然得出同一个等级。

## 数据源（全部免费，2026-10-31 实测可用）

| source_id | 用途 | 实测 |
|---|---|---|
| `fixturedownload` | 全季 380 场赛程，**真 UTC**，含比分 | 78.7KB / 2.16s |
| `football_data_fixtures` | 未来约 10 天赛程，**含裁判署名** | 54.2KB / 3.82s |
| `football_data_results` | 已完赛结果，含裁判 + 红黄牌 | 38KB / 3.62s |

## ⚠️ 一个必须记住的坑：两个源的时间基准不同

`fixturedownload` 的 `DateUtc` 是**真 UTC**；`football-data` 的 `Date`+`Time`
是**英国本地时间**。2026-10-31 那天两者看起来完全一致（因为英国 10-25 已回到 GMT，
本地时间 = UTC），但**夏令时期间会差整整一小时**。实测证据（2026-08-21，BST）：

    fixturedownload: 2026-08-21 19:00:00Z
    football-data  : 21/08/2026 20:00      ← 本地 20:00 = UTC 19:00

如果把 football-data 的时间当成 UTC 直接用，整季前半段的所有开球时间都会**晚一小时**，
首发监控就会**提前一小时**开始盯——白烧一小时额度，还可能在真公布时已经退避了。

因此本模块统一把 football-data 的本地时间按英国夏令时规则换算成 UTC
（`uk_local_to_utc`），并对两个源的同一场比赛做**时间一致性校验**，
偏差超过阈值就记一条 `data_quality` 告警而不是静默取一个。

## 焦点战分级为什么不是写死的

`focus_config.DERBIES` / `BIG_CLUBS` 只提供「是不是德比」「是不是传统强队」两个
布尔输入；等级由 `grade_match()` 用**当前积分榜**的排名与分差累加分数得出。
测试 `test_grade_is_not_hardcoded` 用同一对球队在两份不同积分榜下必须得到不同等级来守这条线。
"""

import calendar
import csv
import datetime
import io
import json
import re
import urllib.request

import focus_config as cfg

# =============================================================================
# 常量
# =============================================================================
COMPETITION = "Premier League"
COMPETITION_CODE = "epl"
CURRENT_SEASON = "2026/27"

SOURCE_FIXTUREDOWNLOAD = "fixturedownload"
SOURCE_FD_FIXTURES = "football_data_fixtures"
SOURCE_FD_RESULTS = "football_data_results"

URL_FIXTUREDOWNLOAD = "https://fixturedownload.com/feed/json/epl-2026"
URL_FD_FIXTURES = "https://www.football-data.co.uk/fixtures.csv"
URL_FD_RESULTS = "https://www.football-data.co.uk/mmz4281/{code}/E0.csv"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# 比赛档案的字段集合（§3 要求，一个都不能少）
MATCH_FIELDS = (
    "match_id", "competition", "season", "home_team", "away_team",
    "kickoff_time_utc", "kickoff_time_beijing", "match_status",
    "focus_level", "focus_reasons", "referee_monitor_enabled",
    "lineup_monitor_enabled", "official_lineup_status", "last_checked_at",
    "source_ids",
)

# 两源开球时间允许的偏差（分钟）。超过就记数据质量告警。
KICKOFF_TOLERANCE_MINUTES = 15

MATCH_STATUS = {
    "upcoming": "未开始",
    "live": "进行中",
    "awaiting_result": "已开球待结果",
    "finished": "已结束",
}

# 比赛时长 + 中场 + 补时，超过这个值还没有结果就标 awaiting_result
MATCH_WINDOW_MINUTES = 150


# =============================================================================
# 一、时区：英国本地时间 → UTC（不依赖 tzdata）
# =============================================================================
# 为什么手写而不用 zoneinfo：Windows 上默认没有 IANA 时区库，
# `zoneinfo.ZoneInfo("Europe/London")` 会抛 KeyError（本机实测）。而 CI 是
# ubuntu-latest（有 tzdata），两边行为不一致比统一手写更危险。
# 规则很简单且确定：3 月最后一个周日 01:00 起 BST(+1)，10 月最后一个周日 02:00 起 GMT(+0)。
def _last_sunday(year, month):
    """该月最后一个周日。"""
    last = calendar.monthrange(year, month)[1]
    d = datetime.date(year, month, last)
    while d.weekday() != 6:      # 6 = Sunday
        d -= datetime.timedelta(days=1)
    return d


def is_bst(naive_local):
    """给定的**英国本地时刻**是否处于夏令时。"""
    y = naive_local.year
    start = datetime.datetime.combine(_last_sunday(y, 3), datetime.time(1, 0))
    end = datetime.datetime.combine(_last_sunday(y, 10), datetime.time(2, 0))
    return start <= naive_local < end


def uk_local_to_utc(naive_local):
    """英国本地时间 → UTC。

    夏令时边界那一小时本身有歧义（01:00–02:00 出现两次）。足球不会在这个
    时段开球，因此按「夏令时优先」处理，并在注释里写死这个假设，
    避免以后有人误以为它能处理任意时刻。
    """
    if naive_local.tzinfo is not None:
        raise ValueError("请传入 naive 的英国本地时间，不要传带时区的时间")
    offset = 1 if is_bst(naive_local) else 0
    return naive_local - datetime.timedelta(hours=offset)


def to_beijing(utc_dt):
    """UTC → 北京时间（固定 UTC+8，中国无夏令时）。"""
    return utc_dt + datetime.timedelta(hours=8)


def iso_utc(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# =============================================================================
# 二、队名归一化
# =============================================================================
# 两个源的差异经实测只有两处（英超 20 队逐一对齐过）：
#   football-data: "Man United" / "Tottenham"
#   fixturedownload: "Man Utd"  / "Spurs"
# 其余 18 队完全一致。别名表保持最小，避免「猜」出错误映射。
TEAM_ALIASES = {
    "man united": "man utd",
    "manchester united": "man utd",
    "man utd": "man utd",
    "manchester city": "man city",
    "man city": "man city",
    "tottenham": "tottenham",
    "tottenham hotspur": "tottenham",
    "spurs": "tottenham",
    "nott'm forest": "nottm forest",
    "nottingham forest": "nottm forest",
    "nottm forest": "nottm forest",
    "newcastle": "newcastle",
    "newcastle united": "newcastle",
    "brighton": "brighton",
    "brighton and hove albion": "brighton",
    "west ham": "west ham",
    "west ham united": "west ham",
    "wolves": "wolves",
    "wolverhampton wanderers": "wolves",
    "leeds": "leeds",
    "leeds united": "leeds",
    "hull": "hull",
    "hull city": "hull",
    "ipswich": "ipswich",
    "ipswich town": "ipswich",
    "coventry": "coventry",
    "coventry city": "coventry",
    "sunderland": "sunderland",
    "bournemouth": "bournemouth",
    "afc bournemouth": "bournemouth",
    "brentford": "brentford",
    "crystal palace": "crystal palace",
    "everton": "everton",
    "fulham": "fulham",
    "aston villa": "aston villa",
    "arsenal": "arsenal",
    "chelsea": "chelsea",
    "liverpool": "liverpool",
}


def canon_team(name):
    """归一化队名（小写、去多余空格）。未收录的名字原样小写返回。

    刻意**不做模糊匹配**：猜错一个队名会让整场比赛的档案挂到错误的对手上，
    宁可保留原名让它 join 不上、在数据质量里报出来。
    """
    n = re.sub(r"\s+", " ", (name or "").strip()).lower()
    return TEAM_ALIASES.get(n, n)


def display_team(name):
    """用于展示的队名。

    两个源对同一支球队的写法不同（`Spurs` / `Tottenham`），若直接透传源里的
    原始字符串，同一支球队会在不同场次里显示成两个名字。所以统一走
    canon → DISPLAY_NAMES；未收录的名字原样返回，不猜。
    """
    raw = re.sub(r"\s+", " ", (name or "").strip())
    return DISPLAY_NAMES.get(canon_team(raw), raw)


# 展示用标准名（canon 键 → 展示名）。只收实测存在差异或写法不规范的队。
DISPLAY_NAMES = {
    "man utd": "Man Utd",
    "man city": "Man City",
    "tottenham": "Tottenham",
    "nottm forest": "Nott'm Forest",
    "newcastle": "Newcastle",
    "brighton": "Brighton",
    "west ham": "West Ham",
    "wolves": "Wolves",
}


# =============================================================================
# 三、解析各源
# =============================================================================
def _parse_uk_date(s):
    """'31/10/2026' 或 '31/10/26' → date。"""
    s = (s or "").strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"无法解析日期：{s!r}")


def _parse_hhmm(s):
    s = (s or "").strip()
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if not m:
        return None
    return datetime.time(int(m.group(1)), int(m.group(2)))


def parse_fixturedownload(raw):
    """解析 fixturedownload 的 JSON（真 UTC，含比分）。"""
    data = raw if isinstance(raw, (list, dict)) else json.loads(raw)
    if isinstance(data, dict):
        data = data.get("fixtures") or data.get("data") or []
    out = []
    for m in data:
        try:
            kick = datetime.datetime.strptime(
                str(m["DateUtc"]).strip(), "%Y-%m-%d %H:%M:%SZ")
        except (KeyError, ValueError):
            continue
        out.append({
            "home_team": display_team(m.get("HomeTeam", "")),
            "away_team": display_team(m.get("AwayTeam", "")),
            "kickoff_utc": kick,
            "round": m.get("RoundNumber"),
            "venue": m.get("Location"),
            "home_score": m.get("HomeTeamScore"),
            "away_score": m.get("AwayTeamScore"),
            "source_id": SOURCE_FIXTUREDOWNLOAD,
            "time_is_utc": True,
        })
    return out


def parse_football_data_fixtures(text):
    """解析 football-data.co.uk 的 fixtures.csv（**英国本地时间**，含裁判）。"""
    rows = list(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))
    out = []
    for r in rows:
        if (r.get("Div") or "").strip() != "E0":
            continue          # 只要英超，别把英冠/苏超混进焦点战
        try:
            d = _parse_uk_date(r.get("Date"))
        except ValueError:
            continue
        t = _parse_hhmm(r.get("Time"))
        if t is None:
            continue
        out.append({
            "home_team": display_team(r.get("HomeTeam", "")),
            "away_team": display_team(r.get("AwayTeam", "")),
            "kickoff_utc": uk_local_to_utc(datetime.datetime.combine(d, t)),
            "referee_raw": (r.get("Referee") or "").strip(),
            "source_id": SOURCE_FD_FIXTURES,
            "time_is_utc": False,
        })
    return out


def parse_football_data_results(text):
    """解析赛季结果 CSV（**英国本地时间**，含裁判 + 红黄牌）。"""
    rows = list(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))
    out = []
    for r in rows:
        if (r.get("Div") or "").strip() != "E0":
            continue
        try:
            d = _parse_uk_date(r.get("Date"))
        except ValueError:
            continue
        t = _parse_hhmm(r.get("Time")) or datetime.time(15, 0)
        try:
            hs, as_ = int(r["FTHG"]), int(r["FTAG"])
        except (KeyError, TypeError, ValueError):
            hs = as_ = None
        out.append({
            "home_team": display_team(r.get("HomeTeam", "")),
            "away_team": display_team(r.get("AwayTeam", "")),
            "kickoff_utc": uk_local_to_utc(datetime.datetime.combine(d, t)),
            "referee_raw": (r.get("Referee") or "").strip(),
            "home_score": hs,
            "away_score": as_,
            "yellow_home": _int_or_none(r.get("HY")),
            "yellow_away": _int_or_none(r.get("AY")),
            "red_home": _int_or_none(r.get("HR")),
            "red_away": _int_or_none(r.get("AR")),
            "source_id": SOURCE_FD_RESULTS,
            "time_is_utc": False,
        })
    return out


def _int_or_none(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


# =============================================================================
# 四、积分榜（自己算，不依赖任何第三方榜单）
# =============================================================================
def build_standings(results):
    """从已完赛结果算积分榜。返回 {canon_team: {...}} 与排名顺序列表。"""
    table = {}

    def row(name):
        key = canon_team(name)
        if key not in table:
            table[key] = {
                "team": key,
                "played": 0, "won": 0, "drawn": 0, "lost": 0,
                "gf": 0, "ga": 0, "points": 0,
            }
        return table[key]

    for r in results:
        if r.get("home_score") is None or r.get("away_score") is None:
            continue
        h, a = row(r["home_team"]), row(r["away_team"])
        hs, as_ = r["home_score"], r["away_score"]
        h["played"] += 1
        a["played"] += 1
        h["gf"] += hs
        h["ga"] += as_
        a["gf"] += as_
        a["ga"] += hs
        if hs > as_:
            h["won"] += 1
            a["lost"] += 1
            h["points"] += 3
        elif hs < as_:
            a["won"] += 1
            h["lost"] += 1
            a["points"] += 3
        else:
            h["drawn"] += 1
            a["drawn"] += 1
            h["points"] += 1
            a["points"] += 1

    for t in table.values():
        t["gd"] = t["gf"] - t["ga"]

    # 排序口径：积分 → 净胜球 → 进球数 → 队名（与英超官方一致的前三项）
    order = sorted(
        table.values(),
        key=lambda t: (-t["points"], -t["gd"], -t["gf"], t["team"]),
    )
    for i, t in enumerate(order, start=1):
        t["position"] = i
    return table, [t["team"] for t in order]


# =============================================================================
# 五、合并两源 + 时间一致性校验
# =============================================================================
def make_match_id(home, away, kickoff_utc):
    """稳定的 match_id：同一场比赛在任何源里都得到同一个 id。

    格式：`epl-20261031-chelsea-manutd`

    刻意不含轮次与**开球时刻**——同一天内改开球时间（转播调整）时 id 不变，
    「已推送过」的状态不会因此丢失。跨日改期会生成新 id，这是有意的：
    那已经是另一场「比赛日」的档案了。
    """
    h = re.sub(r"[^a-z0-9]+", "", canon_team(home))
    a = re.sub(r"[^a-z0-9]+", "", canon_team(away))
    return f"{COMPETITION_CODE}-{kickoff_utc.strftime('%Y%m%d')}-{h}-{a}"


def _key(home, away):
    return (canon_team(home), canon_team(away))


def merge_sources(base_rows, enrich_rows):
    """把 enrich（含裁判）合并进 base（含全季赛程）。

    返回 (matches, warnings)。合并键是「双方队名 + UTC 日期」，
    再校验开球时间偏差；偏差超过 KICKOFF_TOLERANCE_MINUTES 就记告警。
    """
    index = {}
    for r in base_rows:
        index.setdefault((_key(r["home_team"], r["away_team"]),
                          r["kickoff_utc"].date()), []).append(r)

    warnings = []
    for e in enrich_rows:
        k = (_key(e["home_team"], e["away_team"]), e["kickoff_utc"].date())
        cands = index.get(k)
        if not cands:
            # 日期对不上时再试前后一天：跨午夜开球的比赛（极少）会落在相邻日期
            for delta in (-1, 1):
                alt = (k[0], k[1] + datetime.timedelta(days=delta))
                if alt in index:
                    cands = index[alt]
                    break
        if not cands:
            warnings.append({
                "code": "ENRICH_UNMATCHED",
                "detail": f"{e['home_team']} vs {e['away_team']} "
                          f"({iso_utc(e['kickoff_utc'])}) 在基准赛程里找不到对应场次",
                "source_id": e["source_id"],
            })
            continue
        tgt = cands[0]
        diff = abs((tgt["kickoff_utc"] - e["kickoff_utc"]).total_seconds()) / 60
        if diff > KICKOFF_TOLERANCE_MINUTES:
            warnings.append({
                "code": "KICKOFF_MISMATCH",
                "detail": f"{e['home_team']} vs {e['away_team']}："
                          f"{SOURCE_FIXTUREDOWNLOAD}={iso_utc(tgt['kickoff_utc'])} 与 "
                          f"{e['source_id']}={iso_utc(e['kickoff_utc'])} 相差 {diff:.0f} 分钟",
                "source_id": e["source_id"],
            })
        tgt.setdefault("source_ids", []).append(e["source_id"])
        if e.get("referee_raw"):
            tgt["referee_raw"] = e["referee_raw"]
        for f in ("home_score", "away_score"):
            if e.get(f) is not None and tgt.get(f) is None:
                tgt[f] = e[f]
    return base_rows, warnings


# =============================================================================
# 六、焦点战分级（规则化，不靠 AI 主观判断）
# =============================================================================
def _positions_of(standings, home, away):
    h = standings.get(canon_team(home))
    a = standings.get(canon_team(away))
    return h, a


def grade_match(match, standings, total_teams=None, mentions=0):
    """按 focus_config.FOCUS_RULES 计算焦点等级。

    返回 (level, reasons, score)。reasons 是结构化的列表，
    每项含 rule / label / weight / detail，直接进 match["focus_reasons"]。

    `mentions` 必须由调用方传入**真实**的新闻提及次数；拿不到就传 0，
    public_attention 规则因此不命中——宁可少判一个焦点战，也不猜。
    """
    reasons = []
    h, a = _positions_of(standings, match["home_team"], match["away_team"])
    if total_teams is None:
        total_teams = len(standings) or 20

    def add(rule_key, detail):
        rule = cfg.FOCUS_RULES[rule_key]
        if not rule["enabled"]:
            return
        reasons.append({
            "rule": rule_key,
            "label": rule["label"],
            "weight": rule["weight"],
            "detail": detail,
        })

    if h and a:
        hp, ap = h["position"], a["position"]
        gap = abs(h["points"] - a["points"])

        p = cfg.FOCUS_RULES["title_race"]["params"]
        if hp <= p["max_position"] and ap <= p["max_position"] and gap <= p["max_gap"]:
            add("title_race", f"排名 {hp} vs {ap}，分差 {gap} 分（阈值 {p['max_gap']}）")

        p = cfg.FOCUS_RULES["top4_battle"]["params"]
        lo, hi = p["position_from"], p["position_to"]
        if (lo <= hp <= hi or lo <= ap <= hi) and gap <= p["max_gap"]:
            add("top4_battle", f"排名 {hp} vs {ap} 处于争四区间 {lo}–{hi}，分差 {gap} 分")

        p = cfg.FOCUS_RULES["relegation"]["params"]
        bottom_line = total_teams - p["from_bottom"] + 1
        if hp >= bottom_line and ap >= bottom_line and gap <= p["max_gap"]:
            add("relegation", f"排名 {hp} vs {ap} 均在倒数 {p['from_bottom']} 名内，分差 {gap} 分")

        p = cfg.FOCUS_RULES["table_impact"]["params"]
        if hp <= p["max_position"] or ap <= p["max_position"]:
            add("table_impact", f"有一方位于前 {p['max_position']}（{hp} vs {ap}），会改变榜面格局")
    else:
        # 积分榜缺队（赛季初/升班马还没打）时不做排名类判定，
        # 而不是用「默认第 10 名」之类的假数据凑一个结论。
        pass

    pair = tuple(sorted((canon_team(match["home_team"]), canon_team(match["away_team"]))))
    if pair in {tuple(sorted(p)) for p in cfg.DERBY_PAIRS}:
        add("derby", f"{match['home_team']} vs {match['away_team']} 为德比对阵")

    if all(canon_team(t) in cfg.BIG_CLUBS
           for t in (match["home_team"], match["away_team"])):
        add("big_club_clash", "双方均属传统强队")

    p = cfg.FOCUS_RULES["public_attention"]["params"]
    if mentions >= p["min_mentions"]:
        add("public_attention", f"近 24h 新闻提及 {mentions} 次（阈值 {p['min_mentions']}）")

    score = sum(r["weight"] for r in reasons)
    level = cfg.NORMAL_GRADE
    for name, threshold in cfg.GRADE_THRESHOLDS:
        if score >= threshold:
            level = name
            break
    return level, reasons, score


def apply_monitor_policy(match):
    """按等级设置两个监控开关。"""
    policy = cfg.MONITOR_POLICY.get(match["focus_level"],
                                    cfg.MONITOR_POLICY[cfg.NORMAL_GRADE])
    match["referee_monitor_enabled"] = bool(policy["referee"])
    match["lineup_monitor_enabled"] = bool(policy["lineup"])
    return match


# =============================================================================
# 七、比赛状态
# =============================================================================
def match_status_of(kickoff_utc, has_result, now):
    if has_result:
        return "finished"
    if now < kickoff_utc:
        return "upcoming"
    if now < kickoff_utc + datetime.timedelta(minutes=MATCH_WINDOW_MINUTES):
        return "live"
    return "awaiting_result"


# =============================================================================
# 八、组装比赛档案
# =============================================================================
def build_match(row, standings, now, total_teams=None, mentions_map=None):
    """把一行合并后的赛程变成完整比赛档案（§3 的全部字段）。"""
    kick = row["kickoff_utc"]
    has_result = row.get("home_score") is not None and row.get("away_score") is not None
    mentions = int((mentions_map or {}).get(
        make_match_id(row["home_team"], row["away_team"], kick), 0))
    level, reasons, score = grade_match(
        row, standings, total_teams=total_teams, mentions=mentions)

    src = [SOURCE_FIXTUREDOWNLOAD] + [
        s for s in row.get("source_ids", []) if s != SOURCE_FIXTUREDOWNLOAD]
    match = {
        "match_id": make_match_id(row["home_team"], row["away_team"], kick),
        "competition": COMPETITION,
        "season": CURRENT_SEASON,
        "home_team": row["home_team"],
        "away_team": row["away_team"],
        "kickoff_time_utc": iso_utc(kick),
        "kickoff_time_beijing": to_beijing(kick).strftime("%Y-%m-%d %H:%M"),
        "match_status": match_status_of(kick, has_result, now),
        "focus_level": level,
        "focus_reasons": reasons,
        "referee_monitor_enabled": False,
        "lineup_monitor_enabled": False,
        "official_lineup_status": "unknown",
        "last_checked_at": iso_utc(now),
        "source_ids": src,
        # ---- 以下为内部字段，不参与 schema 校验，供 §4/§5 使用 ----
        "focus_score": score,
        "referee_name_raw": row.get("referee_raw", ""),
        "home_score": row.get("home_score"),
        "away_score": row.get("away_score"),
        "round": row.get("round"),
        "venue": row.get("venue"),
    }
    return apply_monitor_policy(match)


# =============================================================================
# 九、网络层（可注入，便于离线测试）
# =============================================================================
def http_get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8-sig", errors="replace")


def season_code(season):
    """'2026/27' → '2627'（football-data.co.uk 的 URL 约定）。"""
    a, b = season.split("/")
    return a[-2:] + b[-2:]


def _load(transport, url, label):
    try:
        status, text = transport(url)
        if status != 200:
            return None, {"source_id": label, "status": status, "ok": False}
        return text, {"source_id": label, "status": status, "ok": True}
    except Exception as exc:                     # noqa: BLE001 - 网络异常一律降级
        return None, {"source_id": label, "status": type(exc).__name__,
                      "ok": False, "error": str(exc)[:160]}


def build_calendar(now=None, transport=None, season=CURRENT_SEASON,
                   mentions_map=None, days_ahead=21):
    """抓取 + 合并 + 分级，返回日历。

    单个源失败不会让整体失败：基准源（fixturedownload）失败时返回空日历
    并带上 source_status，由调用方决定是否告警——**绝不编造赛程**。
    """
    now = now or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    transport = transport or http_get
    source_status = []

    raw_fd, st = _load(transport, URL_FIXTUREDOWNLOAD, SOURCE_FIXTUREDOWNLOAD)
    source_status.append(st)
    base = parse_fixturedownload(raw_fd) if raw_fd else []

    raw_fx, st = _load(transport, URL_FD_FIXTURES, SOURCE_FD_FIXTURES)
    source_status.append(st)
    enrich_fx = parse_football_data_fixtures(raw_fx) if raw_fx else []

    raw_res, st = _load(transport, URL_FD_RESULTS.format(code=season_code(season)),
                        SOURCE_FD_RESULTS)
    source_status.append(st)
    results = parse_football_data_results(raw_res) if raw_res else []

    standings, order = build_standings(results)

    warnings = []
    if base:
        merged, warnings = merge_sources(base, enrich_fx)
    else:
        # 基准源挂了：退回只用 football-data 的赛程（有裁判但只有 10 天），
        # 明确标注降级，不假装是全季日历。
        merged = enrich_fx
        warnings.append({
            "code": "BASE_SOURCE_DOWN",
            "detail": f"{SOURCE_FIXTUREDOWNLOAD} 不可用，日历降级为 "
                      f"{SOURCE_FD_FIXTURES} 的短期赛程（约 10 天）",
            "source_id": SOURCE_FIXTUREDOWNLOAD,
        })

    # 已完赛结果也要合并进来（fixturedownload 本身带比分，但赛季初可能滞后）
    if merged and results:
        merged, w2 = merge_sources(merged, results)
        warnings.extend(w2)

    horizon = now + datetime.timedelta(days=days_ahead)
    matches = []
    for row in merged:
        if row["kickoff_utc"] < now - datetime.timedelta(hours=6):
            continue                       # 过去的比赛不进「待监控日历」
        if row["kickoff_utc"] > horizon:
            continue
        matches.append(build_match(row, standings, now,
                                   total_teams=len(order) or 20,
                                   mentions_map=mentions_map))
    matches.sort(key=lambda m: m["kickoff_time_utc"])

    focus = [m for m in matches if m["focus_level"] != cfg.NORMAL_GRADE]
    return {
        "generated_at": iso_utc(now),
        "season": season,
        "competition": COMPETITION,
        "standings": {
            "table": standings,
            "order": order,
            "computed_from_matches": sum(
                1 for r in results
                if r.get("home_score") is not None and r.get("away_score") is not None),
            "source_id": SOURCE_FD_RESULTS,
        },
        "matches": matches,
        "focus_matches": focus,
        "source_status": source_status,
        "data_quality": warnings,
    }


def validate_match(match):
    """检查一份比赛档案是否满足 §3 的字段要求。返回问题列表。"""
    problems = []
    for f in MATCH_FIELDS:
        if f not in match:
            problems.append(f"缺少字段 {f}")
    if match.get("focus_level") not in {g for g, _ in cfg.GRADE_THRESHOLDS} | {cfg.NORMAL_GRADE}:
        problems.append(f"focus_level 非法：{match.get('focus_level')}")
    if not isinstance(match.get("focus_reasons"), list):
        problems.append("focus_reasons 必须是列表")
    for f in ("referee_monitor_enabled", "lineup_monitor_enabled"):
        if not isinstance(match.get(f), bool):
            problems.append(f"{f} 必须是布尔值")
    if match.get("match_status") not in MATCH_STATUS:
        problems.append(f"match_status 非法：{match.get('match_status')}")
    # 北京时间必须等于 UTC+8，不允许两个字段各写各的
    try:
        utc = datetime.datetime.strptime(match["kickoff_time_utc"], "%Y-%m-%dT%H:%M:%SZ")
        bj = datetime.datetime.strptime(match["kickoff_time_beijing"], "%Y-%m-%d %H:%M")
        if to_beijing(utc) != bj:
            problems.append("kickoff_time_beijing 与 kickoff_time_utc 不是 UTC+8 关系")
    except (KeyError, ValueError) as exc:
        problems.append(f"开球时间字段无法解析：{exc}")
    return problems
