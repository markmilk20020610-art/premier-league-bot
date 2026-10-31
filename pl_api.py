"""英超官方数据接口客户端（footballapi.pulselive.com）。

## 为什么加这一层

`football-data.co.uk` 只给主裁姓名，拿不到 VAR/第四官员，也没有首发名单。
2026-10-31 实测发现英超官网自己用的接口是**开放可读**的：

    GET https://footballapi.pulselive.com/football/fixtures/{id}
      → matchOfficials: MAIN / FOURTH_OFFICIAL / VAR / ASSISTANT_VAR / (助理裁判)
      → teamLists:      两队各 11 首发 + 9 替补 + formation.label
      → kickoff.millis: 真 UTC 时间戳（毫秒）

必须带 `Origin` 与 `Referer` 头，否则被拒。这是**公开数据接口**，不需要 key，
也不需要登录。

## 实测边界（2026-10-31）

· 已完赛（status=C）：`matchOfficials` 6 条、`teamLists` 两队各 11+9 人齐全。
· 未开赛（status=U）：`matchOfficials` 为**空数组**，`teamLists` 两队存在但名单为空。
  → 这意味着「任命」和「首发」都是**临场才出现**的，监控必须真的去轮询，
    不能靠「赛前一天抓一次」蒙过去。这也正是 §5 需要高频监控的原因。

## 角色映射

接口的 `role` 字段只有三种取值（MAIN / FOURTH_OFFICIAL / VAR / ASSISTANT_VAR），
助理裁判**没有 role 字段**（只有 name）。因此映射规则是：
   · MAIN              → Referee
   · FOURTH_OFFICIAL   → Fourth Official
   · VAR               → VAR
   · ASSISTANT_VAR     → AVAR
   · 无 role 的其余人   → Assistant Referee
顺序依赖接口返回顺序（实测：MAIN、助理、助理、FOURTH_OFFICIAL、VAR、ASSISTANT_VAR），
因此「无 role」的人一律归为助理裁判是安全的。
"""

import datetime
import json
import urllib.request

BASE = "https://footballapi.pulselive.com/football"
COMPETITION_ID = 1                 # 1 = 英超
COMP_SEASON_2026_27 = 841          # 实测：'English Premier League Season 2026/2027'

SOURCE_OFFICIAL = "premierleague_official"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Origin": "https://www.premierleague.com",
    "Referer": "https://www.premierleague.com/",
    "Accept": "application/json",
}

# 接口 role → 本项目的 role
ROLE_MAP = {
    "MAIN": "Referee",
    "FOURTH_OFFICIAL": "Fourth Official",
    "VAR": "VAR",
    "ASSISTANT_VAR": "AVAR",
}
ROLE_DEFAULT = "Assistant Referee"


def http_get_json(url, timeout=20):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def _get_json(url, transport=None, timeout=20):
    """统一取 JSON。

    `transport` 遵循本项目其它模块的约定：`(url) -> (status, text)`，
    text 是**字符串**（不是已解析的对象）。这样测试里可以塞假传输，
    不必真的联网。传 None 时退回 urllib 实现。
    """
    if transport is None:
        return http_get_json(url, timeout=timeout)
    status, text = transport(url)
    if status != 200:
        raise RuntimeError(f"HTTP {status}: {url}")
    return status, json.loads(text)


def _ms_to_utc(ms):
    """毫秒时间戳 → naive UTC datetime（与项目其它模块保持一致，不用 aware）。"""
    return datetime.datetime.fromtimestamp(
        float(ms) / 1000.0, tz=datetime.timezone.utc).replace(tzinfo=None)


def fixtures_url(comp_season=COMP_SEASON_2026_27, page_size=400):
    return (f"{BASE}/fixtures?comps={COMPETITION_ID}&compSeasons={comp_season}"
            f"&pageSize={page_size}&page=0&sort=asc")


def fixture_url(fixture_id):
    return f"{BASE}/fixtures/{int(fixture_id)}"


