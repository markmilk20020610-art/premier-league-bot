"""§5 首发阵容监控 —— 测试。

守四条纪律：
1. 只有官方来源 + 解析通过 + 无阻断问题，才允许标「官方确认」
2. 解析失败不得自动发名单，只能发链接 + 待核验
3. 没有三个真实时间戳，不得宣称速度优势
4. 高频轮询必须能退避
"""

import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lineup_monitor as lm         # noqa: E402

NOW = datetime.datetime(2026, 10, 31, 10, 30)          # 距 12:30 开球 T-120
KICK = "2026-10-31T12:30:00Z"
MID = "epl-20261031-chelsea-manutd"

XI = ("Arsenal team news\n"
      "Starting XI: Raya; Timber, Saliba, Gabriel, Calafiori; "
      "Rice, Merino, Odegaard; Saka, Havertz, Martinelli\n"
      "Substitutes: Neto, White, Kiwior")
XI_CN = ("首发阵容：拉亚；廷伯、萨利巴、加布里埃尔、卡拉菲奥里；"
         "赖斯、梅里诺、厄德高；萨卡、哈弗茨、马丁内利")


def rec(**kw):
    base = {
        "match_id": MID, "team": "Arsenal",
        "players": lm.parse_starting_xi(XI)["players"],
        "formation": "4-3-3",
        "groups": lm.parse_starting_xi(XI)["groups"],
        "source_tier": 1, "source_id": "arsenal_official",
        "source_url": "https://www.arsenal.com/news/lineup",
        "status": lm.ST_CONFIRMED,
    }
    base.update(kw)
    return base


def codes(problems):
    return [p["code"] for p in problems]


# =============================================================================
# A. 时间窗口与退避
# =============================================================================
class TestPhaseAndInterval(unittest.TestCase):
    def test_phase_boundaries(self):
        kick = datetime.datetime(2026, 10, 31, 12, 30)
        cases = [
            (kick - datetime.timedelta(minutes=200), lm.PHASE_IDLE),
            (kick - datetime.timedelta(minutes=120), lm.PHASE_WARMUP),
            (kick - datetime.timedelta(minutes=100), lm.PHASE_WARMUP),
            (kick - datetime.timedelta(minutes=90), lm.PHASE_INTENSIFY),
            (kick - datetime.timedelta(minutes=75), lm.PHASE_CRITICAL),
            (kick - datetime.timedelta(minutes=1), lm.PHASE_CRITICAL),
            (kick + datetime.timedelta(minutes=5), lm.PHASE_POST),
            (kick + datetime.timedelta(minutes=90), lm.PHASE_IDLE),
        ]
        for now, expect in cases:
            self.assertEqual(lm.phase_of(KICK, now), expect,
                             f"{now} 应为 {expect}")

    def test_intervals_get_faster_near_kickoff(self):
        kick = datetime.datetime(2026, 10, 31, 12, 30)
        idle, _, _ = lm.next_interval(KICK, kick - datetime.timedelta(minutes=200))
        warm, _, _ = lm.next_interval(KICK, kick - datetime.timedelta(minutes=110))
        inten, _, _ = lm.next_interval(KICK, kick - datetime.timedelta(minutes=85))
        crit, _, _ = lm.next_interval(KICK, kick - datetime.timedelta(minutes=60))
        self.assertGreater(idle, warm)
        self.assertGreater(warm, inten)
        self.assertGreater(inten, crit)
        self.assertEqual(crit, 30, "T-75 后应为 30 秒级")

    def test_backoff_grows_and_caps(self):
        prev = 0
        for failures in range(1, 7):
            sec, _, _ = lm.next_interval(KICK, NOW, consecutive_failures=failures)
            self.assertGreaterEqual(sec, prev, "退避间隔不应变小")
            self.assertLessEqual(sec, 300, "退避不得超过配置上限")
            prev = sec
        self.assertEqual(prev, 300, "连续失败足够多次后应顶到上限")

    def test_backoff_reason_is_explained(self):
        _, _, reason = lm.next_interval(KICK, NOW, consecutive_failures=2)
        self.assertIn("退避", reason)

    def test_monitoring_window(self):
        kick = datetime.datetime(2026, 10, 31, 12, 30)
        self.assertFalse(lm.is_monitoring_window(KICK, kick - datetime.timedelta(hours=5)))
        self.assertTrue(lm.is_monitoring_window(KICK, kick - datetime.timedelta(minutes=100)))


