"""任务 2 验收：重要性分级 S/A/B/C。

核心命题（必须由测试守住，而不是写在文档里）：
  · 球员姓名不直接决定重要性
  · 重要性 ≠ 可信度
  · 未知默认 B，不默认 C
  · 俱乐部所有权/财务变化不被商业过滤器降级
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import content_type as ct
import evidence as ev
import importance as imp


def assess(title, desc="", groups=0, tier1=False, wire=False, source_count=1):
    et = ev.primary_event_type(title)
    c = ct.classify(title, desc, event_type=et)
    return imp.assess(title, desc, event_type=et, content_type=c["content_type"],
                      independent_groups=groups, has_tier1=tier1, has_wire=wire,
                      source_count=source_count)


class TestPlayerNameDoesNotDecide(unittest.TestCase):
    """球员姓名不得直接决定重要性。"""

    def test_star_promotion_is_still_c(self):
        # 顶级球星 + 商品推广 → 只能是 C
        r = assess("Cristiano Ronaldo launches new CR7 boots collection")
        self.assertEqual(r["importance"], imp.C)
        self.assertEqual(r["rule_id"], "CEILING_PROMOTION")

    def test_star_gossip_is_still_c(self):
        r = assess("Cristiano Ronaldo's girlfriend shows off new supercar")
        self.assertEqual(r["importance"], imp.C)

    def test_career_end_without_evidence_is_not_s(self):
        # 同一事件类型，但证据广度不足 → 不能给 S
        weak = assess("Ronaldo retires from Portugal duty", groups=1)
        strong = assess("Ronaldo retires from Portugal duty", groups=5, tier1=True)
        self.assertNotEqual(weak["importance"], imp.S)
        self.assertEqual(strong["importance"], imp.S)

    def test_unknown_player_same_signals_same_level(self):
        # 换掉人名、其他信号不变 → 等级必须一致（证明判定不依赖姓名）
        a = assess("Cristiano Ronaldo announces retirement from international football",
                   groups=4, tier1=True)
        b = assess("John Smith announces retirement from international football",
                   groups=4, tier1=True)
        self.assertEqual(a["importance"], b["importance"])


class TestLevels(unittest.TestCase):
    def test_global_disaster_is_s(self):
        r = assess("Stadium crush in South America leaves dozens injured")
        self.assertEqual(r["importance"], imp.S)

    def test_multi_club_match_fixing_is_s(self):
        r = assess("Italian prosecutors open match-fixing inquiry into 12 clubs")
        self.assertEqual(r["importance"], imp.S)

    def test_pl_transfer_is_a(self):
        r = assess("Arsenal agree deal for striker as transfer talks advance")
        self.assertEqual(r["importance"], imp.A)
        self.assertEqual(r["signals"]["scope"], imp.A_LINE)

    def test_pl_injury_is_a(self):
        r = assess("Newcastle confirm Sven Botman out for eight weeks with knee injury")
        self.assertEqual(r["importance"], imp.A)

    def test_pl_ownership_is_a(self):
        r = assess("Man Utd takeover: Qatari consortium completes stake purchase")
        self.assertEqual(r["importance"], imp.A)

    def test_routine_foreign_match_is_c(self):
        r = assess("Real Betis and Sevilla draw in pre-season friendly")
        self.assertEqual(r["importance"], imp.C)
        self.assertEqual(r["signals"]["scope"], imp.GLOBAL_EVENT_LINE)

    def test_unknown_defaults_to_b_not_c(self):
        # 兜底必须是 B：C 会被排序降权，等于静默删除
        r = imp.assess("Something happened somewhere", event_type="",
                       content_type=ct.OTHER)
        self.assertEqual(r["importance"], imp.B)
        self.assertEqual(r["rule_id"], "FALLBACK_B")


class TestSevillaNotPL(unittest.TestCase):
    """villa 是 Aston Villa 的简称，也是 Sevilla 的子串——必须词边界匹配。"""

    def test_sevilla_is_not_premier_league(self):
        scope, _ = imp.classify_scope("Real Betis and Sevilla draw in pre-season friendly")
        self.assertEqual(scope, imp.GLOBAL_EVENT_LINE)

    def test_aston_villa_is_premier_league(self):
        scope, _ = imp.classify_scope("Aston Villa beat Wolves at Villa Park")
        self.assertEqual(scope, imp.A_LINE)


class TestCombine(unittest.TestCase):
    def test_event_level_takes_max(self):
        self.assertEqual(imp.combine([imp.B, imp.S, imp.C]), imp.S)

    def test_empty_combine_defaults_b(self):
        self.assertEqual(imp.combine([]), imp.B)

    def test_rank_order(self):
        self.assertGreater(imp.rank(imp.S), imp.rank(imp.A))
        self.assertGreater(imp.rank(imp.A), imp.rank(imp.B))
        self.assertGreater(imp.rank(imp.B), imp.rank(imp.C))


class TestExplainability(unittest.TestCase):
    def test_every_result_has_rule_and_signals(self):
        r = assess("Arsenal agree deal for striker as transfer talks advance")
        self.assertTrue(r["rule_id"])
        self.assertTrue(r["reasons"])
        self.assertIn("scope", r["signals"])
        self.assertIn("independent_groups", r["signals"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
