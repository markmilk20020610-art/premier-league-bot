"""§4 裁判情报系统 —— 测试。

重点守三条纪律：
1. 数据拿不到时必须说「没有」，不许编（published_at / VAR / 争议记录）
2. 样本不足时不得出结论
3. 球迷评论（D 级）不得进入事实陈述
"""

import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import focus_config as cfg          # noqa: E402
import referee_intel as ri          # noqa: E402

NOW = datetime.datetime(2026, 10, 31, 12, 50)


def res(home, away, ref, yh=2, ya=2, rh=0, ra=0, day=1, season="2026/27"):
    return {
        "home_team": home, "away_team": away, "referee_raw": ref,
        "kickoff_utc": datetime.datetime(2026, 9, day, 15, 0),
        "yellow_home": yh, "yellow_away": ya,
        "red_home": rh, "red_away": ra,
        "season": season, "source_id": ri.SOURCE_STATS,
        "match_id": f"m-{home}-{away}-{day}",
    }


def match(ref="P Bankes", mid="epl-20261031-chelsea-manutd"):
    return {
        "match_id": mid,
        "competition": "Premier League",
        "home_team": "Chelsea",
        "away_team": "Man Utd",
        "kickoff_time_utc": "2026-10-31T12:30:00Z",
        "referee_name_raw": ref,
    }


# =============================================================================
# A. 身份归一化
# =============================================================================
class TestRefereeIdentity(unittest.TestCase):
    def test_watchlist_csv_name_matched(self):
        rid, name, on_list, conf = ri.normalize_referee("S Attwell")
        self.assertEqual(rid, "stuart_attwell")
        self.assertEqual(name, "Stuart Attwell")
        self.assertTrue(on_list)
        self.assertEqual(conf, "watchlist")

    def test_full_name_and_dotted_name_match_same_id(self):
        ids = {ri.normalize_referee(n)[0]
               for n in ("Stuart Attwell", "S. Attwell", "S Attwell")}
        self.assertEqual(len(ids), 1, f"同一裁判的三种写法必须是同一个 id：{ids}")

    def test_all_seven_watch_referees_resolvable(self):
        """名单里 7 个人必须都能从源里的缩写形式解析出来。"""
        for w in cfg.WATCH_REFEREES:
            rid, _, on_list, _ = ri.normalize_referee(w["csv_names"][0])
            self.assertTrue(on_list, f"{w['display_name']} 未被识别")
            self.assertEqual(rid, w["referee_id"])

    def test_watchlist_has_exactly_seven(self):
        self.assertEqual(len(cfg.WATCH_REFEREES), 7,
                         "任务书要求七位，多一个少一个都要有明确理由")

    def test_unknown_referee_is_not_forced_onto_watchlist(self):
        rid, name, on_list, conf = ri.normalize_referee("M Salisbury")
        self.assertFalse(on_list)
        self.assertEqual(conf, "exact_name")
        self.assertEqual(name, "M Salisbury")

    def test_bare_surname_alias_is_registered(self):
        """'Bankes' 本身就是登记过的写法，属于确定匹配。"""
        _, _, on_list, conf = ri.normalize_referee("Bankes")
        self.assertTrue(on_list)
        self.assertEqual(conf, "watchlist")

    def test_wrong_initial_does_not_get_attributed_to_watchlist(self):
        """首字母不符时宁可不匹配，也不把别人的数据挂到名单裁判头上。"""
        _, _, on_list, conf = ri.normalize_referee("J Bankes")
        self.assertFalse(on_list, "同姓不同名不得当成名单里的那个人")
        self.assertEqual(conf, "exact_name")

    def test_empty_name(self):
        rid, name, on_list, conf = ri.normalize_referee("")
        self.assertEqual(rid, "unknown_referee")
        self.assertFalse(on_list)