# =============================================================================
# B. 解析
# =============================================================================
class TestParsing(unittest.TestCase):
    def test_standard_format(self):
        p = lm.parse_starting_xi(XI)
        self.assertTrue(p["ok"])
        self.assertEqual(len(p["players"]), 11)
        self.assertEqual(p["formation"], "4-3-3")
        self.assertEqual(p["players"][0], "Raya")
        self.assertNotIn("Starting XI: Raya", p["players"])

    def test_substitutes_are_excluded(self):
        p = lm.parse_starting_xi(XI)
        self.assertNotIn("Neto", p["players"])

    def test_chinese_punctuation(self):
        p = lm.parse_starting_xi(XI_CN)
        self.assertTrue(p["ok"])
        self.assertEqual(len(p["players"]), 11)
        self.assertEqual(p["formation"], "4-3-3")

    def test_ten_players_is_parsed_but_flagged(self):
        p = lm.parse_starting_xi(
            "Starting XI: Raya; Timber, Saliba, Gabriel, Calafiori; "
            "Rice, Merino, Odegaard; Saka, Havertz")
        self.assertTrue(p["ok"])
        self.assertEqual(len(p["players"]), 10)
        probs = lm.validate_lineup(rec(players=p["players"],
                                       formation=p["formation"],
                                       groups=p["groups"]))
        self.assertIn("V1", codes(probs))

    def test_no_header_fails_loudly(self):
        p = lm.parse_starting_xi("Raya, Timber, Saliba")
        self.assertFalse(p["ok"])
        self.assertEqual(p["players"], [], "解析失败不得返回半份名单")

    def test_empty_text(self):
        self.assertFalse(lm.parse_starting_xi("")["ok"])

    def test_hash_is_order_insensitive(self):
        a = lm.players_hash(["A", "B", "C"])
        b = lm.players_hash(["C", "A", "B"])
        self.assertEqual(a, b, "同一批球员换顺序应视为同一版")
        self.assertNotEqual(a, lm.players_hash(["A", "B", "D"]))


# =============================================================================
# C. 九项校验
# =============================================================================
class TestNineValidations(unittest.TestCase):
    def test_clean_record_passes(self):
        probs = lm.validate_lineup(rec())
        self.assertEqual([p for p in probs if p["level"] == "block"], [])

    def test_nine_validation_definitions(self):
        self.assertEqual(len(lm.NINE_VALIDATIONS), 9)

    def test_v1_count(self):
        probs = lm.validate_lineup(rec(players=["A"] * 10))
        self.assertIn("V1", codes(probs))

    def test_v2_formation_sum(self):
        probs = lm.validate_lineup(rec(formation="4-3-2"))
        self.assertIn("V2", codes(probs))

    def test_v3_bad_names(self):
        players = list(lm.parse_starting_xi(XI)["players"])
        players[3] = "GK"
        probs = lm.validate_lineup(rec(players=players))
        self.assertIn("V3", codes(probs))

    def test_v4_duplicates(self):
        players = list(lm.parse_starting_xi(XI)["players"])
        players[1] = players[0]
        probs = lm.validate_lineup(rec(players=players))
        self.assertIn("V4", codes(probs))

    def test_v5_squad_check(self):
        squad = lm.parse_starting_xi(XI)["players"]
        probs = lm.validate_lineup(rec(), squad=squad)
        self.assertNotIn("V5", codes(probs))

        probs = lm.validate_lineup(rec(), squad=["Someone", "Else"])
        self.assertIn("V5", codes(probs))

    def test_v5_missing_squad_is_warning_not_block(self):
        probs = lm.validate_lineup(rec())
        v5 = [p for p in probs if p["code"] == "V5"]
        self.assertEqual(len(v5), 1)
        self.assertEqual(v5[0]["level"], "warn", "没名单只能提示，不能阻断")

    def test_v7_overlap_with_opponent(self):
        opp = {"players": ["Raya", "Other"]}
        probs = lm.validate_lineup(rec(), opponent=opp)
        self.assertIn("V7", codes(probs))

    def test_v8_source_tier_blocks_fake_confirmation(self):
        probs = lm.validate_lineup(rec(source_tier=3))
        self.assertIn("V8", codes(probs))

    def test_v8_requires_source_url(self):
        probs = lm.validate_lineup(rec(source_url=""))
        self.assertIn("V8", codes(probs))

    def test_v9_conflict_with_confirmed_version(self):
        prev = {"players": ["Someone"] * 11}
        probs = lm.validate_lineup(rec(), previous_confirmed=prev)
        self.assertIn("V9", codes(probs))

    def test_v9_same_version_no_conflict(self):
        prev = {"players": lm.parse_starting_xi(XI)["players"]}
        probs = lm.validate_lineup(rec(), previous_confirmed=prev)
        self.assertNotIn("V9", codes(probs))


# =============================================================================
# D. 状态判定与推送动作
# =============================================================================
class TestStatusDecision(unittest.TestCase):
    def test_only_official_tier_gets_confirmed(self):
        self.assertEqual(lm.decide_status(1, True, []), lm.ST_CONFIRMED)
        self.assertEqual(lm.decide_status(2, True, []), lm.ST_REPORTED)
        self.assertEqual(lm.decide_status(3, True, []), lm.ST_REPORTED)
        self.assertEqual(lm.decide_status(4, True, []), lm.ST_UNCONFIRMED)

    def test_blocking_problem_prevents_confirmation(self):
        probs = [{"code": "V1", "level": "block", "detail": "x"}]
        self.assertNotEqual(lm.decide_status(1, True, probs), lm.ST_CONFIRMED)

    def test_parse_failure_is_unconfirmed(self):
        self.assertEqual(lm.decide_status(1, False, []), lm.ST_UNCONFIRMED)

    def test_action_link_only_when_parse_fails(self):
        self.assertEqual(
            lm.decide_action(lm.ST_UNCONFIRMED, [], False, "https://x"),
            lm.ACT_PUBLISH_LINK_ONLY)
        self.assertEqual(lm.decide_action(lm.ST_UNCONFIRMED, [], False, ""),
                         lm.ACT_SKIP)

    def test_action_link_only_when_blocked(self):
        probs = [{"code": "V4", "level": "block", "detail": "dup"}]
        self.assertEqual(
            lm.decide_action(lm.ST_CONFIRMED, probs, True, "https://x"),
            lm.ACT_PUBLISH_LINK_ONLY)

    def test_reported_is_publishable(self):
        self.assertEqual(lm.decide_action(lm.ST_REPORTED, [], True, "https://x"),
                         lm.ACT_PUBLISH_REPORTED)


