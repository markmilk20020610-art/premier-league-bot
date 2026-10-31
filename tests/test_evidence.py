"""阶段 1.1 验收：证据核验七类场景。

每个用例都断言「状态正确」且「是否允许自动推送」正确。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import evidence as ev


BBC = "https://www.bbc.co.uk/sport/football/articles/c123"
SKY = "https://www.skysports.com/football/news/11095/arsenal"
SUN = "https://www.thesun.co.uk/sport/football/12345/arsenal"
FA = "https://www.thefa.com/news/2026/oct/30/statement"
MAIL = "https://www.dailymail.com/sport/football/article-999"
MIRROR = "https://www.mirror.co.uk/sport/football/news/arsenal-123"


class TestEvidenceStatus(unittest.TestCase):
    # ---------- 案例 1：有 Grounding URL，但与新闻无关 ----------
    def test_01_grounding_unrelated_to_news(self):
        records = [
            {"url": "https://www.bbc.co.uk/news/technology-123", "title": "Apple releases new iPhone"},
            {"url": "https://www.theguardian.com/uk-news/2026/oct/30/weather", "title": "Storm warning issued"},
        ]
        verdict = ev.evaluate(BBC, records)
        self.assertEqual(verdict["status"], ev.UNVERIFIED)
        self.assertFalse(ev.is_pushable(verdict["status"]))
        self.assertIn("不在证据集合中", verdict["reason"])

    # ---------- 案例 2：evidence_url 不属于证据列表 ----------
    def test_02_evidence_url_not_in_records(self):
        records = [{"url": SKY, "title": "Arsenal injury update ahead of derby"}]
        verdict = ev.evaluate(BBC, records)   # 模型编了个 BBC 链接
        self.assertEqual(verdict["status"], ev.UNVERIFIED)
        self.assertFalse(verdict["evidence_url_matched"])
        self.assertFalse(ev.is_pushable(verdict["status"]))

    # ---------- 案例 3：只有单一爆料来源 ----------
    def test_03_single_low_trust_source(self):
        records = [{"url": SUN, "title": "Arsenal ready to bid 60m for new striker"}]
        verdict = ev.evaluate(SUN, records)
        self.assertEqual(verdict["status"], ev.SINGLE_SOURCE)
        self.assertEqual(verdict["count"], 1)
        self.assertTrue(ev.is_pushable(verdict["status"]))
        # 单一小报来源必须被压到 Tier 3
        key, downgraded, _ = ev.enforce_rating("Tier 1", verdict["status"], ["LOW_TRUST"])
        self.assertEqual(key, "Tier 3")
        self.assertTrue(downgraded)

    # ---------- 案例 4：多家媒体转载同一篇报道 ----------
    def test_04_reprints_count_as_one_source(self):
        same_title = "Arsenal lose appeal against Dowman red card"
        records = [
            {"url": BBC, "title": same_title},
            {"url": MAIL, "title": same_title},
            {"url": MIRROR, "title": same_title},
        ]
        verdict = ev.evaluate(BBC, records)
        # 三家域名，但标题指纹一致 → 只有 1 个独立来源
        self.assertEqual(verdict["count"], 1)
        self.assertEqual(verdict["status"], ev.SINGLE_SOURCE)
        self.assertNotEqual(verdict["status"], ev.MULTIPLE_INDEPENDENT_SOURCES)

    # ---------- 案例 4b：真正独立的多源 ----------
    def test_04b_genuinely_independent_sources(self):
        records = [
            {"url": BBC, "title": "Arsenal lose appeal against Dowman red card"},
            {"url": SKY, "title": "FA rejects Arsenal red card appeal"},
        ]
        verdict = ev.evaluate(BBC, records)
        self.assertEqual(verdict["count"], 2)
        self.assertEqual(verdict["status"], ev.MULTIPLE_INDEPENDENT_SOURCES)
        self.assertTrue(ev.is_pushable(verdict["status"]))

    # ---------- 案例 5：不同来源对同一事件说法冲突 ----------
    def test_05_conflicting_sources(self):
        records = [
            {"url": BBC, "title": "Arsenal set to sign striker in January"},
            {"url": SKY, "title": "Arsenal rule out January striker signing"},
        ]
        claims = [
            {"claim_key": "will_sign_striker", "stance": "supports", "source": "bbc.co.uk"},
            {"claim_key": "will_sign_striker", "stance": "denies", "source": "skysports.com"},
        ]
        verdict = ev.evaluate(BBC, records, source_claims=claims)
        self.assertEqual(verdict["status"], ev.CONFLICTING)
        self.assertFalse(ev.is_pushable(verdict["status"]))

    # ---------- 官方渠道确认 ----------
    def test_06_official_confirmed(self):
        records = [{"url": FA, "title": "FA statement on disciplinary matter"}]
        verdict = ev.evaluate(FA, records)
        self.assertEqual(verdict["status"], ev.OFFICIAL_CONFIRMED)
        self.assertTrue(ev.is_pushable(verdict["status"]))

    # ---------- 聚合器不能当证据 ----------
    def test_07_aggregator_only(self):
        gnews = "https://news.google.com/rss/articles/CBMiabc"
        verdict = ev.evaluate(gnews, [{"url": gnews, "title": "Arsenal news"}])
        self.assertEqual(verdict["status"], ev.UNVERIFIED)
        self.assertFalse(ev.is_pushable(verdict["status"]))

    # ---------- 无任何证据（DeepSeek 场景）----------
    def test_08_no_records_is_unverified(self):
        verdict = ev.evaluate(BBC, [])
        self.assertEqual(verdict["status"], ev.UNVERIFIED)
        self.assertEqual(verdict["count"], 0)
        self.assertFalse(ev.is_pushable(verdict["status"]))

    # ---------- 域名与规范化 ----------
    def test_09_domain_and_canonical(self):
        self.assertEqual(ev.extract_domain("https://www.bbc.co.uk/news/x"), "bbc.co.uk")
        # 可注册域名口径：news.google.com 归并为 google.com，便于跨子域去重
        self.assertEqual(ev.extract_domain("https://news.google.com/rss/articles/x"), "google.com")
        self.assertEqual(ev.classify_source("google.com"), "AGGREGATOR")
        self.assertEqual(
            ev.canonical_url("https://WWW.BBC.co.uk/sport/a/?oc=5#top"),
            "https://bbc.co.uk/sport/a",
        )
        self.assertEqual(ev.classify_source("thefa.com"), "OFFICIAL")
        self.assertEqual(ev.classify_source("bbc.co.uk"), "TIER1")
        self.assertEqual(ev.classify_source("thesun.co.uk"), "LOW_TRUST")
        self.assertEqual(ev.classify_source("rsshub.app"), "AGGREGATOR")


    # ---------- 黑标不得被降级（内容安全标记，不是可信度分级）----------
    def test_10_black_label_never_downgraded(self):
        key, downgraded, _ = ev.enforce_rating("黑标", ev.SINGLE_SOURCE, ["LOW_TRUST"])
        self.assertEqual(key, "黑标")
        self.assertFalse(downgraded)
        key2, _, _ = ev.enforce_rating("黑标", ev.UNVERIFIED, [])
        self.assertEqual(key2, "黑标")


if __name__ == "__main__":
    unittest.main(verbosity=2)
