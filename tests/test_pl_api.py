"""英超官方接口（pl_api）—— 离线测试。

夹具取自 2026-10-31 的真实响应（fixture 128923 Arsenal vs Coventry），
字段结构原样保留，只裁掉用不到的部分。
"""

import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lineup_monitor as lm         # noqa: E402
import pl_api                       # noqa: E402

NOW = datetime.datetime(2026, 10, 31, 12, 50)

# 真实时间戳：1787338800000 ms = 2026-08-21T19:00:00Z
DETAIL = {
    "id": 128923.0,
    "status": "C",
    "kickoff": {"millis": 1787338800000, "label": "Fri 21 Aug 2026, 20:00 BST",
                "gmtOffset": 1.0},
    "teams": [
        {"team": {"id": 1, "name": "Arsenal"}, "score": 3},
        {"team": {"id": 5, "name": "Coventry City"}, "score": 0},
    ],
    "matchOfficials": [
        {"id": 155033, "role": "MAIN", "name": {"display": "Thomas Bramall"}},
        {"id": 16981, "name": {"display": "Gary Beswick"}},
        {"id": 16976, "name": {"display": "Matt Wilkes"}},
        {"id": 28094, "role": "FOURTH_OFFICIAL", "name": {"display": "Adam Herczeg"}},
        {"id": 23530, "role": "VAR", "name": {"display": "Jarred Gillett"}},
        {"id": 23531, "role": "ASSISTANT_VAR", "name": {"display": "Neil Davies"}},
    ],
    "teamLists": [
        {"teamId": 1, "formation": {"label": "4-2-3-1",
                                    "players": [[7975], [14548, 119476, 50234, 69914],
                                                [114564, 15202], [49481, 11419, 67704],
                                                [20810]]},
         "lineup": [{"name": {"display": n}} for n in
                    ("David Raya", "Jurrien Timber", "Cristhian Mosquera",
                     "Gabriel", "Riccardo Calafiori", "Declan Rice",
                     "Martin Zubimendi", "Bukayo Saka", "Martin Odegaard",
                     "Eberechi Eze", "Viktor Gyokeres")],
         "substitutes": [{"name": {"display": "Kepa"}}]},
        {"teamId": 5, "formation": {"label": "4-1-4-1",
                                    "players": [[55556], [75413, 63836, 114738, 10438],
                                                [8650], [74964, 337310, 108612, 19694],
                                                [19925]]},
         "lineup": [{"name": {"display": n}} for n in
                    ("Oliver Dovin", "Milan van Ewijk", "Bobby Thomas",
                     "Liam Kitching", "Jay Dasilva", "Matt Grimes",
                     "Jack Rudoni", "Josh Eccles", "Ephron Mason-Clark",
                     "Tatsuhiro Sakamoto", "Ellis Simms")],
         "substitutes": []},
    ],
}

LIST = {"pageInfo": {"numEntries": 380}, "content": [
    {"id": 128923.0, "status": "C",
     "kickoff": {"millis": 1787338800000},
     "teams": [{"team": {"id": 1, "name": "Arsenal"}},
               {"team": {"id": 5, "name": "Coventry City"}}],
     "gameweek": {"gameweek": 1}, "ground": {"name": "Emirates Stadium"}},
    {"id": 129004.0, "status": "U",
     "kickoff": {"millis": 1793458800000},     # 2026-10-31T15:00:00Z
     "teams": [{"team": {"id": 3, "name": "Bournemouth"}},
               {"team": {"id": 11, "name": "Leeds United"}}],
     "gameweek": {"gameweek": 10}, "ground": {"name": "Vitality Stadium"}},
]}

EMPTY_DETAIL = {
    "id": 129004.0, "status": "U",
    "kickoff": {"millis": 1793458800000},
    "teams": [{"team": {"id": 3, "name": "Bournemouth"}},
              {"team": {"id": 11, "name": "Leeds United"}}],
    "matchOfficials": [],
    "teamLists": [
        {"teamId": 3, "formation": None, "lineup": [], "substitutes": []},
        {"teamId": 11, "formation": None, "lineup": [], "substitutes": []},
    ],
}


# =============================================================================
# A. 赛程列表
# =============================================================================
class TestFixtureList(unittest.TestCase):
    def test_parse_rows(self):
        rows = pl_api.parse_fixture_list(LIST)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["home_team"], "Arsenal")
        self.assertEqual(rows[0]["fixture_id"], 128923)
        self.assertEqual(rows[0]["status"], "C")

    def test_millis_to_utc_is_exact(self):
        rows = pl_api.parse_fixture_list(LIST)
        self.assertEqual(rows[0]["kickoff_utc"],
                         datetime.datetime(2026, 8, 21, 19, 0))
        self.assertEqual(rows[1]["kickoff_utc"],
                         datetime.datetime(2026, 10, 31, 15, 0))

    def test_find_fixture_matches_teams_and_time(self):
        rows = pl_api.parse_fixture_list(LIST)
        hit = pl_api.find_fixture(
            rows, "Arsenal", "Coventry City",
            datetime.datetime(2026, 8, 21, 19, 0),
            canon=lambda s: s.lower())
        self.assertEqual(hit["fixture_id"], 128923)

    def test_find_fixture_rejects_wrong_time(self):
        rows = pl_api.parse_fixture_list(LIST)
        hit = pl_api.find_fixture(
            rows, "Arsenal", "Coventry City",
            datetime.datetime(2026, 8, 21, 22, 0),      # 差 3 小时
            canon=lambda s: s.lower())
        self.assertIsNone(hit, "时间差太多就不能硬认成同一场")