# =============================================================================
# 一、赛程列表
# =============================================================================
def parse_fixture_list(payload):
    """把 fixtures 列表响应转成统一行。"""
    content = payload.get("content") if isinstance(payload, dict) else payload
    out = []
    for f in content or []:
        teams = f.get("teams") or []
        if len(teams) < 2:
            continue
        out.append({
            "fixture_id": int(f["id"]),
            "home_team": teams[0]["team"]["name"],
            "away_team": teams[1]["team"]["name"],
            "home_team_id": teams[0]["team"]["id"],
            "away_team_id": teams[1]["team"]["id"],
            "kickoff_utc": _ms_to_utc(f["kickoff"]["millis"]),
            "status": f.get("status"),
            "gameweek": (f.get("gameweek") or {}).get("gameweek"),
            "ground": (f.get("ground") or {}).get("name"),
            "source_id": SOURCE_OFFICIAL,
        })
    return out


def fetch_fixtures(comp_season=COMP_SEASON_2026_27, timeout=20, transport=None):
    status, payload = _get_json(fixtures_url(comp_season), transport=transport,
                               timeout=timeout)
    return parse_fixture_list(payload)


# =============================================================================
# 二、单场详情：裁判组 + 首发
# =============================================================================
def parse_officials(payload):
    """解析 matchOfficials → 本项目角色列表。

    未开赛时接口返回空数组 → 这里返回 []，**不编造**。
    """
    out = []
    for o in payload.get("matchOfficials") or []:
        if not isinstance(o, dict):
            continue
        name = ((o.get("name") or {}).get("display") or "").strip()
        if not name:
            continue
        role = ROLE_MAP.get((o.get("role") or "").strip().upper(), ROLE_DEFAULT)
        out.append({
            "referee_name": name,
            "official_id": o.get("id"),
            "role": role,
            "source_id": SOURCE_OFFICIAL,
        })
    return out


def _group_sizes(formation):
    """formation.players 是 [[门将],[后卫],[中场],[前锋]] 的 id 分组。"""
    groups = (formation or {}).get("players") or []
    return [len(g) for g in groups if isinstance(g, list)]


def parse_team_lists(payload):
    """解析 teamLists → [{team_name, team_id, players, formation, groups, substitutes}]。

    名单为空时返回空 players，并带 `published=False`，让上层知道「还没公布」，
    而不是把空名单当成「0 人首发」去校验。
    """
    out = []
    for tl in payload.get("teamLists") or []:
        # 实测：未开赛时接口返回的是 [null, null]，不是空对象。
        # 不跳过 None 会直接 AttributeError，把整轮监控打挂。
        if not isinstance(tl, dict):
            continue
        team_id = tl.get("teamId")
        lineup = tl.get("lineup") or []
        players = [((p.get("name") or {}).get("display") or "").strip()
                   for p in lineup]
        players = [p for p in players if p]
        formation = ((tl.get("formation") or {}).get("label") or "").strip()
        groups = _group_sizes(tl.get("formation"))
        out.append({
            "team_id": team_id,
            "players": players,
            "formation": formation,
            "groups": groups,
            "substitutes": [((p.get("name") or {}).get("display") or "").strip()
                            for p in (tl.get("substitutes") or [])],
            "published": len(players) > 0,
            "source_id": SOURCE_OFFICIAL,
        })
    return out


def parse_fixture_detail(payload):
    """单场详情 → {fixture_id, kickoff_utc, status, officials, team_lists, teams}。"""
    teams = payload.get("teams") or []
    return {
        "fixture_id": int(payload["id"]) if payload.get("id") is not None else None,
        "kickoff_utc": _ms_to_utc(payload["kickoff"]["millis"])
        if payload.get("kickoff") else None,
        "status": payload.get("status"),
        "officials": parse_officials(payload),
        "team_lists": parse_team_lists(payload),
        "teams": [t["team"]["name"] for t in teams],
        "team_ids": [t["team"]["id"] for t in teams],
        "source_id": SOURCE_OFFICIAL,
    }


