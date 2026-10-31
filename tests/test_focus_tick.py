"""焦点战 tick 执行体 —— 离线测试（§5 运行时接入）。

`scripts/focus_tick.py` 是整个 5 小时自循环 job 的心脏：每 45 秒被 shell
叫一次，自己决定「现在该不该打官方接口」「这一版首发是不是已经推过」。
它一旦出错，不是报个错就完，而是**要么重复推送、要么整场漏推**。

因此这里全部离线：日历、官方接口、对手资讯三个网络层都用假 transport 注入，
断言的是「调用了几次」「推了几条」「推的文案里写了什么」这类可证伪的事实。
"""

import calendar
import datetime
import json
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import competitor_watch as cw          # noqa: E402
import lineup_monitor as lm            # noqa: E402
import match_calendar as mc            # noqa: E402
import pl_api                          # noqa: E402
import focus_tick as ft                # noqa: E402


# =============================================================================
# 时间基准
# =============================================================================
# S 级：Liverpool(1) vs Arsenal(2)，争冠对话 3 + 传统强队 2 + 榜面 1 = 6
KICK_S = datetime.datetime(2026, 11, 7, 17, 30)
NOW_S = datetime.datetime(2026, 11, 7, 16, 0)        # T-90 → intensify
# B 级：Brighton(19) vs Brentford(18)，保级 2 分
KICK_B = datetime.datetime(2026, 11, 8, 15, 0)
NOW_B = datetime.datetime(2026, 11, 8, 14, 0)        # T-60 → critical

EPOCH_S = calendar.timegm((2026, 11, 7, 17, 30, 0)) * 1000      # 官方接口给毫秒
EPOCH_B = calendar.timegm((2026, 11, 8, 15, 0, 0)) * 1000


# =============================================================================
# 夹具一：两个赛程源
# =============================================================================
FD_JSON = json.dumps([
    {"MatchNumber": 11, "RoundNumber": 11,
     "DateUtc": "2026-11-07 17:30:00Z", "Location": "Anfield",
     "HomeTeam": "Liverpool", "AwayTeam": "Arsenal",
     "HomeTeamScore": None, "AwayTeamScore": None, "Winner": None},
    {"MatchNumber": 12, "RoundNumber": 12,
     "DateUtc": "2026-11-08 15:00:00Z", "Location": "Amex",
     "HomeTeam": "Brighton", "AwayTeam": "Brentford",
     "HomeTeamScore": None, "AwayTeamScore": None, "Winner": None},
])

FD_FIXTURES = """Div,Date,Time,HomeTeam,AwayTeam,Referee
E0,07/11/2026,17:30,Liverpool,Arsenal,M Oliver
E0,08/11/2026,15:00,Brighton,Brentford,A Madley
"""

# 12 场有分的比赛 + 8 支 0 分球队 → 20 队积分榜，让「倒数 6 名」有真实边界
FD_RESULTS = """Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,Referee,HY,AY,HR,AR
E0,15/08/2026,15:00,Liverpool,Leeds,3,0,M Oliver,1,1,0,0
E0,15/08/2026,15:00,Arsenal,Coventry,2,0,S Attwell,2,1,0,0
E0,15/08/2026,15:00,Man City,Brighton,1,0,C Kavanagh,3,2,0,0
E0,15/08/2026,15:00,Chelsea,Tottenham,2,1,P Bankes,1,3,0,0
E0,15/08/2026,15:00,Newcastle,Everton,1,0,A Taylor,2,2,0,0
E0,15/08/2026,15:00,Aston Villa,Fulham,1,1,D England,1,1,0,0
E0,15/08/2026,15:00,West Ham,Wolves,2,0,J Brooks,0,2,0,0
E0,15/08/2026,15:00,Crystal Palace,Sunderland,1,1,T Bramall,2,0,0,0
E0,15/08/2026,15:00,Bournemouth,Brentford,2,0,M Oliver,1,1,0,0
E0,15/08/2026,15:00,Nott'm Forest,Man United,1,0,S Hooper,3,1,0,0
E0,22/08/2026,15:00,Liverpool,Brighton,2,0,A Madley,1,2,0,0
E0,22/08/2026,15:00,Arsenal,Chelsea,1,1,M Oliver,2,2,0,0
"""


# =============================================================================
# 夹具二：官方接口
# =============================================================================
def _team(name, tid):
    return {"team": {"id": tid, "name": name}}


def officials_payload(main="Michael Oliver", var="Stuart Attwell"):
    return [
        {"id": 1, "name": {"display": main}, "role": "MAIN"},
        {"id": 2, "name": {"display": "Simon Bennett"}, "role": None},   # 助理无 role 字段
        {"id": 3, "name": {"display": "James Mainwaring"}, "role": None},
        {"id": 4, "name": {"display": var}, "role": "VAR"},
        {"id": 5, "name": {"display": "Neil Davies"}, "role": "ASSISTANT_VAR"},
        {"id": 6, "name": {"display": "Anthony Taylor"}, "role": "FOURTH_OFFICIAL"},
    ]


LIVERPOOL_XI = ["Alisson", "Alexander-Arnold", "Konate", "Van Dijk", "Robertson",
                "Mac Allister", "Gravenberch", "Szoboszlai",
                "Salah", "Gakpo", "Diaz"]
