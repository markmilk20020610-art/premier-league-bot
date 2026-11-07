"""V1.0 P0-1 验收：商品页 vs 解雇新闻 对抗测试。

真实故障（2026-11-04 快照）：
    "Arsenal Kids Christmas Sack - Arsenal Direct"
被判成 APPOINTMENT → CLUB_GOVERNANCE → A 级 A 线，以 OFFICIAL_CONFIRMED
身份进了早报。一条圣诞袜商品页占了俱乐部治理新闻的名额。

**本文件测的是完整链路，不是单点 classify：**

    evidence.primary_event_type → content_type.classify
        → event_aggregate.build_events → bot._select_morning_events

理由：P0-1 的缺陷横跨三层。只断言 classify 的 content_type，测不出
「商品页被事件类型映射定型成 CLUB_GOVERNANCE，再靠 C 级封顶」这类问题——
封顶只影响排序，`_select_morning_events` 的低价值剔除才是真正的闸门。
所以每条用例都断言**最终有没有进早报**。

反向同样要守：真实的解雇 / 所有权 / 赞助 / 破产新闻不得被商品规则误杀。
两端都过才叫修好，只堵一头只是把误报换成了漏报。
"""
import hashlib
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bot
import content_type as ct
import event_aggregate as evagg
import evidence as ev
import importance as imp

LEGAL_SCOPES = (imp.A_LINE, imp.GLOBAL_EVENT_LINE)


def chain(title, desc="", domain="bbc.co.uk"):
    """跑完整链路，返回 P0-1 要求检查的全部字段。

    `in_morning` 的含义要说准：它是**名额分配层**的结论，即
    `bot._select_morning_events` 有没有给这个事件分配早报位置
    （低价值整体剔除 + S/A/B/C 优先级排序）。这正是 P0-1 出问题的那一层——
    圣诞袜当时就是被分配到了 A 线名额。

    下游还有两道与本缺陷无关的独立闸门：领域范围（`_sport_check`）与
    证据核验（`ev.evaluate`）。正例里 "Club sacks head coach after poor
    results" 这类通用队名标题过不了领域闸门——那是标题本身没写队名的
    测试产物，不是分类缺陷。所以这里断言名额层，不把领域闸门混进来。

    链接用标题哈希生成，保证同一标题每次跑出来的 URL 一致（`hash()`
    受 PYTHONHASHSEED 影响，不能用），也不会与其他用例的 canonical 撞车。
    """
    digest = hashlib.md5(title.encode("utf-8")).hexdigest()[:10]
    item = {
        "title": title,
        "desc": desc,
        "link": "https://{}/p/{}".format(domain, digest),
        "publisher": domain,
        "feed_origin": "https://{}/rss".format(domain),
        "published": "2026-11-04T20:00:00Z",
    }
    events, _stats = evagg.build_events([item])
    assert len(events) == 1, "单条素材必须聚成一个事件：{!r}".format(title)
    event = events[0]

    selected, _report = bot._select_morning_events(events)

    verified, excluded = bot._build_morning_events([item])
    status = None
    if verified:
        status = verified[0]["status"]
    elif excluded:
        status = excluded[0]["status"]

    return {
        "content_type": event["content_type"],
        "event_type": event["event_type"],
        "importance": event["importance"],
        "scope": event["scope"],
        "in_morning": bool(selected),
        "evidence_status": status,
    }


