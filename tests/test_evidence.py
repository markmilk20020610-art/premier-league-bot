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


# ==================== V1.2 第一批验收 ====================
MAL_ARTICLE = "https://malicious.example/fake-liverpool-news"
RSSHUB_FEED = "https://rsshub.app/test"
GNEWS_FEED = "https://news.google.com/rss/search?q=liverpool"
LFC = "https://www.liverpoolfc.com/news/olise-deal"

OLISE_TITLE = "Liverpool agree deal to sign winger Michael Olise"
GORDON_TITLE = "Liverpool agree deal to sign winger Anthony Gordon"


class TestTask1FeedTrustBoundary(unittest.TestCase):
    """任务 1：第三方 RSS 不得用 <source url> 冒充官方来源。"""

    def test_malicious_feed_cannot_claim_official(self):
        records = [{
            "url": MAL_ARTICLE,
            "title": OLISE_TITLE,
            "publisher": "https://www.liverpoolfc.com",
            "feed_origin": RSSHUB_FEED,
        }]
        built = ev.build_records(records)[0]
        self.assertNotEqual(built["source_class"], "OFFICIAL",
                            "不可信 feed 的 source 元数据不得把文章升格为官方")
        self.assertEqual(built["source_class"], "UNKNOWN")
        self.assertEqual(built["url_domain"], "malicious.example")
        self.assertEqual(built["publisher_domain"], "liverpoolfc.com")
        self.assertFalse(built["publisher_trusted"])
        self.assertTrue(built["identity_conflict"])

        verdict = ev.evaluate(MAL_ARTICLE, records)
        self.assertNotEqual(verdict["status"], ev.OFFICIAL_CONFIRMED)
        self.assertFalse(ev.auto_push_allowed(verdict)[0])

    def test_google_news_feed_may_use_source_metadata(self):
        records = [{
            "url": "https://news.google.com/rss/articles/CBMiX",
            "title": OLISE_TITLE,
            "publisher": "https://www.liverpoolfc.com",
            "feed_origin": GNEWS_FEED,
        }]
        built = ev.build_records(records)[0]
        self.assertEqual(built["source_class"], "OFFICIAL")
        self.assertTrue(built["publisher_trusted"])
        self.assertFalse(built["identity_conflict"])
        self.assertEqual(built["domain"], "liverpoolfc.com")
        self.assertEqual(built["url_domain"], "google.com")

    def test_non_google_feed_publisher_is_ignored(self):
        """直连 feed（bbc）自带的 publisher 即使写着官方域名也不采纳。"""
        records = [{
            "url": "https://www.bbc.co.uk/sport/football/articles/c1",
            "title": OLISE_TITLE,
            "publisher": "https://www.liverpoolfc.com",
            "feed_origin": "https://feeds.bbci.co.uk/sport/football/rss.xml",
        }]
        built = ev.build_records(records)[0]
        self.assertEqual(built["domain"], "bbc.co.uk")
        self.assertEqual(built["source_class"], "TIER1")
        self.assertFalse(built["publisher_trusted"])

    def test_matching_publisher_and_url_is_not_a_conflict(self):
        records = [{
            "url": "https://www.bbc.co.uk/sport/football/articles/c1",
            "title": OLISE_TITLE,
            "publisher": "https://www.bbc.co.uk",
            "feed_origin": "https://feeds.bbci.co.uk/sport/football/rss.xml",
        }]
        built = ev.build_records(records)[0]
        self.assertFalse(built["identity_conflict"])
        self.assertEqual(built["source_class"], "TIER1")

    def test_spoofed_official_record_cannot_confirm_other_story(self):
        """即使池子里混进一条冒充官方的记录，也不能让真新闻变成官方确认。"""
        records = [
            {"url": BBC2, "title": OLISE_TITLE,
             "feed_origin": "https://feeds.bbci.co.uk/sport/football/rss.xml"},
            {"url": MAL_ARTICLE, "title": OLISE_TITLE,
             "publisher": "https://www.liverpoolfc.com", "feed_origin": RSSHUB_FEED},
        ]
        verdict = ev.evaluate(BBC2, records)
        self.assertNotEqual(verdict["status"], ev.OFFICIAL_CONFIRMED)

    def test_source_id_is_program_derived(self):
        records = ev.build_records([{"url": BBC2, "title": OLISE_TITLE}])
        self.assertTrue(records[0]["source_id"].startswith("src_"))
        same = ev.build_records([{"url": BBC2 + "/", "title": "other"}])
        self.assertEqual(records[0]["source_id"], same[0]["source_id"],
                         "同一规范化 URL 必须得到同一个 source_id")