# =============================================================================
# E. 七步流程
# =============================================================================
class TestPushFlow(unittest.TestCase):
    def _flow(self, **kw):
        args = dict(
            match={"match_id": MID}, team="Arsenal", text=XI,
            source_tier=1, source_id="arsenal_official",
            source_url="https://www.arsenal.com/news/lineup", now=NOW)
        args.update(kw)
        return lm.run_push_flow(**args)

    def test_seven_steps_in_order(self):
        out = self._flow()
        self.assertEqual(len(out["steps"]), 7)
        self.assertEqual([s["step"] for s in out["steps"]],
                         [k for k, _ in lm.FLOW_STEPS])

    def test_official_source_publishes_confirmed(self):
        out = self._flow()
        self.assertEqual(out["status"], lm.ST_CONFIRMED)
        self.assertEqual(out["action"], lm.ACT_PUBLISH_CONFIRMED)

    def test_media_source_cannot_claim_official(self):
        out = self._flow(source_tier=3, source_id="sky_sports")
        self.assertEqual(out["status"], lm.ST_REPORTED)
        self.assertNotIn("官方已确认", out["message"])

    def test_parse_failure_sends_link_only(self):
        out = self._flow(text="nothing useful here")
        self.assertEqual(out["action"], lm.ACT_PUBLISH_LINK_ONLY)
        self.assertIn("暂不发布名单", out["message"])
        self.assertIn("待核验", out["message"])

    def test_dedupe_skips_same_version(self):
        first = self._flow()
        second = self._flow(already_pushed_hash=first["record"]["raw_hash"])
        self.assertEqual(second["action"], lm.ACT_SKIP)

    def test_all_three_timestamps_recorded(self):
        out = self._flow()
        for f in lm.SPEED_RECORD_FIELDS:
            self.assertIn(f, out["speed"])
        self.assertEqual(out["speed"]["first_detected_at"], "2026-10-31T10:30:00Z")
        self.assertEqual(out["speed"]["pushed_at"], "2026-10-31T10:30:00Z")


# =============================================================================
# F. 速度证明
# =============================================================================
class TestSpeedProof(unittest.TestCase):
    def _speed(self, **kw):
        base = {"match_id": MID, "team": "Arsenal",
                "official_published_at": "", "first_detected_at": "2026-10-31T10:30:00Z",
                "pushed_at": "2026-10-31T10:30:05Z"}
        base.update(kw)
        return base

    def test_no_competitor_time_means_not_proven(self):
        proof = lm.speed_proof(self._speed())
        self.assertFalse(proof["proven"])
        self.assertIn("不宣称领先", proof["note"])

    def test_missing_timestamp_field_is_caught(self):
        speed = self._speed()
        del speed["pushed_at"]
        proof = lm.speed_proof(speed)
        self.assertFalse(proof["proven"])

    def test_proven_when_competitor_is_later(self):
        proof = lm.speed_proof(self._speed(), competitors_seen_at="2026-10-31T10:33:00Z")
        self.assertTrue(proof["proven"])
        self.assertEqual(proof["lead_seconds"], 180)

    def test_not_proven_when_competitor_is_earlier(self):
        proof = lm.speed_proof(self._speed(), competitors_seen_at="2026-10-31T10:20:00Z")
        self.assertFalse(proof["proven"])
        self.assertIn("不得宣称速度优势", proof["note"])

    def test_message_omits_speed_when_unproven(self):
        out = lm.run_push_flow(
            match={"match_id": MID}, team="Arsenal", text=XI, source_tier=1,
            source_id="arsenal_official", source_url="https://x", now=NOW)
        self.assertNotIn("⏱", out["message"])

    def test_message_shows_speed_when_proven(self):
        out = lm.run_push_flow(
            match={"match_id": MID}, team="Arsenal", text=XI, source_tier=1,
            source_id="arsenal_official", source_url="https://x", now=NOW,
            competitors_seen_at="2026-10-31T10:35:00Z")
        self.assertIn("⏱", out["message"])
        self.assertIn("领先", out["message"])

    def test_validate_speed_record(self):
        self.assertEqual(lm.validate_speed_record(self._speed()), [])
        bad = self._speed(status=lm.ST_CONFIRMED, official_published_at="")
        self.assertTrue(lm.validate_speed_record(bad))


if __name__ == "__main__":
    unittest.main(verbosity=2)
