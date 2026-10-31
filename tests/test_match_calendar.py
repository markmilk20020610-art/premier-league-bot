"""§3 比赛日历与焦点战识别 —— 测试。

全部离线：网络层用 `build_calendar(transport=...)` 注入假响应，
不依赖 football-data.co.uk / fixturedownload 的可用性。
真实数据的验证单独放在 `tests/live_probe_match_calendar.py`（不纳入 CI）。
"""

import datetime
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import focus_config as cfg          # noqa: E402
import match_calendar as mc         # noqa: E402


# =============================================================================
# 离线夹具：两个源各造一场，时间基准故意不同（一个 UTC，一个英国本地）
# =============================================================================
FD_JSON = json.dumps([
    {   # 夏令时期间：英国本地 20:00 = UTC 19:00
        "MatchNumber": 1, "RoundNumber": 1,
        "DateUtc": "2026-08-21 19:00:00Z",
        "Location": "Emirates Stadium",
        "HomeTeam": "Arsenal", "AwayTeam": "Coventry",
        "HomeTeamScore": 3, "AwayTeamScore": 0, "Winner": "Arsenal",
    },
    {   # 未来场次（无比分）
        "MatchNumber": 2, "RoundNumber": 2,
        "DateUtc": "2026-10-31 12:30:00Z",
        "Location": "Stamford Bridge",
        "HomeTeam": "Chelsea", "AwayTeam": "Man Utd",
        "HomeTeamScore": None, "AwayTeamScore": None, "Winner": None,
    },
    {   # 德比 + 争冠对话（用假积分榜喂）
        "MatchNumber": 3, "RoundNumber": 3,
        "DateUtc": "2026-11-07 15:00:00Z",
        "Location": "Etihad",
        "HomeTeam": "Man City", "AwayTeam": "Man Utd",
        "HomeTeamScore": None, "AwayTeamScore": None, "Winner": None,
    },
])

# football-data 的 fixtures.csv：英国本地时间，含裁判
FD_FIXTURES = """Div,Date,Time,HomeTeam,AwayTeam,Referee
E0,31/10/2026,12:30,Chelsea,Man United,P Bankes
E0,07/11/2026,15:00,Man City,Man United,C Kavanagh
E1,31/10/2026,15:00,Leicester,Stoke,S Hooper
"""

# 结果 CSV：英国本地时间 + 红黄牌。用来算积分榜。
FD_RESULTS = """Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,Referee,HY,AY,HR,AR
E0,21/08/2026,20:00,Arsenal,Coventry,3,0,S Attwell,1,2,0,0
E0,22/08/2026,15:00,Man City,Leeds,2,0,C Kavanagh,2,1,0,0
E0,29/08/2026,15:00,Man Utd,Arsenal,1,1,P Bankes,3,3,0,0
E0,05/09/2026,15:00,Leeds,Man Utd,0,2,A Taylor,4,2,1,0
E0,12/09/2026,15:00,Coventry,Chelsea,1,3,D England,2,2,0,0
E0,19/09/2026,15:00,Chelsea,Man City,0,1,J Brooks,5,1,0,1
"""


def fake_transport(routes):
    """routes: {url: (status, text)}；未登记的 URL 抛异常（模拟网络故障）。"""
    def _t(url, timeout=25):
        if url not in routes:
            raise ConnectionError(f"no route for {url}")
        return routes[url]
    return _t


def all_routes():
    return {
        mc.URL_FIXTUREDOWNLOAD: (200, FD_JSON),
        mc.URL_FD_FIXTURES: (200, FD_FIXTURES),
        mc.URL_FD_RESULTS.format(code="2627"): (200, FD_RESULTS),
    }


def st(home, away, hp, hpts, ap, apts, n=20):
    """手工构造积分榜，用于验证分级随榜单变化。"""
    return {
        mc.canon_team(home): {"team": mc.canon_team(home), "position": hp, "points": hpts},
        mc.canon_team(away): {"team": mc.canon_team(away), "position": ap, "points": apts},
    }, n