ARSENAL_XI = ["Raya", "Timber", "Saliba", "Gabriel", "Calafiori",
              "Rice", "Merino", "Odegaard", "Saka", "Havertz", "Martinelli"]


def team_list(team_id, names, label="4-3-3", sizes=(1, 4, 3, 3)):
    return {
        "teamId": team_id,
        "lineup": [{"name": {"display": n}} for n in names],
        "formation": {"label": label,
                      "players": [list(range(i, i + n)) for i, n in enumerate(sizes)]},
        "substitutes": [{"name": {"display": "Sub One"}}],
    }


def detail_s(referee=True, lineups=True, home_xi=None, away_xi=None,
             home_name="Liverpool"):
    payload = {
        "id": 999001,
        "kickoff": {"millis": EPOCH_S},
        "status": "U",
        "teams": [_team(home_name, 1), _team("Arsenal", 2)],
    }
    payload["matchOfficials"] = officials_payload() if referee else []
    if lineups:
        payload["teamLists"] = [
            team_list(1, home_xi or LIVERPOOL_XI),
            team_list(2, away_xi or ARSENAL_XI),
        ]
    else:
        # 实测：未开赛时接口返回的是 [null, null]，不是空对象
        payload["teamLists"] = [None, None]
    return payload


def detail_b():
    return {
        "id": 999002,
        "kickoff": {"millis": EPOCH_B},
        "status": "U",
        "teams": [_team("Brighton", 3), _team("Brentford", 4)],
        "matchOfficials": [],
        "teamLists": [None, None],
    }


def fixtures_payload():
    return {"content": [
        {"id": 999001, "kickoff": {"millis": EPOCH_S}, "status": "U",
         "teams": [_team("Liverpool", 1), _team("Arsenal", 2)],
         "gameweek": {"gameweek": 11}, "ground": {"name": "Anfield"}},
        {"id": 999002, "kickoff": {"millis": EPOCH_B}, "status": "U",
         "teams": [_team("Brighton", 3), _team("Brentford", 4)],
         "gameweek": {"gameweek": 12}, "ground": {"name": "Amex"}},
    ]}


# =============================================================================
# 夹具三：对手资讯（懂球帝一条，比我们晚 32 秒）
# =============================================================================
DQD = {"articles": [
    {"title": "利物浦vs阿森纳首发：萨拉赫领衔，维尔茨先发",
     "published_at": "2026-11-08 00:00:32",     # → 16:00:32Z
     "created_at": "2026-11-08 00:00:32",
     "share": "https://www.dongqiudi.com/article/9"},
]}


# =============================================================================
# 传输层
# =============================================================================
class Recorder:
    """记录每次请求的 URL；未登记的 URL 抛异常（模拟网络故障）。"""

    def __init__(self, routes=None):
        self.routes = dict(routes or base_routes())
        self.calls = []

    def __call__(self, url, timeout=20):
        self.calls.append(url)
        if url not in self.routes:
            raise ConnectionError(f"no route: {url}")
        return self.routes[url]

    def count(self, url):
        return self.calls.count(url)

    def set(self, url, value):
        self.routes[url] = value


def base_routes():
    return {
        mc.URL_FIXTUREDOWNLOAD: (200, FD_JSON),
        mc.URL_FD_FIXTURES: (200, FD_FIXTURES),
        mc.URL_FD_RESULTS.format(code=mc.season_code(mc.CURRENT_SEASON)): (200, FD_RESULTS),
        pl_api.fixtures_url(): (200, json.dumps(fixtures_payload())),
        pl_api.fixture_url(999001): (200, json.dumps(detail_s())),
        pl_api.fixture_url(999002): (200, json.dumps(detail_b())),
        cw.SOURCES[cw.SOURCE_DONGQIUDI]["url"]: (200, json.dumps(DQD)),
        cw.SOURCES[cw.SOURCE_HUPU]["url"]: (500, ""),
        cw.SOURCES[cw.SOURCE_ZHIBO8]["url"]: (500, ""),
    }


class Sink:
    """假 Telegram。"""

    def __init__(self, ok=True):
        self.ok = ok
        self.sent = []

    def __call__(self, text):
        self.sent.append(text)
        return self.ok

    def joined(self):
        return "\n\n".join(self.sent)


def deps(rec, now=NOW_S, sink=None):
    return ft.Deps(transport=rec, telegram=sink or Sink(), now=now)


class Base(unittest.TestCase):
    def setUp(self):
        # 三个源之间的礼貌间隔在测试里没必要真睡
        self._old_delay = ft.COMPETITOR_DELAY_SECONDS
        ft.COMPETITOR_DELAY_SECONDS = 0.0
        # 缓存跨 tick 复用（真实运行时它落在 .focus_cache.json 里）
        self.cache = ft.blank_cache()

    def tearDown(self):
        ft.COMPETITOR_DELAY_SECONDS = self._old_delay

    def tick(self, *args, **kwargs):
        kwargs.setdefault("cache", self.cache)
        return ft.tick(*args, **kwargs)