class TestMerchNegative(unittest.TestCase):
    """商品反例：必须 PROMOTION、必须 C 级、必须不进早报。"""

    def assert_merch(self, title, rule=None, domain="bbc.co.uk"):
        got = chain(title, domain=domain)
        msg = "{!r} → {}".format(title, got)
        self.assertEqual(got["content_type"], ct.PROMOTION, msg)
        self.assertEqual(got["importance"], imp.C, msg)
        self.assertTrue(ct.is_low_value(got["content_type"]), msg)
        # 商品页不得被识别成任何事件类型——尤其不得是 APPOINTMENT，
        # 那是 P0-1 的原始病因。
        self.assertEqual(got["event_type"], "", msg)
        # scope 对商品页没有业务含义（整条事件被剔除，不参与名额分配），
        # 但必须是合法枚举，不能出现未定义值。
        self.assertIn(got["scope"], LEGAL_SCOPES, msg)
        self.assertFalse(got["in_morning"], "商品页不得进入早报：{}".format(msg))
        if rule:
            r = ct.classify(title, "", event_type=ev.primary_event_type(title))
            self.assertEqual(r["rule_id"], rule, msg)
        return got

    def test_christmas_sack_club_store_suffix(self):
        """P0-1 原始故障样本：俱乐部官方商店后缀。"""
        self.assert_merch("Arsenal Kids Christmas Sack - Arsenal Direct",
                          rule="PROMOTION_MERCH_PAGE")

    def test_santa_hat(self):
        self.assert_merch("Arsenal Santa Hat", rule="PROMOTION_MERCH_PAGE")

    def test_christmas_decoration(self):
        self.assert_merch("Manchester United Christmas Decoration",
                          rule="PROMOTION_MERCH_PAGE")

    def test_official_store_plaque(self):
        self.assert_merch("Chelsea Official Store Plaque",
                          rule="PROMOTION_MERCH_PAGE")

    def test_club_shop_varsity_jacket(self):
        self.assert_merch("Club Shop Varsity Jacket", rule="PROMOTION_MERCH_PAGE")

    def test_home_kit(self):
        self.assert_merch("Arsenal Home Kit")

    def test_merch_christmas_collection(self):
        self.assert_merch("Club merchandise Christmas collection")

    def test_sack_never_becomes_governance(self):
        """直接钉住 P0-1：圣诞袜不得是 CLUB_GOVERNANCE，也不得是 A 级。"""
        got = chain("Arsenal Kids Christmas Sack - Arsenal Direct")
        self.assertNotEqual(got["content_type"], ct.CLUB_GOVERNANCE, got)
        self.assertNotEqual(got["importance"], imp.A, got)
        self.assertNotEqual(got["importance"], imp.S, got)


class TestRealSnapshotMerch(unittest.TestCase):
    """2026-11-04 真实快照里的六条 Arsenal Direct 商品页。

    这六条在修复前是各自独立的事件，其中 Christmas Sack 被定型成
    A 级治理新闻。修复后六条聚成**一个** PROMOTION 事件并被整体剔除。
    标题按快照原文硬编码，测试不依赖本地快照文件。
    """

    REAL = [
        "Arsenal Wooden Crest Plaque Decoration - Arsenal Direct",
        "Arsenal Kids Christmas Santa Hat - Arsenal Direct",
        "Arsenal Christmas Classic Santa Hat - Arsenal Direct",
        "Arsenal Kids Christmas Sack - Arsenal Direct",
        "Arsenal Wooden Victoria Concordia Crescit Plaque Decoration - Arsenal Direct",
        "Arsenal Champions Bus Christmas Decoration - Arsenal Direct",
    ]

    def test_all_real_merch_pages_are_promotion(self):
        for title in self.REAL:
            with self.subTest(title=title):
                got = chain(title, domain="arsenaldirect.arsenal.com")
                self.assertEqual(got["content_type"], ct.PROMOTION, got)
                self.assertFalse(got["in_morning"], got)

    def test_real_merch_cluster_is_one_low_value_event(self):
        """六条商品页聚成一个事件：早报名额只被占用 0 次，不是 6 次。"""
        items = [{
            "title": t,
            "desc": "",
            "link": "https://arsenaldirect.arsenal.com/p/{}".format(i),
            "publisher": "arsenaldirect.arsenal.com",
            "feed_origin": "https://arsenaldirect.arsenal.com/rss",
            "published": "2026-11-04T18:00:00Z",
        } for i, t in enumerate(self.REAL)]
        events, _stats = evagg.build_events(items)
        self.assertEqual(len(events), 1, "六条商品页应聚成一个事件")
        event = events[0]
        self.assertEqual(event["content_type"], ct.PROMOTION)
        self.assertEqual(event["importance"], imp.C)
        selected, report = bot._select_morning_events(events)
        self.assertEqual(selected, [], "商品事件不得入选早报")
        self.assertGreaterEqual(report["low_value_excluded"], 1)