# =============================================================================
# A. 时区换算（本模块最容易出错的地方）
# =============================================================================
class TestUkTimezone(unittest.TestCase):
    def test_bst_summer_is_utc_plus_one(self):
        """2026-08-21 是 BST，本地 20:00 → UTC 19:00。"""
        local = datetime.datetime(2026, 8, 21, 20, 0)
        self.assertTrue(mc.is_bst(local))
        self.assertEqual(mc.iso_utc(mc.uk_local_to_utc(local)), "2026-08-21T19:00:00Z")

    def test_gmt_winter_is_utc(self):
        """2026-10-31 已过 BST 结束日，本地 12:30 → UTC 12:30。"""
        local = datetime.datetime(2026, 10, 31, 12, 30)
        self.assertFalse(mc.is_bst(local))
        self.assertEqual(mc.iso_utc(mc.uk_local_to_utc(local)), "2026-10-31T12:30:00Z")

    def test_bst_boundary_last_sunday_of_march(self):
        """2026 年 BST 起点是 3 月 29 日 01:00。"""
        self.assertEqual(mc._last_sunday(2026, 3), datetime.date(2026, 3, 29))
        self.assertFalse(mc.is_bst(datetime.datetime(2026, 3, 29, 0, 59)))
        self.assertTrue(mc.is_bst(datetime.datetime(2026, 3, 29, 1, 0)))

    def test_bst_boundary_last_sunday_of_october(self):
        """2026 年 BST 终点是 10 月 25 日 02:00。"""
        self.assertEqual(mc._last_sunday(2026, 10), datetime.date(2026, 10, 25))
        self.assertTrue(mc.is_bst(datetime.datetime(2026, 10, 25, 1, 59)))
        self.assertFalse(mc.is_bst(datetime.datetime(2026, 10, 25, 2, 0)))

    def test_beijing_is_utc_plus_eight(self):
        utc = datetime.datetime(2026, 10, 31, 12, 30)
        self.assertEqual(mc.to_beijing(utc).strftime("%Y-%m-%d %H:%M"),
                         "2026-10-31 20:30")

    def test_beijing_crosses_midnight(self):
        utc = datetime.datetime(2026, 10, 31, 20, 0)
        self.assertEqual(mc.to_beijing(utc).strftime("%Y-%m-%d %H:%M"),
                         "2026-11-01 04:00")

    def test_tz_aware_input_is_rejected(self):
        """传带时区的时间是调用方搞错了，必须当场炸掉而不是悄悄算错。"""
        aware = datetime.datetime(2026, 10, 31, 12, 0,
                                  tzinfo=datetime.timezone.utc)
        with self.assertRaises(ValueError):
            mc.uk_local_to_utc(aware)


# =============================================================================
# B. 队名归一化
# =============================================================================
class TestTeamNames(unittest.TestCase):
    def test_known_alias_divergence(self):
        """两个源的实测差异只有这两处，必须能 join 上。"""
        self.assertEqual(mc.canon_team("Man United"), mc.canon_team("Man Utd"))
        self.assertEqual(mc.canon_team("Tottenham"), mc.canon_team("Spurs"))

    def test_unknown_name_is_not_guessed(self):
        """没见过的队名不做模糊匹配——猜错会把档案挂到错误的对手上。"""
        self.assertEqual(mc.canon_team("Fictional FC"), "fictional fc")

    def test_display_name_is_consistent(self):
        self.assertEqual(mc.display_team("Spurs"), "Tottenham")
        self.assertEqual(mc.display_team("Tottenham"), "Tottenham")

    def test_official_api_names_all_map_to_source_names(self):
        """英超官方接口的 20 个队名必须全部能 join 到赛程源的写法。

        这不是锦上添花：`_fixture_for` 靠 canon 相等去把我们的 match 映射到
        官方 fixture_id。只要有一队对不上，那队的焦点战就**静默**拿不到裁判
        和首发——不报错、不告警，只是永远没有内容。

        实测踩过的坑：官方写 `Brighton & Hove Albion`，源里写 `Brighton`。
        """
        pairs = [
            ("Arsenal", "Arsenal"),
            ("Aston Villa", "Aston Villa"),
            ("Bournemouth", "Bournemouth"),
            ("Brentford", "Brentford"),
            ("Brighton & Hove Albion", "Brighton"),
            ("Chelsea", "Chelsea"),
            ("Coventry City", "Coventry"),
            ("Crystal Palace", "Crystal Palace"),
            ("Everton", "Everton"),
            ("Fulham", "Fulham"),
            ("Hull City", "Hull"),
            ("Ipswich Town", "Ipswich"),
            ("Leeds United", "Leeds"),
            ("Liverpool", "Liverpool"),
            ("Manchester City", "Man City"),
            ("Manchester United", "Man Utd"),
            ("Newcastle United", "Newcastle"),
            ("Nottingham Forest", "Nott'm Forest"),
            ("Sunderland", "Sunderland"),
            ("Tottenham Hotspur", "Spurs"),
        ]
        for official, source in pairs:
            self.assertEqual(mc.canon_team(official), mc.canon_team(source),
                             f"官方名 {official!r} 与源名 {source!r} 归不到一起")

    def test_ampersand_is_normalised(self):
        self.assertEqual(mc.canon_team("Brighton & Hove Albion"), "brighton")
        self.assertEqual(mc.canon_team("Brighton&Hove Albion"), "brighton")