# =============================================================================
# A. 状态
# =============================================================================
class TestState(Base):
    def test_blank_state_shape(self):
        """状态里只留「重启后不能丢」的：已推送标记、指纹、时间。"""
        st = ft.blank_state()
        for k in ("matches", "last_tick_at", "ticks"):
            self.assertIn(k, st)
        self.assertEqual(st["ticks"], 0)

    def test_state_has_no_cache_fields(self):
        """缓存 176KB，绝不能混进每 15 分钟提交一次的状态文件。"""
        st = ft.blank_state()
        for k in ("calendar", "official_fixtures"):
            self.assertNotIn(k, st)

    def test_legacy_cache_fields_are_stripped(self):
        """老版本把缓存写进了 state，第一次跑要自动瘦身。"""
        state = {ft.STATE_KEY: {
            "ticks": 7, "calendar": {"big": "x"}, "official_fixtures": [1] * 100,
            "calendar_at": "2026-11-07T15:00:00Z", "competitor_at": "x",
        }}
        sub = ft.ensure(state)
        self.assertEqual(sub["ticks"], 7)
        for k in ft.LEGACY_STATE_KEYS:
            self.assertNotIn(k, sub)

    def test_cache_roundtrip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.json")
            c = ft.blank_cache()
            c["calendar"] = {"focus_matches": [{"a": 1}]}
            c["calendar_at"] = "2026-11-07T16:00:00Z"
            ft.save_cache(c, path)
            back = ft.load_cache(path)
            self.assertEqual(back["calendar"], {"focus_matches": [{"a": 1}]})
            self.assertEqual(back["calendar_at"], "2026-11-07T16:00:00Z")

    def test_missing_or_broken_cache_is_not_fatal(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(ft.load_cache(os.path.join(d, "nope.json")),
                             ft.blank_cache())
            bad = os.path.join(d, "bad.json")
            with open(bad, "w", encoding="utf-8") as f:
                f.write("{not json")
            self.assertEqual(ft.load_cache(bad), ft.blank_cache())

    def test_ensure_creates_subtree(self):
        state = {}
        sub = ft.ensure(state)
        self.assertIs(state[ft.STATE_KEY], sub)
        self.assertEqual(sub["ticks"], 0)

    def test_ensure_repairs_missing_keys_without_overwriting(self):
        """老版本 state 里可能缺字段；补默认值，但绝不能把已有值冲掉。"""
        state = {ft.STATE_KEY: {"ticks": 42, "matches": {"m1": {"referee_pushed": True}}}}
        sub = ft.ensure(state)
        self.assertEqual(sub["ticks"], 42)
        self.assertEqual(sub["matches"], {"m1": {"referee_pushed": True}})
        self.assertIn("last_tick_at", sub)

    def test_match_state_defaults(self):
        sub = ft.blank_state()
        ms = ft.match_state(sub, "epl-1")
        self.assertFalse(ms["referee_pushed"])
        self.assertEqual(ms["lineup_pushed"], {})
        self.assertIs(sub["matches"]["epl-1"], ms)

    def test_tick_does_not_touch_other_state_keys(self):
        """焦点战必须与新闻扫描的状态完全隔离，否则两条流水线会互相踩。"""
        state = {"processed_urls": ["u1"], "news_pool": [{"a": 1}], "events": []}
        self.tick(state, deps(Recorder()))
        self.assertEqual(state["processed_urls"], ["u1"])
        self.assertEqual(state["news_pool"], [{"a": 1}])
        self.assertEqual(set(state), {"processed_urls", "news_pool", "events", ft.STATE_KEY})

    def test_main_uses_separate_state_file(self):
        """6 小时循环若写 state.json，会和每 5 分钟提交一次的扫描 job 反复冲突。"""
        with open(os.path.join(ROOT, "scripts", "focus_tick.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('"focus_state.json"', src)
        self.assertNotIn("bot.load_state()", src)
        self.assertNotIn("bot.save_state(", src)


# =============================================================================
# B. 日历缓存
# =============================================================================
class TestCalendarCache(Base):
    def test_calendar_fetched_once_within_ttl(self):
        rec = Recorder()
        state = {}
        self.tick(state, deps(rec))
        self.tick(state, deps(rec))
        self.assertEqual(rec.count(mc.URL_FIXTUREDOWNLOAD), 1)
        self.assertEqual(state[ft.STATE_KEY]["ticks"], 2)

    def test_calendar_refetched_after_ttl(self):
        rec = Recorder()
        state = {}
        self.tick(state, deps(rec))
        later = NOW_S + datetime.timedelta(minutes=ft.CALENDAR_TTL_MINUTES + 1)
        self.tick(state, deps(rec, now=later))
        self.assertEqual(rec.count(mc.URL_FIXTUREDOWNLOAD), 2)

    def test_calendar_flag_reports_cache_hit(self):
        rec = Recorder()
        state = {}
        first = self.tick(state, deps(rec))
        second = self.tick(state, deps(rec))
        self.assertEqual(first["calendar"], "fresh")
        self.assertEqual(second["calendar"], "cached")

    def test_official_fixture_list_cached_separately(self):
        """380 场官方赛程不能每拍都抓；缓存 6 小时。"""
        rec = Recorder()
        state = {}
        self.tick(state, deps(rec))
        self.tick(state, deps(rec, now=NOW_S + datetime.timedelta(minutes=5)))
        self.assertEqual(rec.count(pl_api.fixtures_url()), 1)

    def test_empty_calendar_does_not_crash(self):
        rec = Recorder()
        rec.set(mc.URL_FIXTUREDOWNLOAD, (500, ""))
        rec.set(mc.URL_FD_FIXTURES, (500, ""))
        rec.set(mc.URL_FD_RESULTS.format(code=mc.season_code(mc.CURRENT_SEASON)),
                (500, ""))
        summary = self.tick({}, deps(rec))
        self.assertEqual(summary["calendar"], "empty")
        self.assertEqual(summary["pushed"], [])


# =============================================================================
# C. 监控窗口
# =============================================================================
class TestWindow(Base):
    def test_idle_match_does_not_hit_official_api(self):
        """开球前 5 小时（idle）不该打官方接口——省请求，也少一个被封的理由。"""
        rec = Recorder()
        state = {}
        far = KICK_S - datetime.timedelta(hours=5)
        summary = self.tick(state, deps(rec, now=far))
        self.assertEqual(summary["checked"], 0)
        self.assertEqual(rec.count(pl_api.fixture_url(999001)), 0)
        self.assertEqual(summary["phase"][mc.make_match_id("Liverpool", "Arsenal", KICK_S)],
                         lm.PHASE_IDLE)

    def test_intensify_window_checks_match(self):
        rec = Recorder()
        summary = self.tick({}, deps(rec))
        mid = mc.make_match_id("Liverpool", "Arsenal", KICK_S)
        self.assertEqual(summary["phase"][mid], lm.PHASE_INTENSIFY)
        self.assertEqual(summary["checked"], 1)
        self.assertEqual(rec.count(pl_api.fixture_url(999001)), 1)

    def test_b_grade_monitors_referee_only(self):
        """B 级只监控裁判，不抓首发——否则会在无意义的场次上打满请求。"""
        rec = Recorder()
        state = {}
        summary = self.tick(state, deps(rec, now=NOW_B))
        bid = mc.make_match_id("Brighton", "Brentford", KICK_B)
        self.assertEqual(summary["phase"][bid], lm.PHASE_CRITICAL)
        self.assertEqual(summary["checked"], 1)
        self.assertEqual(rec.count(pl_api.fixture_url(999002)), 1)
        # 没有抓对手资讯
        self.assertEqual(rec.count(cw.SOURCES[cw.SOURCE_DONGQIUDI]["url"]), 0)
        self.assertEqual(summary["pushed"], [])

    def test_max_matches_limits_api_calls_not_calendar_scan(self):
        """max_matches 限制的是打官方接口的场次数；窗口内的场次不能被挤出队列。"""
        rec = Recorder()
        state = {}
        summary = self.tick(state, deps(rec), max_matches=1)
        self.assertEqual(summary["checked"], 1)


# =============================================================================
# D. 裁判
# =============================================================================
class TestReferee(Base):
    def test_referee_report_pushed_once(self):
        rec = Recorder()
        sink = Sink()
        state = {}
        first = self.tick(state, deps(rec, sink=sink))
        second = self.tick(state, deps(rec, now=NOW_S + datetime.timedelta(seconds=45),
                                    sink=sink))
        self.assertTrue(any(":referee" in p for p in first["pushed"]))
        self.assertEqual(second["pushed"], [])
        self.assertEqual(len([t for t in sink.sent if "主裁" in t]), 1)

    def test_referee_not_pushed_when_officials_empty(self):
        """T-130 分钟官方还没任命是常态，不能因此推一条空报告。"""
        rec = Recorder()
        rec.set(pl_api.fixture_url(999001),
                (200, json.dumps(detail_s(referee=False))))
        sink = Sink()
        summary = self.tick({}, deps(rec, sink=sink))
        self.assertEqual([p for p in summary["pushed"] if ":referee" in p], [])
        self.assertNotIn("主裁", sink.joined())

    def test_referee_failure_is_retried_next_tick(self):
        """推送失败不能标记为已推，否则这场就永远不推了。"""
        rec = Recorder()
        bad = Sink(ok=False)
        state = {}
        first = self.tick(state, deps(rec, sink=bad))
        self.assertEqual(first["pushed"], [])
        good = Sink()
        second = self.tick(state, deps(rec, now=NOW_S + datetime.timedelta(seconds=45),
                                    sink=good))
        self.assertTrue(any(":referee" in p for p in second["pushed"]))

    def test_referee_text_names_main_referee_not_var(self):
        """VAR 判罚不得算作主裁个人判罚——文案里主裁必须是 Michael Oliver。"""
        rec = Recorder()
        sink = Sink()
        self.tick({}, deps(rec, sink=sink))
        text = sink.joined()
        self.assertIn("Michael Oliver", text)
        self.assertIn("Stuart Attwell", text)          # VAR 也要列出来
        self.assertNotIn("主裁：Stuart Attwell", text)

    def test_watchlist_marker_only_for_watch_referee(self):
        rec = Recorder()
        sink = Sink()
        self.tick({}, deps(rec, sink=sink))
        # 主裁 Oliver 不在七人名单 → 不该出现观察名单提示
        self.assertNotIn("观察名单", sink.joined())

    def test_watchlist_marker_appears_when_main_is_on_list(self):
        rec = Recorder()
        rec.set(pl_api.fixture_url(999001), (200, json.dumps(
            detail_s(referee=True) | {})))
        payload = detail_s()
        payload["matchOfficials"] = officials_payload(main="Stuart Attwell",
                                                      var="Michael Oliver")
        rec.set(pl_api.fixture_url(999001), (200, json.dumps(payload)))
        sink = Sink()
        self.tick({}, deps(rec, sink=sink))
        self.assertIn("观察名单", sink.joined())

    def test_referee_text_carries_focus_level_and_beijing_time(self):
        rec = Recorder()
        sink = Sink()
        self.tick({}, deps(rec, sink=sink))
        text = sink.joined()
        self.assertIn("焦点等级", text)
        self.assertIn("北京", text)

    def test_appointment_lead_time_recorded_as_observation(self):
        """记的是「我方首次看到」的时刻，文案里必须写清楚不是官方公布时刻。

        实测：官方接口 T-69 分钟时 officials 仍是空数组，任命临开球才可见。
        源里没有 published_at 字段，填一个就是编造。
        """
        rec = Recorder()
        sink = Sink()
        state = {}
        self.tick(state, deps(rec, sink=sink))
        text = sink.joined()
        self.assertIn("开球前 90 分钟", text)
        self.assertIn("不做推断", text)
        self.assertNotIn("官方公布时间：", text)

        sub = ft.ensure(state)
        mid = mc.make_match_id("Liverpool", "Arsenal", KICK_S)
        self.assertEqual(sub["matches"][mid]["appointment_seen_at"],
                         "2026-11-07T16:00:00Z")
        self.assertEqual(sub["matches"][mid]["appointment_seen_lead_minutes"], 90)


# =============================================================================
# E. 首发
# =============================================================================
class TestLineup(Base):
    def _lineup_sent(self, sink):
        return [t for t in sink.sent if t.startswith("【首发】")]

    def test_lineup_pushed_as_official_confirmed(self):
        rec = Recorder()
        sink = Sink()
        summary = self.tick({}, deps(rec, sink=sink))
        self.assertTrue(any("lineup:Liverpool" in p for p in summary["pushed"]))
        self.assertTrue(any("lineup:Arsenal" in p for p in summary["pushed"]))
        for text in self._lineup_sent(sink):
            self.assertIn("Official Lineup Confirmed", text)
            self.assertIn("4-3-3", text)

    def test_lineup_deduped_by_players_hash(self):
        rec = Recorder()
        sink = Sink()
        state = {}
        self.tick(state, deps(rec, sink=sink))
        n1 = len(self._lineup_sent(sink))
        self.tick(state, deps(rec, now=NOW_S + datetime.timedelta(seconds=45), sink=sink))
        self.assertEqual(len(self._lineup_sent(sink)), n1)
        self.assertEqual(n1, 2)

    def test_lineup_new_version_pushes_again(self):
        """换人后指纹变了，必须重推——否则用户看到的是过期名单。"""
        rec = Recorder()
        sink = Sink()
        state = {}
        self.tick(state, deps(rec, sink=sink))
        n1 = len(self._lineup_sent(sink))

        changed = list(LIVERPOOL_XI)
        changed[-1] = "Nunez"
        rec.set(pl_api.fixture_url(999001),
                (200, json.dumps(detail_s(home_xi=changed))))
        self.tick(state, deps(rec, now=NOW_S + datetime.timedelta(seconds=45), sink=sink))
        self.assertEqual(len(self._lineup_sent(sink)), n1 + 1)
        self.assertIn("Nunez", self._lineup_sent(sink)[-1])

    def test_not_published_is_not_an_error(self):
        """未开赛返回 [null, null]：不推、不报错、不炸。"""
        rec = Recorder()
        rec.set(pl_api.fixture_url(999001),
                (200, json.dumps(detail_s(lineups=False))))
        sink = Sink()
        summary = self.tick({}, deps(rec, sink=sink))
        self.assertEqual([p for p in summary["pushed"] if "lineup" in p], [])
        self.assertEqual(self._lineup_sent(sink), [])
        self.assertEqual(summary["errors"], [])

    def test_overlap_blocks_and_publishes_link_only(self):
        """同一球员同时出现在两队 → 阻断，只发官方链接，不发名单。"""
        rec = Recorder()
        bad = list(LIVERPOOL_XI)
        bad[-1] = "Saka"                       # 与阿森纳重叠
        rec.set(pl_api.fixture_url(999001),
                (200, json.dumps(detail_s(home_xi=bad))))
        sink = Sink()
        self.tick({}, deps(rec, sink=sink))
        liverpool = [t for t in self._lineup_sent(sink) if "Liverpool" in t]
        self.assertTrue(liverpool)
        self.assertIn("暂不发布名单", liverpool[0])
        self.assertIn(pl_api.fixture_url(999001), liverpool[0])
        self.assertNotIn("Official Lineup Confirmed", liverpool[0])

    def test_ten_players_blocks_and_publishes_link_only(self):
        rec = Recorder()
        rec.set(pl_api.fixture_url(999001),
                (200, json.dumps(detail_s(home_xi=LIVERPOOL_XI[:10]))))
        sink = Sink()
        self.tick({}, deps(rec, sink=sink))
        liverpool = [t for t in self._lineup_sent(sink) if "Liverpool" in t]
        self.assertTrue(liverpool)
        self.assertIn("暂不发布名单", liverpool[0])
        self.assertIn("V1", liverpool[0])

    def test_link_only_push_is_recorded_to_avoid_spam(self):
        """被阻断的版本只发官方链接，但**要记指纹**。

        不记的话，每 45 秒就会把同一条「解析未通过，请看官方链接」再推一遍。
        记下来之后，一旦官方修正（指纹变了）仍会正常重推。
        """
        rec = Recorder()
        bad = list(LIVERPOOL_XI)
        bad[-1] = "Saka"
        rec.set(pl_api.fixture_url(999001),
                (200, json.dumps(detail_s(home_xi=bad))))
        state = {}
        sink = Sink()
        self.tick(state, deps(rec, sink=sink))
        sub = ft.ensure(state)
        mid = mc.make_match_id("Liverpool", "Arsenal", KICK_S)
        self.assertIn("Liverpool", sub["matches"][mid]["lineup_pushed"])

        # 官方修正后必须重推（指纹变了）
        rec.set(pl_api.fixture_url(999001), (200, json.dumps(detail_s())))
        n1 = len([t for t in sink.sent if t.startswith("【首发】")])
        self.tick(state, deps(rec, now=NOW_S + datetime.timedelta(seconds=45), sink=sink))
        self.assertGreater(len([t for t in sink.sent if t.startswith("【首发】")]), n1)

    def test_official_alias_maps_to_our_team_name(self):
        """官方写 Spurs、我们写 Tottenham 时，绝不能把 Spurs 的首发挂到别的队名下。"""
        rec = Recorder()
        payload = detail_s()
        payload["teams"] = [_team("Spurs", 1), _team("Arsenal", 2)]
        rec.set(pl_api.fixture_url(999001), (200, json.dumps(payload)))
        sink = Sink()
        summary = self.tick({}, deps(rec, sink=sink))
        lineup_pushes = [p for p in summary["pushed"] if "lineup" in p]
        # Spurs 对不上本场任何一队 → 直接跳过；只有 Arsenal 该被推
        self.assertEqual(lineup_pushes, [
            f"{mc.make_match_id('Liverpool', 'Arsenal', KICK_S)}:lineup:Arsenal"])
        text = "\n".join(t for t in sink.sent if t.startswith("【首发】"))
        self.assertIn("Raya", text)
        self.assertNotIn("Alisson", text)      # 绝不能把 Spurs 的名单当成 Arsenal 的

    def test_speed_record_has_three_timestamps(self):
        rec = Recorder()
        sink = Sink()
        self.tick({}, deps(rec, sink=sink))
        self.assertIn("⏱", sink.joined())
        self.assertIn("领先 32 秒", sink.joined())


# =============================================================================
# F. 对手采集节流
# =============================================================================
class TestCompetitorThrottle(Base):
    def test_competitor_not_refetched_within_interval(self):
        rec = Recorder()
        state = {}
        self.tick(state, deps(rec))
        n1 = rec.count(cw.SOURCES[cw.SOURCE_DONGQIUDI]["url"])
        self.tick(state, deps(rec, now=NOW_S + datetime.timedelta(seconds=60)))
        self.assertEqual(rec.count(cw.SOURCES[cw.SOURCE_DONGQIUDI]["url"]), n1)

    def test_competitor_refetched_after_interval(self):
        rec = Recorder()
        state = {}
        self.tick(state, deps(rec))
        n1 = rec.count(cw.SOURCES[cw.SOURCE_DONGQIUDI]["url"])
        later = NOW_S + datetime.timedelta(seconds=ft.COMPETITOR_MIN_INTERVAL_SECONDS + 5)
        self.tick(state, deps(rec, now=later))
        self.assertEqual(rec.count(cw.SOURCES[cw.SOURCE_DONGQIUDI]["url"]), n1 + 1)

    def test_competitor_throttle_is_per_match(self):
        """节流按场次算。放全局的话，一场刷完其余场次全被跳过，
        多场同时进窗口时只有第一场能拿到对手时间。"""
        rec = Recorder()
        state = {}
        self.tick(state, deps(rec, now=NOW_B))       # B 级：只监控裁判，不采对手
        self.assertEqual(rec.count(cw.SOURCES[cw.SOURCE_DONGQIUDI]["url"]), 0)

        # 手工塞两场都进了窗口的日历，验证两场各自采一次
        self.cache["calendar"] = {
            "focus_matches": [
                {"match_id": "m1", "kickoff_time_utc": "2026-11-07T17:30:00Z",
                 "home_team": "Liverpool", "away_team": "Arsenal",
                 "focus_level": "S", "kickoff_time_beijing": "2026-11-08 00:30",
                 "lineup_monitor_enabled": True, "referee_monitor_enabled": False},
                {"match_id": "m2", "kickoff_time_utc": "2026-11-07T17:30:00Z",
                 "home_team": "Liverpool", "away_team": "Arsenal",
                 "focus_level": "S", "kickoff_time_beijing": "2026-11-08 00:30",
                 "lineup_monitor_enabled": True, "referee_monitor_enabled": False},
            ],
            "calendar_at": "2026-11-07T16:00:00Z",
        }
        rec2 = Recorder()
        state2 = {}
        self.tick(state2, deps(rec2))
        self.assertEqual(rec2.count(cw.SOURCES[cw.SOURCE_DONGQIUDI]["url"]), 2)

    def test_no_speed_claim_without_competitor_time(self):
        """没采到对手时间就不许写「领先」——这是任务书明令的纪律。"""
        rec = Recorder()
        rec.set(cw.SOURCES[cw.SOURCE_DONGQIUDI]["url"], (500, ""))
        sink = Sink()
        self.tick({}, deps(rec, sink=sink))
        text = sink.joined()
        self.assertNotIn("⏱", text)
        self.assertNotIn("领先", text)

    def test_competitor_failure_does_not_break_lineup_push(self):
        rec = Recorder()
        rec.set(cw.SOURCES[cw.SOURCE_DONGQIUDI]["url"], (500, ""))
        rec.set(cw.SOURCES[cw.SOURCE_HUPU]["url"], (500, ""))
        rec.set(cw.SOURCES[cw.SOURCE_ZHIBO8]["url"], (500, ""))
        summary = self.tick({}, deps(rec, sink=Sink()))
        self.assertTrue(any("lineup:Liverpool" in p for p in summary["pushed"]))


# =============================================================================
# G. 下一拍间隔
# =============================================================================
class TestNextSleep(Base):
    def test_default_when_no_calendar(self):
        self.assertEqual(ft.next_sleep_seconds({}, NOW_S), 60)

    def test_uses_min_interval_across_matches(self):
        """两场同时进入窗口时取更紧的那个节拍，不能因为一场 idle 就整体变慢。"""
        state = {}
        self.tick(state, deps(Recorder()))
        self.cache["calendar"]["focus_matches"].append({
            "match_id": "x", "kickoff_time_utc": "2026-11-07T16:30:00Z",
            "lineup_monitor_enabled": True, "referee_monitor_enabled": True,
        })
        # 16:00 时：Liverpool T-90（intensify 60s），x 场 T-30（critical 30s）
        self.assertEqual(ft.next_sleep_seconds(self.cache, NOW_S), 30)

    def test_returns_int(self):
        state = {}
        self.tick(state, deps(Recorder()))
        self.assertIsInstance(ft.next_sleep_seconds(self.cache, NOW_S), int)

    def test_clamped_to_safe_range(self):
        """小于 30 秒是往封禁上撞，大于 300 秒会错过首发。"""
        state = {}
        self.tick(state, deps(Recorder()))
        self.cache["calendar"]["focus_matches"] = [{
            "match_id": "x", "kickoff_time_utc": "2026-11-07T16:01:00Z",
            "lineup_monitor_enabled": True, "referee_monitor_enabled": True,
        }]
        sec = ft.next_sleep_seconds(self.cache, NOW_S)      # T-1 分钟 → critical 30s
        self.assertGreaterEqual(sec, ft.INTERVAL_MIN)
        self.assertLessEqual(sec, ft.INTERVAL_MAX)

    def test_idle_interval_is_capped(self):
        state = {}
        self.tick(state, deps(Recorder()))
        self.cache["calendar"]["focus_matches"] = [{
            "match_id": "x", "kickoff_time_utc": "2026-11-20T15:00:00Z",
            "lineup_monitor_enabled": True, "referee_monitor_enabled": True,
        }]
        self.assertEqual(ft.next_sleep_seconds(self.cache, NOW_S), ft.INTERVAL_MAX)

    def test_interval_file_written_atomically(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = ft.write_interval_file(30, root=d)
            self.assertTrue(os.path.exists(path))
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), "30")
            # 不能留半截临时文件
            self.assertFalse(os.path.exists(path + ".tmp"))

    def test_interval_file_is_gitignored(self):
        """每次 tick 都变的值不能进仓库，否则每 30 秒一个提交。"""
        with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as f:
            self.assertIn(ft.INTERVAL_FILE, f.read())

    def test_cache_file_is_gitignored(self):
        """缓存 176KB，每 15 分钟提交一次就是每天 3.5MB 的 git 对象。"""
        with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as f:
            self.assertIn(ft.CACHE_FILE, f.read())


# =============================================================================
# H. 时间戳解析
# =============================================================================
class TestTimeHelpers(Base):
    def test_parse_accepts_project_formats(self):
        for s in ("2026-11-07T16:00:00Z", "2026-11-07T16:00:00+00:00",
                  "2026-11-07T16:00:00.123456+00:00"):
            got = ft._parse(s)
            self.assertIsNotNone(got, s)
            self.assertEqual(got.replace(microsecond=0),
                             datetime.datetime(2026, 11, 7, 16, 0), s)

    def test_parse_rejects_garbage(self):
        self.assertIsNone(ft._parse(""))
        self.assertIsNone(ft._parse("not-a-time"))

    def test_iso_roundtrip(self):
        self.assertEqual(ft._iso(datetime.datetime(2026, 11, 7, 16, 0)),
                         "2026-11-07T16:00:00Z")


# =============================================================================
# I. 状态回写与合并
# =============================================================================
class TestStateMerge(unittest.TestCase):
    """focus_state.json 也走 commit_state.sh，rebase 失败时会进 merge_state。"""

    def setUp(self):
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import merge_state
        self.ms = merge_state

    def test_referee_pushed_is_or(self):
        local = {"focus": {"matches": {"m1": {"referee_pushed": False}}}}
        remote = {"focus": {"matches": {"m1": {"referee_pushed": True}}}}
        out = self.ms.merge(local, remote)
        self.assertTrue(out["focus"]["matches"]["m1"]["referee_pushed"])

    def test_lineup_pushed_is_union(self):
        local = {"focus": {"matches": {"m1": {"lineup_pushed": {"Liverpool": "aaa"}}}}}
        remote = {"focus": {"matches": {"m1": {"lineup_pushed": {"Arsenal": "bbb"}}}}}
        out = self.ms.merge(local, remote)
        self.assertEqual(out["focus"]["matches"]["m1"]["lineup_pushed"],
                         {"Liverpool": "aaa", "Arsenal": "bbb"})

    def test_first_seen_takes_earliest(self):
        """首次检测时间必须取更早的那个，否则速度证明会被自己改坏。"""
        local = {"focus": {"matches": {"m1": {"first_seen": {"Liverpool": "2026-11-07T16:00:00Z"}}}}}
        remote = {"focus": {"matches": {"m1": {"first_seen": {"Liverpool": "2026-11-07T15:59:30Z"}}}}}
        out = self.ms.merge(local, remote)
        self.assertEqual(out["focus"]["matches"]["m1"]["first_seen"]["Liverpool"],
                         "2026-11-07T15:59:30Z")

    def test_competitor_takes_earlier_sighting(self):
        """对手时间取更早的那份 = 更保守，不会把领先秒数吹大。"""
        local = {"focus": {"matches": {"m1": {"competitor": {
            "competitors_seen_at": "2026-11-07T16:00:40Z"}}}}}
        remote = {"focus": {"matches": {"m1": {"competitor": {
            "competitors_seen_at": "2026-11-07T16:00:32Z"}}}}}
        out = self.ms.merge(local, remote)
        self.assertEqual(out["focus"]["matches"]["m1"]["competitor"]["competitors_seen_at"],
                         "2026-11-07T16:00:32Z")

    def test_focus_subtree_survives_merge(self):
        """不处理 focus 键时，merge 会把远端整棵子树静默丢掉。"""
        local = {"focus": {"ticks": 3}}
        remote = {"focus": {"ticks": 9, "matches": {"m1": {"referee_pushed": True}}}}
        out = self.ms.merge(local, remote)
        self.assertEqual(out["focus"]["ticks"], 9)
        self.assertIn("m1", out["focus"]["matches"])

    def test_news_state_still_merges(self):
        """加了 focus 分支不能影响原有 state.json 的合并。"""
        local = {"processed_urls": ["a"], "events": {}}
        remote = {"processed_urls": ["b"], "events": {}}
        out = self.ms.merge(local, remote)
        self.assertEqual(out["processed_urls"], ["a", "b"])
        self.assertNotIn("focus", out)


class TestWorkflowFile(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(ROOT, ".github", "workflows", "focus.yml")
        with open(self.path, encoding="utf-8") as f:
            self.src = f.read()

    def test_job_has_write_permission(self):
        """workflow 级只有 read；job 不提权的话状态回写会全部失败，
        而循环本身照跑，看起来一切正常——这是最阴的一类故障。"""
        self.assertIn("contents: read", self.src)
        self.assertIn("contents: write", self.src)
        self.assertRegex(self.src, r"jobs:\s*\n\s+focus:\s*\n\s+permissions:")

    def test_independent_concurrency_group(self):
        """挂进 scanner.yml 的组会把这个 6 小时 job 变成全站串行锁。"""
        self.assertIn("group: focus-watch", self.src)
        self.assertIn("cancel-in-progress: false", self.src)

    def test_timeout_under_six_hours(self):
        m = re.search(r"timeout-minutes:\s*(\d+)", self.src)
        self.assertTrue(m, "未设置 timeout-minutes")
        self.assertLess(int(m.group(1)), 360)

    def test_has_schedule_fallback(self):
        self.assertIn("cron:", self.src)

    def test_loop_tick_failure_does_not_kill_loop(self):
        """某一拍失败要继续下一拍，否则一次网络抖动就废掉整晚。

        保证来自两点：`set -uo pipefail` **不带 -e**，以及失败分支只 warning 不 exit。
        """
        self.assertIn("set -uo pipefail", self.src)
        self.assertNotIn("set -euo pipefail", self.src)
        self.assertRegex(self.src, r"if ! python scripts/focus_tick\.py; then")
        self.assertIn("::warning title=Focus Tick Failed", self.src)
        self.assertNotIn("exit 1", self.src)

    def test_commits_focus_state_not_shared_state(self):
        self.assertIn("commit_state.sh focus_state.json", self.src)
        self.assertNotIn("commit_state.sh state.json", self.src)

    def test_loop_interval_comes_from_tick(self):
        """shell 写死间隔会让 focus_config 的 30/120/300 秒配置失效。"""
        self.assertIn(".focus_interval", self.src)
        self.assertIn("DEFAULT_INTERVAL", self.src)
        # 不能有行首的 INTERVAL=<数字> 硬编码（DEFAULT_INTERVAL=45 与夹取语句不算）
        self.assertIsNone(re.search(r"^\s*INTERVAL=\d+", self.src, re.MULTILINE))


if __name__ == "__main__":
    unittest.main(verbosity=2)