class TestTask3EntityAwareSameEvent(unittest.TestCase):
    """任务 3：same_event 必须识别球员/教练主体。"""

    def build(self, url, title, **extra):
        return ev.build_records([dict({"url": url, "title": title}, **extra)])[0]

    def test_different_players_are_different_events(self):
        a = self.build("https://www.bbc.co.uk/a", OLISE_TITLE)
        b = self.build(LFC, GORDON_TITLE)
        ok, why = ev.same_event(a, b)
        self.assertFalse(ok, "同俱乐部不同球员的转会不得判为同一事件")
        self.assertIn("人物主体不同", why)

    def test_different_players_block_official_confirmation(self):
        records = [
            {"url": "https://www.bbc.co.uk/a", "title": OLISE_TITLE,
             "feed_origin": "https://feeds.bbci.co.uk/sport/football/rss.xml"},
            {"url": LFC, "title": GORDON_TITLE,
             "feed_origin": "https://feeds.bbci.co.uk/sport/football/rss.xml"},
        ]
        verdict = ev.evaluate("https://www.bbc.co.uk/a", records)
        self.assertNotEqual(verdict["status"], ev.OFFICIAL_CONFIRMED)
        self.assertFalse(ev.auto_push_allowed(verdict)[0])

    def test_same_player_different_event_is_not_merged(self):
        """同一球员、不同事件：转会 vs 伤情，不能被合并。"""
        a = self.build("https://www.bbc.co.uk/a", "Liverpool agree deal to sign winger Michael Olise")
        b = self.build("https://www.bbc.co.uk/b",
                       "Liverpool confirm winger Michael Olise suffers knee injury")
        ok, why = ev.same_event(a, b)
        self.assertFalse(ok)
        self.assertIn("事件类型不同", why)

    def test_same_player_same_event_is_merged(self):
        """同一球员、同一事件的不同措辞：必须能认出来。"""
        a = self.build("https://www.bbc.co.uk/a", OLISE_TITLE)
        b = self.build(LFC, "Liverpool agree deal to sign winger Michael Olise from Crystal Palace")
        ok, why = ev.same_event(a, b)
        self.assertTrue(ok, why)

    def test_person_names_extraction(self):
        self.assertEqual(ev.person_names(OLISE_TITLE), {"michael", "olise"})
        self.assertEqual(ev.person_names("Chelsea sack manager Mauricio Pochettino"),
                         {"mauricio", "pochettino"})
        # 普通标题不得抽出假人名
        self.assertEqual(ev.person_names("Arsenal set to sign striker in January"), set())
        self.assertEqual(ev.person_names("Arsenal lose appeal against Dowman red card"), set())

    def test_official_confirmation_requires_shared_key_fact(self):
        """标题像但没有可对齐的关键事实 → 不予官方确认。"""
        a = self.build("https://www.bbc.co.uk/a", "Arsenal agree deal for striker as transfer talks advance")
        b = self.build("https://www.arsenal.com/b", "Arsenal agree deal for striker after transfer talks")
        ok, why = ev.official_confirmation_ok(a, b)
        self.assertTrue(ok, why)
        self.assertIn("关键事实一致", why)