# =============================================================================
# B. 统计与样本量纪律
# =============================================================================
class TestRefereeStats(unittest.TestCase):
    def test_sample_size_and_date_range(self):
        rows = [res("A", "B", "P Bankes", day=d) for d in (1, 5, 9)]
        stats = ri.build_referee_stats(rows)["peter_bankes"]
        self.assertEqual(stats["sample_size"], 3)
        self.assertEqual(stats["date_range"], ("2026-09-01", "2026-09-09"))
        self.assertEqual(stats["role"], ri.ROLE_REFEREE)

    def test_small_sample_cannot_conclude(self):
        rows = [res("A", "B", "P Bankes", day=d) for d in range(1, 6)]
        stats = ri.build_referee_stats(rows)["peter_bankes"]
        self.assertEqual(stats["sample_size"], 5)
        self.assertFalse(stats["can_conclude"])
        self.assertIn("不得据此下任何结论", stats["interpretation_warning"])

    def test_threshold_boundary(self):
        n = cfg.REFEREE_STATS_POLICY["min_matches_for_claim"]
        rows = [res("A", "B", "P Bankes", day=1 + i % 28) for i in range(n)]
        stats = ri.build_referee_stats(rows)["peter_bankes"]
        self.assertEqual(stats["sample_size"], n)
        self.assertTrue(stats["can_conclude"], f"恰好 {n} 场应允许下结论")

    def test_cards_are_summed_both_sides(self):
        rows = [res("A", "B", "P Bankes", yh=3, ya=2, rh=1, ra=0)]
        stats = ri.build_referee_stats(rows)["peter_bankes"]
        self.assertEqual(stats["yellow_total"], 5)
        self.assertEqual(stats["red_total"], 1)

    def test_league_average_is_computed(self):
        rows = [res("A", "B", "P Bankes", yh=1, ya=1, day=1),
                res("C", "D", "S Attwell", yh=3, ya=3, day=2)]
        stats = ri.build_referee_stats(rows)
        self.assertEqual(stats["peter_bankes"]["league_avg_yellow"], 4.0)
        self.assertEqual(stats["peter_bankes"]["yellow_per_match"], 2.0)

    def test_warning_always_present(self):
        """「黄牌多 ≠ 有问题」这句话必须永远跟着数字走。"""
        rows = [res("A", "B", "P Bankes", day=1)]
        stats = ri.build_referee_stats(rows)["peter_bankes"]
        self.assertIn("黄牌多不等于裁判有问题", stats["interpretation_warning"])

    def test_metric_note_is_recorded(self):
        rows = [res("A", "B", "P Bankes", day=1)]
        stats = ri.build_referee_stats(rows)["peter_bankes"]
        self.assertEqual(stats["metric_note"], cfg.REFEREE_STATS_POLICY["metric_note"])

    def test_rows_without_referee_are_skipped(self):
        stats = ri.build_referee_stats([res("A", "B", "", day=1)])
        self.assertEqual(stats, {})