# =============================================================================
# C. 积分榜自算
# =============================================================================
class TestStandings(unittest.TestCase):
    def test_points_and_positions(self):
        results = mc.parse_football_data_results(FD_RESULTS)
        table, order = mc.build_standings(results)
        # Arsenal: 3-0 胜 + 1-1 平 = 4 分；Man City: 2-0 胜 + 1-0 胜 = 6 分
        self.assertEqual(table["man city"]["points"], 6)
        self.assertEqual(table["arsenal"]["points"], 4)
        self.assertEqual(order[0], "man city")
        self.assertEqual(table["man city"]["position"], 1)

    def test_goal_difference_breaks_tie(self):
        results = mc.parse_football_data_results(FD_RESULTS)
        table, order = mc.build_standings(results)
        # Man Utd: 1-1 平 + 2-0 胜 = 4 分，净胜球 +2；Arsenal 4 分，净胜球 +3
        self.assertEqual(table["man utd"]["points"], 4)
        self.assertGreater(table["arsenal"]["gd"], table["man utd"]["gd"])
        self.assertLess(order.index("arsenal"), order.index("man utd"))

    def test_unplayed_matches_are_ignored(self):
        rows = [{"home_team": "A", "away_team": "B",
                 "home_score": None, "away_score": None}]
        table, order = mc.build_standings(rows)
        self.assertEqual(table, {})
        self.assertEqual(order, [])


# =============================================================================
# D. 焦点战分级（核心：不得写死对阵）
# =============================================================================
class TestFocusGrading(unittest.TestCase):
    def _match(self, home, away):
        return {"home_team": home, "away_team": away}

    def test_grade_is_not_hardcoded(self):
        """同一对球队，在不同积分榜下必须得到不同等级。

        这是任务书「示例对阵不得写死」的守门测试：如果哪天有人把
        「切尔西 vs 曼联 = B 级」写进代码，这条会立刻失败。
        """
        m = self._match("Chelsea", "Man Utd")

        # 场景一：两队都在争冠区，分差 3 分 → 争冠对话 + 强队 + 榜面
        s1, n1 = st("Chelsea", "Man Utd", 1, 25, 2, 22)
        lvl1, r1, sc1 = mc.grade_match(m, s1, n1)
        self.assertEqual(lvl1, "S", f"应判 S，实得 {lvl1}，理由 {r1}")

        # 场景二：曼联排第 16 → 只剩「强队对决」+ 榜面，掉到 B
        s2, n2 = st("Chelsea", "Man Utd", 5, 13, 16, 6)
        lvl2, r2, sc2 = mc.grade_match(m, s2, n2)
        self.assertEqual(lvl2, "B", f"应判 B，实得 {lvl2}，理由 {r2}")

        self.assertNotEqual(lvl1, lvl2, "分级必须随积分榜变化，不能是常量")
        self.assertGreater(sc1, sc2)

    def test_derby_is_detected_from_config(self):
        m = self._match("Man City", "Man Utd")
        s, n = st("Man City", "Man Utd", 5, 13, 16, 6)
        lvl, reasons, _ = mc.grade_match(m, s, n)
        self.assertIn("derby", [r["rule"] for r in reasons])

    def test_non_derby_does_not_trigger_derby(self):
        m = self._match("Chelsea", "Brentford")
        s, n = st("Chelsea", "Brentford", 5, 13, 10, 9)
        _, reasons, _ = mc.grade_match(m, s, n)
        self.assertNotIn("derby", [r["rule"] for r in reasons])

    def test_relegation_battle(self):
        m = self._match("Coventry", "Sunderland")
        s, n = st("Coventry", "Sunderland", 19, 5, 20, 4)
        _, reasons, _ = mc.grade_match(m, s, n)
        self.assertIn("relegation", [r["rule"] for r in reasons])

    def test_public_attention_requires_real_mentions(self):
        """拿不到真实提及次数时这条规则必须不命中，而不是猜一个值。"""
        m = self._match("Chelsea", "Brentford")
        s, n = st("Chelsea", "Brentford", 12, 8, 11, 9)
        _, r0, _ = mc.grade_match(m, s, n, mentions=0)
        self.assertNotIn("public_attention", [r["rule"] for r in r0])
        _, r3, _ = mc.grade_match(m, s, n, mentions=3)
        self.assertIn("public_attention", [r["rule"] for r in r3])

    def test_missing_standings_does_not_fabricate_rank(self):
        """积分榜里没有这两队时，排名类规则一律不命中。"""
        m = self._match("Chelsea", "Man Utd")
        _, reasons, score = mc.grade_match(m, {}, 20)
        rules = [r["rule"] for r in reasons]
        for r in ("title_race", "top4_battle", "relegation", "table_impact"):
            self.assertNotIn(r, rules)
        self.assertEqual(score, sum(cfg.FOCUS_RULES[k]["weight"] for k in rules))

    def test_reasons_are_structured(self):
        m = self._match("Man City", "Man Utd")
        s, n = st("Man City", "Man Utd", 1, 25, 2, 22)
        _, reasons, _ = mc.grade_match(m, s, n)
        for r in reasons:
            self.assertEqual(set(r), {"rule", "label", "weight", "detail"})
            self.assertIn(r["rule"], cfg.FOCUS_RULES)

    def test_all_rules_can_be_disabled(self):
        """规则必须真能关掉（配置化不是摆设）。"""
        saved = {k: dict(v) for k, v in cfg.FOCUS_RULES.items()}
        try:
            for v in cfg.FOCUS_RULES.values():
                v["enabled"] = False
            m = self._match("Man City", "Man Utd")
            s, n = st("Man City", "Man Utd", 1, 25, 2, 22)
            lvl, reasons, score = mc.grade_match(m, s, n)
            self.assertEqual(reasons, [])
            self.assertEqual(score, 0)
            self.assertEqual(lvl, cfg.NORMAL_GRADE)
        finally:
            cfg.FOCUS_RULES.clear()
            cfg.FOCUS_RULES.update(saved)


