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


# ==================== V1.1 第一批验收 ====================
BBC2 = "https://www.bbc.co.uk/sport/football/articles/c999"
SKY2 = "https://www.skysports.com/football/news/11095/arsenal-2"
ARS = "https://www.arsenal.com/news/striker-update"
FA2 = "https://www.thefa.com/news/2026/oct/30/statement-2"
UNK_A = "https://transfer-rumours-a.example/arsenal-striker"
UNK_B = "https://football-gossip-b.example/arsenal-striker"
WIRE_1 = "https://www.reuters.com/sports/football/arsenal-striker-deal"
WIRE_2 = "https://www.reuters.com/sports/football/arsenal-striker-latest"

TRANSFER_TITLE = "Arsenal agree deal for striker as transfer talks advance"
INJURY_TITLE = "Arsenal confirm striker injury after transfer window opens"


class TestTask1OfficialMustProveSameEvent(unittest.TestCase):
    """任务 1：官方确认必须证明是同一件具体事件，否则降级。"""

    def test_official_other_event_is_not_confirmation(self):
        # BBC 说「谈妥前锋转会」，arsenal.com 那条是「窗口开启后确认前锋受伤」
        # 两家都是阿森纳、都出现 striker / transfer，但明显是两件事。
        records = [
            {"url": BBC2, "title": TRANSFER_TITLE},
            {"url": ARS, "title": INJURY_TITLE},
        ]
        verdict = ev.evaluate(BBC2, records)
        self.assertNotEqual(verdict["status"], ev.OFFICIAL_CONFIRMED)
        self.assertEqual(verdict["status"], ev.UNVERIFIED,
                         "官方来源指向另一事件时，官方确认不成立，降级为 UNVERIFIED")
        self.assertIn("arsenal.com", verdict["official_rejected"])
        self.assertFalse(ev.auto_push_allowed(verdict)[0])

    def test_official_same_event_is_confirmed(self):
        records = [
            {"url": BBC2, "title": "Arsenal agree deal for striker as transfer talks advance"},
            {"url": ARS, "title": "Arsenal agree deal for striker after transfer talks"},
        ]
        verdict = ev.evaluate(BBC2, records)
        self.assertEqual(verdict["status"], ev.OFFICIAL_CONFIRMED)
        self.assertTrue(ev.auto_push_allowed(verdict)[0])

    def test_official_anchor_self_confirms(self):
        records = [{"url": FA2, "title": "FA statement on disciplinary matter"}]
        verdict = ev.evaluate(FA2, records)
        self.assertEqual(verdict["status"], ev.OFFICIAL_CONFIRMED)

    def test_same_event_requires_subject_and_type(self):
        a = ev.build_records([{"url": BBC2, "title": TRANSFER_TITLE}])[0]
        b = ev.build_records([{"url": ARS, "title": INJURY_TITLE}])[0]
        ok, why = ev.same_event(a, b)
        self.assertFalse(ok)
        self.assertIn("事件类型不同", why)

        c = ev.build_records([{"url": ARS, "title": "Chelsea confirm striker deal after transfer talks"}])[0]
        ok2, why2 = ev.same_event(a, c)
        self.assertFalse(ok2)
        self.assertIn("主体不同", why2)


class TestTask2UnknownDomainsDoNotCount(unittest.TestCase):
    """任务 2：UNKNOWN 域名不得计入 MULTIPLE_INDEPENDENT_SOURCES。"""

    def test_two_unknown_sites_are_not_cross_verification(self):
        records = [
            {"url": UNK_A, "title": TRANSFER_TITLE},
            {"url": UNK_B, "title": TRANSFER_TITLE},
        ]
        verdict = ev.evaluate(UNK_A, records)
        self.assertNotEqual(verdict["status"], ev.MULTIPLE_INDEPENDENT_SOURCES)
        self.assertEqual(verdict["count"], 1, "两个来路不明的域名最多算 1 个来源")
        self.assertEqual(verdict["status"], ev.SINGLE_SOURCE)

    def test_unknown_cannot_upgrade_known_source(self):
        records = [
            {"url": BBC2, "title": "Arsenal lose appeal against Dowman red card"},
            {"url": UNK_A, "title": "FA rejects Arsenal appeal over Dowman dismissal"},
        ]
        verdict = ev.evaluate(BBC2, records)
        self.assertNotEqual(verdict["status"], ev.MULTIPLE_INDEPENDENT_SOURCES)
        self.assertEqual(verdict["count"], 1)

    def test_aggregator_never_counts(self):
        gnews = "https://news.google.com/rss/articles/CBMiabc"
        records = [
            {"url": BBC2, "title": "Arsenal lose appeal against Dowman red card"},
            {"url": gnews, "title": "FA rejects Arsenal appeal over Dowman dismissal"},
        ]
        verdict = ev.evaluate(BBC2, records)
        self.assertEqual(verdict["count"], 1)

    def test_single_whitelist_source_of_truth(self):
        """来源分级只认 evidence.classify_source 一份白名单。"""
        self.assertEqual(ev.classify_source("arsenal.com"), "OFFICIAL")
        self.assertEqual(ev.classify_source("www2.arsenal.com"), "OFFICIAL")
        self.assertEqual(ev.classify_source("transfer-rumours-a.example"), "UNKNOWN")
        self.assertEqual(ev.classify_source(""), "UNKNOWN")