# =============================================================================
# C. 任命记录：不许编造字段
# =============================================================================
class TestAppointment(unittest.TestCase):
    def test_confirmed_appointment_fields(self):
        rec = ri.build_appointment(match(), NOW)
        self.assertEqual(ri.validate_appointment(rec), [])
        self.assertEqual(rec["referee_name"], "Peter Bankes")
        self.assertEqual(rec["appointment_status"], ri.APPT_CONFIRMED)
        self.assertEqual(rec["role"], ri.ROLE_REFEREE)
        self.assertEqual(rec["appointment_date"], "2026-10-31")

    def test_published_at_is_never_fabricated(self):
        """源里没有公告发布时间 → 必须留空，不能拿「现在」冒充。"""
        rec = ri.build_appointment(match(), NOW)
        self.assertEqual(rec["published_at"], "")
        self.assertEqual(rec["first_seen_at"], "2026-10-31T12:50:00Z")

    def test_first_seen_at_is_preserved(self):
        rec = ri.build_appointment(match(), NOW, first_seen_at="2026-10-24T03:00:00Z")
        self.assertEqual(rec["first_seen_at"], "2026-10-24T03:00:00Z",
                         "首次观测时间必须保留原值，否则无法证明速度")

    def test_pending_when_no_referee(self):
        rec = ri.build_appointment(match(ref=""), NOW)
        self.assertEqual(rec["appointment_status"], ri.APPT_PENDING)
        self.assertEqual(rec["referee_name"], "")
        self.assertEqual(ri.validate_appointment(rec), [])

    def test_other_roles_marked_unavailable_not_guessed(self):
        recs = ri.build_unavailable_roles(match(), NOW)
        self.assertEqual(len(recs), 4)
        self.assertEqual({r["role"] for r in recs},
                         {ri.ROLE_ASSISTANT, ri.ROLE_FOURTH, ri.ROLE_VAR, ri.ROLE_AVAR})
        for r in recs:
            self.assertEqual(r["appointment_status"], ri.APPT_UNAVAILABLE)
            self.assertEqual(r["referee_name"], "",
                             "拿不到 VAR 就留空，不能猜一个名字")

    def test_validate_catches_confirmed_without_name(self):
        rec = ri.build_appointment(match(), NOW)
        rec["referee_name"] = ""
        self.assertTrue(ri.validate_appointment(rec))

    def test_validate_catches_bad_role(self):
        rec = ri.build_appointment(match(), NOW)
        rec["role"] = "Linesman"
        self.assertTrue(any("role" in p for p in ri.validate_appointment(rec)))


# =============================================================================
# D. 公布规律审计
# =============================================================================
class TestLeadTimeAudit(unittest.TestCase):
    def test_distribution_from_snapshot(self):
        appts = [
            ri.build_appointment(dict(match(), match_id="m1",
                                      kickoff_time_utc="2026-11-07T15:00:00Z"), NOW),
            ri.build_appointment(dict(match(), match_id="m2",
                                      kickoff_time_utc="2026-11-02T20:00:00Z"), NOW),
        ]
        audit = ri.audit_lead_time(appts, NOW)
        self.assertEqual(audit["samples"], 2)
        self.assertEqual(audit["min_days"], 2)
        self.assertEqual(audit["max_days"], 7)
        self.assertIn("单次快照", audit["note"])

    def test_no_samples(self):
        audit = ri.audit_lead_time([], NOW)
        self.assertEqual(audit["samples"], 0)
        self.assertIsNone(audit["median_days"])

    def test_pending_appointments_excluded(self):
        appts = [ri.build_appointment(match(ref=""), NOW)]
        self.assertEqual(ri.audit_lead_time(appts, NOW)["samples"], 0)


# =============================================================================
# E. 证据分级：球迷评论不得升级
# =============================================================================
class TestEvidenceGrades(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(ri.grade_evidence("official_confirm"), "A")
        self.assertEqual(ri.grade_evidence("official_explain"), "B")
        self.assertEqual(ri.grade_evidence("media_analysis"), "C")
        self.assertEqual(ri.grade_evidence("fan_comment"), "D")
        self.assertEqual(ri.grade_evidence("whatever"), "E")

    def test_fan_comment_never_becomes_fact(self):
        facts, leads = ri.split_by_grade([
            {"text": "球迷认为这是误判", "source_type": "fan_comment"},
        ])
        self.assertEqual(facts, [], "D 级线索绝不能进入事实陈述")
        self.assertEqual(len(leads), 1)

    def test_official_confirm_is_fact(self):
        facts, leads = ri.split_by_grade([
            {"text": "裁判机构承认误判", "source_type": "official_confirm"},
        ])
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["grade"], "A")
        self.assertEqual(leads, [])