class TestTask4ConflictIgnoresSourceCount(unittest.TestCase):
    """任务 4：已验证的事实冲突不得因为来源少而被忽略。"""

    def test_single_official_source_with_internal_conflict(self):
        records = [{"url": FA2, "title": "FA statement on disciplinary matter"}]
        claims = [
            {"claim_key": "appeal_upheld", "stance": "supports", "source": "thefa.com"},
            {"claim_key": "appeal_upheld", "stance": "denies", "source": "thefa.com"},
        ]
        verdict = ev.evaluate(FA2, records, source_claims=claims)
        self.assertEqual(verdict["status"], ev.CONFLICTING)
        self.assertNotEqual(verdict["status"], ev.OFFICIAL_CONFIRMED)
        self.assertFalse(ev.auto_push_allowed(verdict)[0])

    def test_conflict_beats_multi_source(self):
        records = [
            {"url": BBC2, "title": "Arsenal set to sign striker in January"},
            {"url": SKY2, "title": "Arsenal rule out January striker signing"},
        ]
        claims = [
            {"claim_key": "sign_striker", "stance": "supports", "source": "bbc.co.uk"},
            {"claim_key": "sign_striker", "stance": "denies", "source": "skysports.com"},
        ]
        verdict = ev.evaluate(BBC2, records, source_claims=claims)
        self.assertEqual(verdict["status"], ev.CONFLICTING)
        self.assertFalse(ev.auto_push_allowed(verdict)[0])

    def test_model_reported_conflict_alone_blocks_push(self):
        """模型自报冲突只是线索：程序无法确证 → 保守拒绝自动推送。"""
        verdict = ev.evaluate(FA2, [{"url": FA2, "title": "FA statement on disciplinary matter"}],
                              model_conflict_flag=True)
        self.assertNotEqual(verdict["status"], ev.OFFICIAL_CONFIRMED)
        self.assertFalse(ev.auto_push_allowed(verdict)[0])
        self.assertIn("保守拒绝", verdict["reason"])

    def test_detect_conflict_reports_source_of_conflict(self):
        self.assertEqual(ev.detect_conflict([], False), (False, "", ""))
        conflict, _reason, kind = ev.detect_conflict([], True)
        self.assertTrue(conflict)
        self.assertEqual(kind, "model")
        conflict2, _r2, kind2 = ev.detect_conflict([
            {"claim_key": "k", "stance": "supports"},
            {"claim_key": "k", "stance": "denies"},
        ], False)
        self.assertTrue(conflict2)
        self.assertEqual(kind2, "claims")