class TestNewsPositive(unittest.TestCase):
    """真实新闻正例：不得被商品规则误杀，必须能进早报。"""

    def assert_news(self, title, content_type, event_type, importance, scope):
        got = chain(title)
        msg = "{!r} → {}".format(title, got)
        self.assertEqual(got["content_type"], content_type, msg)
        self.assertEqual(got["event_type"], event_type, msg)
        self.assertEqual(got["importance"], importance, msg)
        self.assertEqual(got["scope"], scope, msg)
        self.assertFalse(ct.is_low_value(got["content_type"]), msg)
        self.assertTrue(got["in_morning"], "真实新闻必须能进早报：{}".format(msg))

    def test_west_ham_sack_lopetegui(self):
        self.assert_news("West Ham sack Julen Lopetegui",
                         ct.CLUB_GOVERNANCE, "APPOINTMENT", imp.A, imp.A_LINE)

    def test_man_utd_sack_ten_hag(self):
        self.assert_news("Manchester United sack Erik ten Hag",
                         ct.CLUB_GOVERNANCE, "APPOINTMENT", imp.A, imp.A_LINE)

    def test_sacks_head_coach_after_poor_results(self):
        self.assert_news("Club sacks head coach after poor results",
                         ct.CLUB_GOVERNANCE, "APPOINTMENT", imp.B,
                         imp.GLOBAL_EVENT_LINE)

    def test_confirms_manager_departure(self):
        """departure 在管理层语境下必须算换帅，不能被当成普通离队。"""
        self.assert_news("Club confirms manager departure",
                         ct.CLUB_GOVERNANCE, "APPOINTMENT", imp.B,
                         imp.GLOBAL_EVENT_LINE)

    def test_parts_company_with_head_coach(self):
        """part company with 分词取不到，靠短语补计。"""
        self.assert_news("Club parts company with head coach",
                         ct.CLUB_GOVERNANCE, "APPOINTMENT", imp.B,
                         imp.GLOBAL_EVENT_LINE)

    def test_ownership_change(self):
        self.assert_news("Club owners announce major ownership change",
                         ct.CLUB_GOVERNANCE, "CLUB_OWNERSHIP", imp.B,
                         imp.GLOBAL_EVENT_LINE)

    def test_new_sponsorship_agreement(self):
        """含 sponsorship 这类商业词，但属于结构性商业新闻，不是推广。"""
        self.assert_news("Club announces major new sponsorship agreement",
                         ct.FINANCIAL_NEWS, "SPONSORSHIP", imp.B,
                         imp.GLOBAL_EVENT_LINE)

    def test_enters_administration(self):
        self.assert_news("Club enters administration",
                         ct.CLUB_GOVERNANCE, "CLUB_OWNERSHIP", imp.B,
                         imp.GLOBAL_EVENT_LINE)