# =============================================================================
# F. 九项报告
# =============================================================================
class TestNineItemReport(unittest.TestCase):
    def _report(self, disputes=None, mentions=0, ref="P Bankes"):
        m = match(ref=ref)
        appt = ri.build_appointment(m, NOW)
        stats = ri.build_referee_stats(
            [res("A", "B", "P Bankes", day=1)])["peter_bankes"]
        return ri.build_nine_item_report(m, appt, stats,
                                         disputes=disputes, mentions=mentions)

    def test_all_nine_items_present(self):
        rep = self._report()
        self.assertEqual([k for k, _ in ri.NINE_ITEMS], rep["order"])
        self.assertEqual(set(rep["items"]), {k for k, _ in ri.NINE_ITEMS})

    def test_unavailable_items_are_marked_not_filled(self):
        rep = self._report()
        self.assertEqual(rep["items"]["E"]["status"], ri.ITEM_UNAVAILABLE,
                         "VAR 拿不到就必须标 source_unavailable")
        self.assertEqual(rep["items"]["C"]["status"], ri.ITEM_INSUFFICIENT)
        self.assertIn("源不提供", rep["items"]["I"]["text"])

    def test_no_disputes_does_not_claim_no_dispute(self):
        """「没检索到」不等于「没有争议」，措辞必须区分。"""
        rep = self._report()
        self.assertEqual(rep["items"]["D"]["status"], ri.ITEM_INSUFFICIENT)
        self.assertIn("未检索到", rep["items"]["D"]["text"])
        self.assertNotIn("无争议", rep["items"]["D"]["text"].replace("不等于无争议", ""))

    def test_leads_are_flagged_as_not_facts(self):
        rep = self._report(disputes=[{"text": "球迷骂裁判", "source_type": "fan_comment"}])
        self.assertEqual(rep["facts_count"], 0)
        self.assertEqual(rep["leads_count"], 1)
        self.assertIn("不得作为误判陈述", rep["items"]["D"]["text"])

    def test_fact_dispute_counted(self):
        rep = self._report(disputes=[{"text": "官方承认误判",
                                      "source_type": "official_confirm"}])
        self.assertEqual(rep["facts_count"], 1)

    def test_render_contains_all_sections(self):
        text = ri.render_nine_item_report(self._report())
        for key, label in ri.NINE_ITEMS:
            self.assertIn(f"{key}. {label}", text)

    def test_risk_note_mentions_small_sample(self):
        m = match()
        appt = ri.build_appointment(m, NOW)
        stats = ri.build_referee_stats([res("A", "B", "P Bankes", day=1)])["peter_bankes"]
        rep = ri.build_nine_item_report(m, appt, stats)
        self.assertIn("样本仅", rep["items"]["H"]["text"])


# =============================================================================
# G. 八步流程
# =============================================================================
class TestEightSteps(unittest.TestCase):
    def _steps(self, ref="P Bankes", rows=None):
        m = match(ref=ref)
        appt = ri.build_appointment(m, NOW)
        stats = ri.build_referee_stats(
            rows or [res("A", "B", "P Bankes", day=1)]).get(appt["referee_id"])
        return ri.run_eight_steps(m, appt, stats)

    def test_exactly_eight_steps_in_order(self):
        steps = self._steps()
        self.assertEqual(len(steps), 8)
        self.assertEqual([s["step"] for s in steps], [k for k, _ in ri.EIGHT_STEPS])

    def test_human_review_is_always_pending(self):
        steps = self._steps()
        last = steps[-1]
        self.assertEqual(last["step"], "human_review")
        self.assertEqual(last["status"], ri.STEP_PENDING,
                         "未经人工复核不得对外定性")

    def test_missing_data_steps_are_marked(self):
        steps = {s["step"]: s for s in self._steps()}
        self.assertEqual(steps["team_history"]["status"], ri.STEP_INSUFFICIENT)
        self.assertEqual(steps["disputes"]["status"], ri.STEP_INSUFFICIENT)

    def test_unknown_referee_marks_pending(self):
        steps = {s["step"]: s for s in self._steps(ref="")}
        self.assertEqual(steps["confirm_appointment"]["status"], ri.STEP_PENDING)
        self.assertEqual(steps["pull_history"]["status"], ri.STEP_INSUFFICIENT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