class TestTask3ReprintDetection(unittest.TestCase):
    """任务 3：转载 / 通稿识别。"""

    def test_three_near_identical_reprints_count_once(self):
        records = [
            {"url": BBC2, "title": "Arsenal lose appeal against Dowman red card"},
            {"url": "https://www.dailymail.com/sport/article-999",
             "title": "Arsenal lose appeal against Dowman red card"},
            {"url": "https://www.mirror.co.uk/sport/football/news/arsenal-123",
             "title": "Arsenal lose appeal against Dowman red card after FA hearing"},
        ]
        verdict = ev.evaluate(BBC2, records)
        self.assertEqual(verdict["count"], 1, "三家域名同一篇稿 → 1 个来源")
        self.assertEqual(verdict["status"], ev.SINGLE_SOURCE)

    def test_two_independent_outlets_count_twice(self):
        records = [
            {"url": BBC2, "title": "Arsenal lose appeal against Dowman red card"},
            {"url": SKY2, "title": "FA rejects Arsenal appeal over Dowman dismissal"},
        ]
        verdict = ev.evaluate(BBC2, records)
        self.assertEqual(verdict["count"], 2)
        self.assertEqual(verdict["status"], ev.MULTIPLE_INDEPENDENT_SOURCES)

    def test_two_events_same_club_not_merged(self):
        records = [
            {"url": BBC2, "title": TRANSFER_TITLE},
            {"url": SKY2, "title": INJURY_TITLE},
        ]
        verdict = ev.evaluate(BBC2, records)
        self.assertEqual(verdict["count"], 1, "同一俱乐部的两件不同事件不得互相印证")
        self.assertEqual(verdict["status"], ev.SINGLE_SOURCE)

    def test_two_unknown_sites_not_independent(self):
        records = [
            {"url": UNK_A, "title": "Arsenal lose appeal against Dowman red card"},
            {"url": UNK_B, "title": "FA rejects Arsenal appeal over Dowman dismissal"},
        ]
        verdict = ev.evaluate(UNK_A, records)
        self.assertNotEqual(verdict["status"], ev.MULTIPLE_INDEPENDENT_SOURCES)

    def test_same_wire_service_counts_once(self):
        """同一家通讯社的两条稿子不是两个独立来源。"""
        records = [
            {"url": WIRE_1, "title": "Arsenal agree deal for striker as transfer talks advance"},
            {"url": WIRE_2, "title": "Arsenal close in on striker after fresh transfer talks"},
        ]
        verdict = ev.evaluate(WIRE_1, records)
        self.assertEqual(verdict["count"], 1)
        self.assertEqual(verdict["status"], ev.SINGLE_SOURCE)

    def test_boilerplate_description_is_not_reprint_evidence(self):
        """两篇不同报道的 RSS 摘要都是同一句样板话时，不能被判成转载。"""
        records = [
            {"url": BBC2, "title": "Arsenal lose appeal against Dowman red card",
             "desc": "Read more on our football page."},
            {"url": SKY2, "title": "FA rejects Arsenal appeal over Dowman dismissal",
             "desc": "Read more on our football page."},
        ]
        verdict = ev.evaluate(BBC2, records)
        self.assertEqual(verdict["count"], 2, "样板摘要不得把两篇不同报道合并成一个来源")