class TestNoFalseKill(unittest.TestCase):
    """商品规则的护栏：商品名词出现在真新闻里，不得被当成商品页。"""

    def test_plaque_unveiled_is_not_promotion(self):
        """plaque 是商品名词，但「揭幕纪念牌」是新闻。"""
        got = chain("Plaque unveiled at stadium to honour former captain")
        self.assertNotEqual(got["content_type"], ct.PROMOTION, got)
        self.assertTrue(got["in_morning"], got)

    def test_player_departure_not_appointment(self):
        """球员离队不是主教练解雇：departure 必须看管理层语境。"""
        got = chain("Player departure confirmed as winger joins rivals")
        self.assertNotEqual(got["event_type"], "APPOINTMENT", got)
        self.assertNotEqual(got["content_type"], ct.CLUB_GOVERNANCE, got)

    def test_steward_dismissal_not_appointment(self):
        """Steward 不是主教练，dismissal 不得触发 APPOINTMENT。"""
        got = chain("St Johnstone fight back from Steward dismissal to take points")
        self.assertNotEqual(got["event_type"], "APPOINTMENT", got)

    def test_in_charge_is_not_charge(self):
        """in charge 是「掌管」，不是 DISCIPLINE 的 charge（假朋友归一化）。"""
        got = chain("Lopetegui leaves West Ham after seven months in charge")
        self.assertEqual(got["event_type"], "APPOINTMENT", got)
        self.assertEqual(got["content_type"], ct.CLUB_GOVERNANCE, got)
        self.assertEqual(got["importance"], imp.A, got)

    def test_sack_beats_derby_defeat(self):
        """任免动词必须压过 derby / defeat 这类场景词。"""
        got = chain("Everton sack Sean Dyche after derby defeat")
        self.assertEqual(got["event_type"], "APPOINTMENT", got)
        self.assertEqual(got["content_type"], ct.CLUB_GOVERNANCE, got)
        self.assertEqual(got["importance"], imp.A, got)

    def test_contract_extension_not_appointment(self):
        got = chain("New contract: winger signs until 2029")
        self.assertNotEqual(got["event_type"], "APPOINTMENT", got)

    def test_dismisses_furore_is_not_sacking(self):
        """`dismiss` 的第二个义项是「驳回/淡化」。

        「Arteta dismisses Isak penalty furore」是淡化点球争议，不是解雇。
        实测这条把一整个 Arsenal 赛前事件从 COMPETITIVE_NEWS 拉成了
        CLUB_GOVERNANCE。判别方式：没有职位词、且 dismiss 的宾语是抽象名词
        （furore / claims / talk / speculation…）时不算任免。
        """
        got = chain("Arteta dismisses Isak penalty furore: "
                    "I have massive library we can talk about!")
        self.assertNotEqual(got["event_type"], "APPOINTMENT", got)
        self.assertNotEqual(got["content_type"], ct.CLUB_GOVERNANCE, got)

    def test_dismissed_head_coach_is_sacking(self):
        """反过来：有职位词就是任免语境，不能被抽象名词规则误伤。"""
        got = chain("Club dismissed head coach after claims of misconduct")
        self.assertEqual(got["event_type"], "APPOINTMENT", got)


class TestOfficialConfirmedIndependent(unittest.TestCase):
    """来源可信度与编辑价值必须独立。

    OFFICIAL_CONFIRMED 只说明「这条来自官方、可以当已确认事实用」，
    不说明「这条值得占早报名额」。俱乐部官网发的商品页恰恰是最典型的
    「官方确认的广告」——可信度满分，编辑价值为零。
    """

    def test_official_merch_stays_promotion(self):
        got = chain("Arsenal Kids Christmas Sack - Arsenal Direct",
                    domain="arsenal.com")
        self.assertEqual(got["evidence_status"], ev.OFFICIAL_CONFIRMED, got)
        self.assertEqual(got["content_type"], ct.PROMOTION, got)
        self.assertEqual(got["importance"], imp.C, got)
        self.assertFalse(got["in_morning"], got)

    def test_official_news_still_goes_out(self):
        """同一个官方域名下，真新闻照样进早报——不能一刀切屏蔽官方源。"""
        got = chain("Club sacks head coach after poor results", domain="arsenal.com")
        self.assertEqual(got["content_type"], ct.CLUB_GOVERNANCE, got)
        self.assertTrue(got["in_morning"], got)


if __name__ == "__main__":
    unittest.main(verbosity=2)