# =============================================================================
# E、合并与数据质量
# =============================================================================
class TestMerge(unittest.TestCase):
    def test_match_id_stable_across_sources(self):
        kick = datetime.datetime(2026, 10, 31, 12, 30)
        a = mc.make_match_id("Chelsea", "Man United", kick)
        b = mc.make_match_id("Chelsea", "Man Utd", kick)
        self.assertEqual(a, b, "同一场比赛换一个源的队名写法必须得到同一个 id")
        self.assertEqual(a, "epl-20261031-chelsea-manutd")

    def test_kickoff_mismatch_is_flagged(self):
        """两源开球时间差超过阈值必须报出来，不能静默取一个。"""
        base = mc.parse_fixturedownload(FD_JSON)
        bad = mc.parse_football_data_fixtures(
            "Div,Date,Time,HomeTeam,AwayTeam,Referee\n"
            "E0,31/10/2026,14:30,Chelsea,Man United,P Bankes\n")
        _, warns = mc.merge_sources(base, bad)
        codes = [w["code"] for w in warns]
        self.assertIn("KICKOFF_MISMATCH", codes)

    def test_enrich_matched_by_team_and_date(self):
        base = mc.parse_fixturedownload(FD_JSON)
        enrich = mc.parse_football_data_fixtures(FD_FIXTURES)
        merged, warns = mc.merge_sources(base, enrich)
        chelsea = [m for m in merged
                   if mc.canon_team(m["home_team"]) == "chelsea"
                   and mc.canon_team(m["away_team"]) == "man utd"][0]
        self.assertEqual(chelsea["referee_raw"], "P Bankes")
        self.assertIn(mc.SOURCE_FD_FIXTURES, chelsea["source_ids"])
        self.assertEqual([w for w in warns if w["code"] == "KICKOFF_MISMATCH"], [])

    def test_non_epl_divisions_are_dropped(self):
        rows = mc.parse_football_data_fixtures(FD_FIXTURES)
        self.assertEqual([r for r in rows if "Leicester" in r["home_team"]], [],
                         "英冠比赛不得混进英超焦点战")