class TestTask4SingleSourceGate(unittest.TestCase):
    """任务 4：SINGLE_SOURCE 默认不得自动推送，白名单例外且必须标注。"""

    def single(self):
        return ev.evaluate(SUN, [{"url": SUN, "title": "Arsenal ready to bid 60m for new striker"}])

    def test_single_source_not_auto_pushed_by_default(self):
        verdict = self.single()
        self.assertEqual(verdict["status"], ev.SINGLE_SOURCE)
        allowed, why = ev.auto_push_allowed(verdict)
        self.assertFalse(allowed)
        self.assertIn("不在可信白名单", why)

    def test_allowlisted_single_source_allowed(self):
        verdict = self.single()
        allowed, _ = ev.auto_push_allowed(verdict, {"thesun.co.uk"})
        self.assertTrue(allowed)

    def test_allowlist_does_not_cover_unknown(self):
        verdict = ev.evaluate(UNK_A, [{"url": UNK_A, "title": TRANSFER_TITLE}])
        allowed, why = ev.auto_push_allowed(verdict, {"transfer-rumours-a.example"})
        self.assertFalse(allowed, "UNKNOWN 域名即使写进白名单也不得放行")
        self.assertIn("未识别", why)

    def test_single_source_note_has_name_url_and_disclaimer(self):
        verdict = self.single()
        note = ev.single_source_note(verdict)
        self.assertEqual(note["source_name"], "thesun.co.uk")
        self.assertEqual(note["url"], SUN)
        self.assertIn("未经独立交叉验证", note["note"])
        # 多源时不应产生单一来源标注
        multi = ev.evaluate(BBC2, [
            {"url": BBC2, "title": "Arsenal lose appeal against Dowman red card"},
            {"url": SKY2, "title": "FA rejects Arsenal appeal over Dowman dismissal"},
        ])
        self.assertEqual(ev.single_source_note(multi), {})

    def test_multi_source_still_allowed(self):
        verdict = ev.evaluate(BBC2, [
            {"url": BBC2, "title": "Arsenal lose appeal against Dowman red card"},
            {"url": SKY2, "title": "FA rejects Arsenal appeal over Dowman dismissal"},
        ])
        self.assertTrue(ev.auto_push_allowed(verdict)[0])


class TestTask5BlackLabelGate(unittest.TestCase):
    """任务 5：黑标 / 严肃议题的证据闸门。"""

    def test_low_trust_single_source_cannot_go_black_label(self):
        url = "https://www.thesun.co.uk/sport/football/99999/arsenal-injury"
        verdict = ev.evaluate(url, [{
            "url": url,
            "title": "Arsenal star suffers serious knee injury in training",
        }])
        self.assertEqual(verdict["status"], ev.SINGLE_SOURCE)
        allowed, why = ev.black_label_gate("黑标", verdict, "球员训练中受重伤")
        self.assertFalse(allowed, "单一低可信来源不得自动生成黑标快讯")
        self.assertIn("证据不足", why)

    def test_model_black_label_cannot_bypass_gate(self):
        """模型自己写 var_rating=黑标 不能绕过闸门（不看模型自信，只看证据）。"""
        url = "https://www.thesun.co.uk/sport/football/99999/arsenal-injury"
        verdict = ev.evaluate(url, [{"url": url, "title": "Arsenal player injured"}])
        allowed, _ = ev.black_label_gate("黑标", verdict, "")
        self.assertFalse(allowed)

    def test_black_label_allowed_with_two_independent_sources(self):
        verdict = ev.evaluate(BBC2, [
            {"url": BBC2, "title": "Arsenal player dies after road accident"},
            {"url": SKY2, "title": "Tragedy as Arsenal squad mourn player death"},
        ])
        self.assertEqual(verdict["status"], ev.MULTIPLE_INDEPENDENT_SOURCES)
        allowed, _ = ev.black_label_gate("黑标", verdict, "球员事故身亡")
        self.assertTrue(allowed)

    def test_strict_topic_detected_even_without_model_label(self):
        """模型没标黑标，但内容涉及死亡/重伤 → 仍按严肃议题卡证据。"""
        self.assertEqual(ev.strict_topic("Arsenal legend dies aged 78"), "DEATH")
        self.assertEqual(ev.strict_topic("Player arrested after police probe"), "LEGAL")
        self.assertEqual(ev.strict_topic("Star suffers serious knee injury"), "SEVERE_INJURY")
        self.assertEqual(ev.strict_topic("Arsenal agree deal for striker"), "")

    def test_unverified_and_conflicting_never_pushable(self):
        for status in (ev.UNVERIFIED, ev.CONFLICTING):
            self.assertFalse(ev.is_pushable(status))
            self.assertFalse(ev.auto_push_allowed({"status": status})[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