class TestP0_3ScoreAndCoordinateNames(unittest.TestCase):
    """V1.3 P0-3：same_event 必须认出「比分差异」和「并列人名」。

    审计实测发现两处漏判，后果都是同一个：把两条互不相关的报道算成
    「同一事件的 2 个独立来源」，把 SINGLE_SOURCE 直接抬成多源印证并自动推送。
    多源虚高比误拦严重——误拦只是少发，虚高是把错的发出去。
    """

    def build(self, url, title):
        return ev.build_records([{"url": url, "title": title}])[0]

    # ---------- 比分必须算关键数字 ----------
    def test_score_is_a_key_number(self):
        self.assertEqual(ev._key_numbers("Arsenal beat Chelsea 2-1 at Emirates"),
                         {"score:2-1"})

    def test_formation_is_not_a_score(self):
        """4-3-3 是阵型，不是比分。"""
        self.assertEqual(ev._key_numbers("Arsenal switch to 4-3-3 formation"), set())

    def test_season_range_is_not_a_score(self):
        """2024-25 是赛季区间，不是比分。"""
        self.assertEqual(ev._key_numbers("Premier League 2024-25 season review"), set())

    def test_chinese_score_is_a_key_number(self):
        self.assertEqual(ev._key_numbers("阿森纳2比1切尔西"), {"score:2-1"})

    def test_different_score_is_different_event(self):
        a = self.build(BBC2, "Arsenal beat Chelsea 2-1 at Emirates")
        b = self.build(SKY2, "Arsenal beat Chelsea 3-0 at Emirates")
        ok, why = ev.same_event(a, b)
        self.assertFalse(ok, "两场不同比分的比赛不得判为同一事件")
        self.assertIn("关键数字不一致", why)

    def test_same_score_is_same_event(self):
        a = self.build(BBC2, "Arsenal beat Chelsea 2-1 at Emirates")
        b = self.build(SKY2, "Arsenal beat Chelsea 2-1")
        ok, why = ev.same_event(a, b)
        self.assertTrue(ok, why)

    def test_different_score_does_not_count_as_independent_sources(self):
        """多源虚高的复现：两条不同比赛必须只剩 1 个来源。"""
        a = self.build(BBC2, "Arsenal beat Chelsea 2-1 at Emirates")
        b = self.build(SKY2, "Arsenal beat Chelsea 3-0 at Emirates")
        self.assertEqual(len(ev.filter_related([a, b], a)), 1)

    # ---------- 并列人名 / 无角色词的人名 ----------
    def test_coordinate_names_are_extracted(self):
        self.assertEqual(ev.person_names("Saka and Martinelli limp off"),
                         {"saka", "martinelli"})

    def test_comma_list_names_are_extracted(self):
        self.assertIn("havertz", ev.person_names("Saka, Martinelli and Havertz limp off"))

    def test_single_name_before_person_verb_is_extracted(self):
        self.assertEqual(ev.person_names("Havertz limps off"), {"havertz"})

    def test_venue_is_not_a_person(self):
        """球场名同样是首字母大写，绝不能被当成人名——那会让同一场比赛
        的两篇报道互相判成「人物主体不同」。"""
        self.assertEqual(ev.person_names("Arsenal beat Chelsea at Emirates"), set())
        self.assertEqual(ev.person_names("Arsenal win at Stamford Bridge"), set())

    def test_different_injured_players_are_different_events(self):
        a = self.build(BBC2, "Arsenal injury blow as Saka and Martinelli limp off")
        b = self.build(SKY2, "Arsenal injury blow as Havertz limps off")
        ok, why = ev.same_event(a, b)
        self.assertFalse(ok, "同一支球队、不同球员的伤情不得判为同一事件")
        self.assertIn("人物主体不同", why)
        self.assertEqual(len(ev.filter_related([a, b], a)), 1)

    def test_same_injured_players_same_event(self):
        a = self.build(BBC2, "Arsenal injury blow as Saka and Martinelli limp off")
        b = self.build(SKY2, "Arsenal blow as Saka and Martinelli limp off")
        ok, why = ev.same_event(a, b)
        self.assertTrue(ok, why)

    def test_one_sided_missing_person_stays_permissive(self):
        """只有一边抽得出人名时保持宽松：宁可少拦，也不能把真来源拦掉。"""
        a = self.build(BBC2, "Arsenal injury blow as Saka and Martinelli limp off")
        b = self.build(SKY2, "Arsenal injury blow as two players limp off")
        ok, _why = ev.same_event(a, b)
        self.assertTrue(ok)

    def test_existing_negative_name_cases_unchanged(self):
        """原来「不得抽出假人名」的断言不能被放松。"""
        self.assertEqual(ev.person_names("Arsenal set to sign striker in January"), set())
        self.assertEqual(ev.person_names("Arsenal lose appeal against Dowman red card"), set())

    def test_club_abbreviation_is_not_a_person(self):
        """Man Utd 不是人名。

        如果两边都抽到 man / utd，人物比对就恒成立，「Man Utd 签 Gordon」与
        「Man Utd 签 Olise」会被当成同一笔转会——这是 P0-3 检查被中和的典型路径。
        """
        self.assertEqual(ev.person_names("Man Utd agree deal to sign winger Anthony Gordon"),
                         {"anthony", "gordon"})
        a = self.build(BBC2, "Man Utd agree deal to sign winger Anthony Gordon")
        b = self.build(SKY2, "Man Utd agree deal to sign winger Michael Olise")
        ok, why = ev.same_event(a, b)
        self.assertFalse(ok, "同一俱乐部、不同球员的转会不得判为同一事件")
        self.assertIn("人物主体不同", why)

    # ---------- 端到端 ----------
    def test_different_injury_stays_single_source(self):
        records = [
            {"url": BBC2, "title": "Arsenal injury blow as Saka and Martinelli limp off"},
            {"url": SKY2, "title": "Arsenal injury blow as Havertz limps off"},
        ]
        verdict = ev.evaluate(BBC2, records)
        self.assertEqual(verdict["status"], ev.SINGLE_SOURCE)
        self.assertNotEqual(verdict["status"], ev.MULTIPLE_INDEPENDENT_SOURCES)

    def test_same_injury_becomes_multi_source(self):
        """同一伤情的两篇报道仍必须能拿到多源印证，修复不能变成一律拦死。

        这里刻意用两种不同措辞（相似度 0.44，高于相关线 0.30、低于转载线 0.80），
        避免被当成「同一篇稿子转载」而只算 1 个来源。
        """
        records = [
            {"url": BBC2, "title": "Arsenal injury blow as Saka and Martinelli limp off"},
            {"url": SKY2, "title": "Arsenal sweat on Saka and Martinelli after both limp off"},
        ]
        verdict = ev.evaluate(BBC2, records)
        self.assertEqual(verdict["status"], ev.MULTIPLE_INDEPENDENT_SOURCES)
        self.assertTrue(ev.auto_push_allowed(verdict)[0])