# =============================================================================
# B. 裁判组
# =============================================================================
class TestOfficials(unittest.TestCase):
    def test_all_five_roles_mapped(self):
        offs = pl_api.parse_officials(DETAIL)
        self.assertEqual(len(offs), 6)
        roles = [o["role"] for o in offs]
        self.assertEqual(roles[0], "Referee")
        self.assertEqual(roles[1], "Assistant Referee")
        self.assertEqual(roles[2], "Assistant Referee")
        self.assertEqual(roles[3], "Fourth Official")
        self.assertEqual(roles[4], "VAR")
        self.assertEqual(roles[5], "AVAR")

    def test_assistants_without_role_are_classified(self):
        offs = pl_api.parse_officials(DETAIL)
        assistants = [o["referee_name"] for o in offs
                      if o["role"] == "Assistant Referee"]
        self.assertEqual(assistants, ["Gary Beswick", "Matt Wilkes"])

    def test_upcoming_fixture_has_no_officials(self):
        self.assertEqual(pl_api.parse_officials(EMPTY_DETAIL), [],
                         "未开赛时接口返回空，不得编造")

    def test_appointment_records_cover_all_roles(self):
        match = {"match_id": "m1", "competition": "Premier League",
                 "kickoff_time_utc": "2026-08-21T19:00:00Z"}
        recs = pl_api.appointments_for_match(
            match, pl_api.parse_fixture_detail(DETAIL), NOW)
        self.assertEqual(len(recs), 6)
        self.assertEqual({r["role"] for r in recs},
                         {"Referee", "Assistant Referee", "Fourth Official",
                          "VAR", "AVAR"})
        self.assertTrue(all(r["appointment_status"] == "confirmed" for r in recs))

    def test_appointment_records_empty_when_not_published(self):
        match = {"match_id": "m1", "competition": "Premier League",
                 "kickoff_time_utc": "2026-10-31T15:00:00Z"}
        recs = pl_api.appointments_for_match(
            match, pl_api.parse_fixture_detail(EMPTY_DETAIL), NOW)
        self.assertEqual(recs, [])

    def test_watchlist_referee_is_recognized(self):
        """Thomas Bramall 是七人名单里的，必须能被认出来。"""
        import referee_intel as ri
        offs = pl_api.parse_officials(DETAIL)
        self.assertEqual(offs[0]["referee_name"], "Thomas Bramall")
        self.assertTrue(ri.is_watch_referee(offs[0]["referee_name"]))


# =============================================================================
# C. 首发名单
# =============================================================================
class TestTeamLists(unittest.TestCase):
    def test_eleven_players_each(self):
        lists = pl_api.parse_team_lists(DETAIL)
        self.assertEqual(len(lists), 2)
        for tl in lists:
            self.assertEqual(len(tl["players"]), 11)
            self.assertTrue(tl["published"])

    def test_formation_and_groups(self):
        lists = pl_api.parse_team_lists(DETAIL)
        self.assertEqual(lists[0]["formation"], "4-2-3-1")
        self.assertEqual(lists[0]["groups"], [1, 4, 2, 3, 1])

    def test_unpublished_list_is_flagged(self):
        lists = pl_api.parse_team_lists(EMPTY_DETAIL)
        for tl in lists:
            self.assertEqual(tl["players"], [])
            self.assertFalse(tl["published"], "空名单不能当成 0 人首发")

    def test_lineup_record_for_team(self):
        detail = pl_api.parse_fixture_detail(DETAIL)
        rec = pl_api.lineup_for_team(detail, "Arsenal", NOW)
        self.assertIsNotNone(rec)
        self.assertEqual(len(rec["players"]), 11)
        self.assertEqual(rec["formation"], "4-2-3-1")
        self.assertEqual(rec["source_tier"], 1)
        self.assertEqual(rec["status"], "official_confirmed")

    def test_lineup_record_none_when_not_published(self):
        detail = pl_api.parse_fixture_detail(EMPTY_DETAIL)
        self.assertIsNone(pl_api.lineup_for_team(detail, "Bournemouth", NOW))

    def test_lineup_record_none_for_unknown_team(self):
        detail = pl_api.parse_fixture_detail(DETAIL)
        self.assertIsNone(pl_api.lineup_for_team(detail, "Chelsea", NOW))

    def test_groups_reconstruct_from_formation(self):
        detail = pl_api.parse_fixture_detail(DETAIL)
        rec = pl_api.lineup_for_team(detail, "Arsenal", NOW)
        self.assertEqual([len(g) for g in rec["groups"]], [1, 4, 2, 3, 1])
        flat = [p for g in rec["groups"] for p in g]
        self.assertEqual(flat, rec["players"], "分组还原不得丢失或重排球员")


# =============================================================================
# D. 与九项校验的衔接（真实数据结构能过校验）
# =============================================================================
class TestIntegrationWithValidations(unittest.TestCase):
    def test_official_lineup_passes_nine_validations(self):
        detail = pl_api.parse_fixture_detail(DETAIL)
        rec = pl_api.lineup_for_team(detail, "Arsenal", NOW)
        probs = lm.validate_lineup({**rec, "match_id": "m1"})
        blocking = [p for p in probs if p["level"] == "block"]
        self.assertEqual(blocking, [], f"真实官方名单不该有阻断问题：{blocking}")
        # 只剩 V5（无阵容名单）这一条提示
        self.assertEqual([p["code"] for p in probs], ["V5"])

    def test_two_teams_do_not_overlap(self):
        detail = pl_api.parse_fixture_detail(DETAIL)
        home = pl_api.lineup_for_team(detail, "Arsenal", NOW)
        away = pl_api.lineup_for_team(detail, "Coventry City", NOW)
        probs = lm.validate_lineup({**home, "match_id": "m1"}, opponent=away)
        self.assertNotIn("V7", [p["code"] for p in probs])


if __name__ == "__main__":
    unittest.main(verbosity=2)