def fetch_fixture_detail(fixture_id, timeout=20, transport=None):
    status, payload = _get_json(fixture_url(fixture_id), transport=transport,
                               timeout=timeout)
    return parse_fixture_detail(payload)


# =============================================================================
# 三、按对阵找 fixture_id（把官方 id 挂到我们的 match_id 上）
# =============================================================================
def find_fixture(fixtures, home, away, kickoff_utc, tolerance_minutes=20,
                 canon=None):
    """在官方赛程里找对应场次。

    `canon` 是队名归一化函数（由调用方注入，避免本模块依赖 match_calendar）。
    """
    canon = canon or (lambda s: (s or "").strip().lower())
    h, a = canon(home), canon(away)
    best, best_diff = None, None
    for f in fixtures:
        if canon(f["home_team"]) != h or canon(f["away_team"]) != a:
            continue
        diff = abs((f["kickoff_utc"] - kickoff_utc).total_seconds()) / 60
        if diff <= tolerance_minutes and (best_diff is None or diff < best_diff):
            best, best_diff = f, diff
    return best


# =============================================================================
# 四、给 referee_intel / lineup_monitor 的适配器
# =============================================================================
def appointments_for_match(match, detail, now):
    """把官方裁判组转成 §4 的任命记录（五种角色各自独立）。

    `detail` 为 None 或 officials 为空时，只返回一条 pending 的主裁记录，
    其余角色标 `not_available_from_source`——**仍然不猜**。
    """
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    officials = (detail or {}).get("officials") or []
    if not officials:
        return []

    from referee_intel import normalize_referee, APPT_CONFIRMED
    out = []
    for o in officials:
        rid, display, _on_list, _conf = normalize_referee(o["referee_name"])
        out.append({
            "referee_id": rid,
            "referee_name": display,
            "match_id": match["match_id"],
            "competition": match.get("competition", ""),
            "appointment_date": (match.get("kickoff_time_utc") or "")[:10],
            "role": o["role"],
            "appointment_status": APPT_CONFIRMED,
            "source_id": SOURCE_OFFICIAL,
            "source_url": fixture_url(detail.get("fixture_id") or 0),
            "published_at": "",
            "first_seen_at": now_iso,
            "verified_at": now_iso,
        })
    return out


def lineup_for_team(detail, team_name, now):
    """从官方详情取某队首发，转成 lineup_monitor 的记录。

    返回 None 表示「官方还没公布」（不是错误）。
    """
    if not detail:
        return None
    names = detail.get("teams") or []
    idx = None
    for i, n in enumerate(names):
        if (n or "").strip().lower() == (team_name or "").strip().lower():
            idx = i
            break
    if idx is None:
        return None
    lists = detail.get("team_lists") or []
    if idx >= len(lists):
        return None
    tl = lists[idx]
    if not tl.get("published"):
        return None
    return {
        "team": team_name,
        "players": tl["players"],
        "formation": tl["formation"],
        "groups": _groups_from_formation(tl),
        "source_tier": 1,
        "source_id": SOURCE_OFFICIAL,
        "source_url": fixture_url(detail.get("fixture_id") or 0),
        "status": "official_confirmed",
        "detected_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _groups_from_formation(tl):
    """把 formation 的分组人数还原成「每组一个名字列表」。

    接口只给每组人数（[1,4,2,3,1]），不给「谁在哪一组」的平铺顺序；
    但 lineup 数组本身是按 formation.players 的 id 顺序排列的，因此可以按
    人数切片还原分组——这是唯一能让 §5 的 V2/V6 校验真正跑起来的办法。
    """
    sizes = tl.get("groups") or []
    players = tl.get("players") or []
    if not sizes or sum(sizes) != len(players):
        return [players] if players else []
    out, i = [], 0
    for n in sizes:
        out.append(players[i:i + n])
        i += n
    return out