class TestP0_4ProgramSideClaims(unittest.TestCase):
    """V1.3 P0-4：冲突必须由程序从记录里重建，模型 claim 只作线索。

    以前 detect_conflict 只读模型给的 source_claims，等于让模型单方面决定
    「这条新闻算不算冲突」。模型既可以编造冲突把正常新闻拦死，
    也可以漏报冲突让矛盾内容发出去。现在改成程序先重建、模型只作线索。
    """

    AFFIRM = "Arsenal set to sign striker in January"
    DENY = "Arsenal rule out January striker signing"

    def test_stance_of(self):
        self.assertIs(ev._stance_of(self.AFFIRM), True)
        self.assertIs(ev._stance_of(self.DENY), False)
        # 既没肯定也没否定的标题不得参与冲突判定
        self.assertIsNone(ev._stance_of("Arsenal transfer news roundup"))
        self.assertIsNone(ev._stance_of(""))

    def test_rebuild_claims_shape(self):
        records = ev.build_records([
            {"url": BBC2, "title": self.AFFIRM},
            {"url": SKY2, "title": self.DENY},
        ])
        claims = ev.rebuild_claims(records)
        self.assertEqual(len(claims), 2)
        self.assertEqual({c["claim_key"] for c in claims}, {"arsenal#TRANSFER"})
        self.assertEqual({c["stance"] for c in claims}, {"supports", "denies"})

    def test_rebuild_claims_skips_titles_without_subject_or_type(self):
        records = ev.build_records([
            {"url": BBC2, "title": "Premier League announce fixture changes"},
        ])
        self.assertEqual(ev.rebuild_claims(records), [])

    def test_program_detects_conflict_without_any_model_claim(self):
        """关键能力：模型一个字都没说，程序也能自己发现冲突。"""
        records = [
            {"url": BBC2, "title": self.AFFIRM},
            {"url": SKY2, "title": self.DENY},
        ]
        verdict = ev.evaluate(BBC2, records)          # 注意：source_claims 为空
        self.assertEqual(verdict["status"], ev.CONFLICTING)
        self.assertIn("程序核验", verdict["reason"])
        self.assertFalse(ev.auto_push_allowed(verdict)[0])

    def test_program_conflict_needs_two_different_urls(self):
        """同一篇稿子内部出现相反表述不算「两个来源打架」。"""
        records = [
            {"url": BBC2, "title": self.AFFIRM},
            {"url": BBC2, "title": self.DENY},
        ]
        conflict, _reason, kind = ev.detect_conflict(None, False, records=records)
        self.assertFalse(conflict, f"不应判冲突，实际 kind={kind}")

    def test_cross_source_model_claim_is_only_a_clue(self):
        """跨来源的模型 claim，程序核不实 → 不得判 CONFLICTING。"""
        records = [
            {"url": BBC2, "title": "Arsenal transfer news roundup"},
            {"url": SKY2, "title": "Arsenal transfer news latest"},
        ]
        claims = [
            {"claim_key": "sign_striker", "stance": "supports", "source": "bbc.co.uk"},
            {"claim_key": "sign_striker", "stance": "denies", "source": "skysports.com"},
        ]
        verdict = ev.evaluate(BBC2, records, source_claims=claims)
        self.assertNotEqual(verdict["status"], ev.CONFLICTING)
        self.assertEqual(verdict["status"], ev.UNVERIFIED)
        self.assertIn("保守拒绝", verdict["reason"])
        self.assertFalse(ev.auto_push_allowed(verdict)[0])

    def test_same_source_self_contradiction_still_hard_blocks(self):
        """同一来源自相矛盾：证据链本身不可用，仍按硬冲突处理。"""
        claims = [
            {"claim_key": "appeal_upheld", "stance": "supports", "source": "thefa.com"},
            {"claim_key": "appeal_upheld", "stance": "denies", "source": "thefa.com"},
        ]
        conflict, reason, kind = ev.detect_conflict(claims, False)
        self.assertTrue(conflict)
        self.assertEqual(kind, "claims")
        self.assertIn("同一来源", reason)

    def test_detect_conflict_kinds(self):
        self.assertEqual(ev.detect_conflict([], False), (False, "", ""))
        _c, _r, kind = ev.detect_conflict([], True)
        self.assertEqual(kind, "model")
        _c2, _r2, kind2 = ev.detect_conflict(None, False, records=[
            {"url": BBC2, "title": self.AFFIRM},
            {"url": SKY2, "title": self.DENY},
        ])
        self.assertEqual(kind2, "program")


if __name__ == "__main__":
    unittest.main(verbosity=2)