# =============================================================================
# F、档案字段与校验
# =============================================================================
class TestMatchRecord(unittest.TestCase):
    def test_all_required_fields_present(self):
        cal = mc.build_calendar(
            now=datetime.datetime(2026, 10, 30, 12, 0),
            transport=fake_transport(all_routes()))
        self.assertTrue(cal["matches"], "假数据下也应产出比赛")
        for m in cal["matches"]:
            self.assertEqual(mc.validate_match(m), [],
                             f"{m['match_id']} 档案不完整")

    def test_beijing_mismatch_is_caught(self):
        cal = mc.build_calendar(
            now=datetime.datetime(2026, 10, 30, 12, 0),
            transport=fake_transport(all_routes()))
        m = dict(cal["matches"][0])
        m["kickoff_time_beijing"] = "2026-01-01 00:00"
        self.assertTrue(mc.validate_match(m), "北京时间与 UTC 不自洽时必须报错")

    def test_monitor_policy_follows_grade(self):
        for grade, expect in (("S", (True, True)), ("A", (True, True)),
                              ("B", (True, False)), (cfg.NORMAL_GRADE, (False, False))):
            m = mc.apply_monitor_policy({"focus_level": grade})
            self.assertEqual(
                (m["referee_monitor_enabled"], m["lineup_monitor_enabled"]), expect,
                f"{grade} 级的监控开关不符合策略")

    def test_match_status_transitions(self):
        kick = datetime.datetime(2026, 10, 31, 12, 30)
        self.assertEqual(mc.match_status_of(kick, False, kick - datetime.timedelta(hours=1)),
                         "upcoming")
        self.assertEqual(mc.match_status_of(kick, False, kick + datetime.timedelta(minutes=30)),
                         "live")
        self.assertEqual(mc.match_status_of(kick, False, kick + datetime.timedelta(hours=4)),
                         "awaiting_result")
        self.assertEqual(mc.match_status_of(kick, True, kick + datetime.timedelta(hours=4)),
                         "finished")


# =============================================================================
# G、端到端（离线）
# =============================================================================
class TestBuildCalendar(unittest.TestCase):
    NOW = datetime.datetime(2026, 10, 30, 12, 0)

    def test_end_to_end_offline(self):
        cal = mc.build_calendar(now=self.NOW, transport=fake_transport(all_routes()))
        self.assertEqual(cal["competition"], "Premier League")
        self.assertEqual(cal["season"], "2026/27")
        self.assertIn("man city", cal["standings"]["order"])
        ids = [m["match_id"] for m in cal["matches"]]
        self.assertEqual(len(ids), len(set(ids)), "match_id 不得重复")
        # 已完赛的 8-21 那场超出 21 天视界，不该出现在待监控日历里
        self.assertNotIn("epl-20260821-arsenal-coventry", ids)

    def test_focus_matches_are_subset(self):
        cal = mc.build_calendar(now=self.NOW, transport=fake_transport(all_routes()))
        all_ids = {m["match_id"] for m in cal["matches"]}
        for m in cal["focus_matches"]:
            self.assertIn(m["match_id"], all_ids)
            self.assertNotEqual(m["focus_level"], cfg.NORMAL_GRADE)

    def test_base_source_down_degrades_with_warning(self):
        """基准源挂了要明确降级，而不是假装日历完整。"""
        routes = all_routes()
        del routes[mc.URL_FIXTUREDOWNLOAD]
        cal = mc.build_calendar(now=self.NOW, transport=fake_transport(routes))
        codes = [w["code"] for w in cal["data_quality"]]
        self.assertIn("BASE_SOURCE_DOWN", codes)
        self.assertTrue(cal["matches"], "降级后仍应能从 fixtures.csv 拿到短期赛程")

    def test_all_sources_down_returns_empty_without_crash(self):
        cal = mc.build_calendar(now=self.NOW, transport=fake_transport({}))
        self.assertEqual(cal["matches"], [])
        self.assertEqual(cal["focus_matches"], [])
        self.assertTrue(all(not s["ok"] for s in cal["source_status"]))

    def test_no_fabricated_matches_when_results_missing(self):
        """只有结果、没有赛程时不得凭空造比赛。"""
        routes = all_routes()
        del routes[mc.URL_FIXTUREDOWNLOAD]
        del routes[mc.URL_FD_FIXTURES]
        cal = mc.build_calendar(now=self.NOW, transport=fake_transport(routes))
        self.assertEqual(cal["matches"], [])
        self.assertTrue(cal["standings"]["order"], "结果还在，积分榜仍应算出来")

    def test_horizon_filter(self):
        cal = mc.build_calendar(now=self.NOW,
                                transport=fake_transport(all_routes()),
                                days_ahead=1)
        for m in cal["matches"]:
            kick = datetime.datetime.strptime(m["kickoff_time_utc"], "%Y-%m-%dT%H:%M:%SZ")
            self.assertLessEqual(kick, self.NOW + datetime.timedelta(days=1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
