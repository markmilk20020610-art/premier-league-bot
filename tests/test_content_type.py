"""任务 1 验收：内容类型分类。

覆盖三类硬要求：
  · 普通商品推广必须被判为 PROMOTION（否则会挤占重大新闻名额）
  · 所有权 / 财务危机 / 赞助变化不得因为含商业词被误过滤
  · 判定失败走保守默认（OTHER），不得误删重要新闻
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import content_type as ct
import evidence as ev


def cls(title, desc="", event_type=None):
    et = ev.primary_event_type(title) if event_type is None else event_type
    return ct.classify(title, desc, event_type=et)


class TestPromotion(unittest.TestCase):
    """商品推广：必须判出 PROMOTION，且被标为低价值。"""

    def test_kit_launch(self):
        r = cls("Spurs unveil 2026/27 home kit in club shop")
        self.assertEqual(r["content_type"], ct.PROMOTION)
        self.assertTrue(r["is_low_value"])

    def test_membership_giveaway(self):
        r = cls("Chelsea membership: join now and win a signed shirt")
        self.assertEqual(r["content_type"], ct.PROMOTION)

    def test_fan_vote(self):
        r = cls("Liverpool fans vote for November player of the month")
        self.assertEqual(r["content_type"], ct.PROMOTION)

    def test_boots_collection(self):
        r = cls("Cristiano Ronaldo launches new CR7 boots collection")
        self.assertEqual(r["content_type"], ct.PROMOTION)

    def test_chinese_promo(self):
        r = cls("阿森纳官方商店新品球衣预售，限时折扣")
        self.assertEqual(r["content_type"], ct.PROMOTION)


class TestExemption(unittest.TestCase):
    """豁免规则：商业词不得导致重要新闻被误过滤。"""

    def test_club_takeover_not_promotion(self):
        r = cls("Chelsea takeover completed as consortium buys majority stake")
        self.assertEqual(r["content_type"], ct.CLUB_GOVERNANCE)
        self.assertFalse(r["is_low_value"])
        self.assertTrue(ct.is_commercial_but_important(r["content_type"]))

    def test_administration_not_promotion(self):
        r = cls("Sheffield Wednesday enter administration as debts spiral")
        self.assertIn(r["content_type"], (ct.CLUB_GOVERNANCE, ct.FINANCIAL_NEWS))
        self.assertFalse(r["is_low_value"])

    def test_shirt_sponsor_is_financial_news(self):
        r = cls("Chelsea announce new shirt sponsor in record deal")
        self.assertEqual(r["content_type"], ct.FINANCIAL_NEWS)
        self.assertFalse(r["is_low_value"])

    def test_psr_breach_not_promotion(self):
        r = cls("Everton points deduction: club appeal against PSR breach")
        self.assertFalse(r["is_low_value"])

    def test_ticket_price_protest_is_governance(self):
        r = cls("Fans protest as season ticket price rise announced")
        self.assertEqual(r["content_type"], ct.CLUB_GOVERNANCE)
        self.assertFalse(r["is_low_value"])


class TestConservativeDefault(unittest.TestCase):
    """判定失败必须保守，不得误删。"""

    def test_empty_title(self):
        r = ct.classify("")
        self.assertEqual(r["content_type"], ct.OTHER)
        self.assertFalse(r["is_low_value"])

    def test_unknown_title_falls_back_to_other(self):
        r = cls("Something happened somewhere on Tuesday")
        self.assertEqual(r["content_type"], ct.OTHER)
        self.assertFalse(r["is_low_value"])

    def test_promo_words_vetoed_by_news_signal(self):
        # 含推广词但明显是新闻：不得判成广告
        r = cls("Arsenal announce new shirt deal after transfer ban lifted")
        self.assertFalse(r["is_low_value"])


class TestExplainability(unittest.TestCase):
    """每次判定都要留下可追溯的依据。"""

    def test_result_has_rule_and_reasons(self):
        r = cls("Spurs unveil 2026/27 home kit in club shop")
        self.assertTrue(r["rule_id"])
        self.assertTrue(r["reasons"])
        self.assertIn(r["content_type"], ct.CONTENT_TYPES)

    def test_confidence_in_range(self):
        for t in ("Spurs unveil home kit", "Arsenal agree deal for striker",
                  "Chelsea takeover completed", "random words here"):
            r = cls(t)
            self.assertGreaterEqual(r["confidence"], 0.0)
            self.assertLessEqual(r["confidence"], 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
