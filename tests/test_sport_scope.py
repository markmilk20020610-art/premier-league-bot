"""足球领域边界回归测试（V1.3 §F1 专项审计 A–E + R4）。

覆盖审计要求的 10 条用例，另加来源登记表自检与采集层过滤。
全部使用模拟数据，不联网、不发任何 Telegram 消息。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bot  # noqa: E402
import evidence as ev  # noqa: E402
import feed_registry as feeds  # noqa: E402
import sport_scope as ss  # noqa: E402

# ---- 真实形态的样本（URL 路径与标题都取自生产快照）----
SKY_F1 = ("https://www.skysports.com/f1/news/12040/13605836/"
          "max-verstappen-liam-lawson-receive-grid-penalties-for-unacceptable-"
          "practice-collision-at-mexico-city-grand-prix")
SKY_F1_LIVE = ("https://www.skysports.com/f1/live-blog/12040/13605426/"
               "f1-mexico-city-gp-live-friday-practice-updates-results-stream-"
               "highlights-from-formula-1-race-weekend")
SKY_F1_TITLE = ("Verstappen, Lawson receive grid penalties after "
                "'unacceptable' practice collision")
SKY_F1_LIVE_TITLE = ("Mexico City GP: Verstappen, Lawson summoned by stewards "
                     "after collision LIVE!")

SKY_FB = "https://www.skysports.com/football/news/11095/12345/fernandes-restored"
SKY_FB_TITLE = ("Fernandes restored to Man Utd line-up as Estevao starts for "
                "Chelsea LIVE!")

BBC_F1 = "https://www.bbc.co.uk/sport/formula1/articles/c123456"
BBC_F1_TITLE = "Verstappen takes pole for Mexico City Grand Prix"
BBC_FB = "https://www.bbc.co.uk/sport/football/articles/c999"
BBC_FB_TITLE = "Arsenal lose appeal against Dowman red card"

FIFA_FB = "https://www.fifa.com/en/tournaments/mens/worldcup/news/draw"
FIFA_FB_TITLE = "FIFA confirms World Cup qualifying draw dates"
FIA_F1 = "https://www.fia.com/news/fia-formula-1-world-championship-stewards"
FIA_F1_TITLE = "FIA Formula 1 World Championship: stewards decision published"

SKY_FEED = "https://www.skysports.com/rss/12040"      # 综合体育源
BBC_FEED = "https://feeds.bbci.co.uk/sport/football/rss.xml"   # 足球专属源


def scan_payload(**over):
    """构造一份可通过 Schema 的扫描结果，用于走真实 _finalize_result。"""
    data = {
        "has_valid_news": True,
        "var_rating": "✨【金哨认证·绝对真理】(Tier 1)",
        "var_reason": "天空体育报道",
        "event_title": "维斯塔潘与劳森练习赛碰撞双双被罚退后一位发车",
        "event_time": "2小时前",
        "fact_summary": "两人在墨西哥城大奖赛二练发生碰撞，各被罚退后一位发车。",
        "evidence_url": SKY_F1,
        "draft_a": "你站哪一边？这次判罚到底公不公平？",
        "draft_b": "天空体育与 FIA 公告均已确认这一判罚。",
        "draft_c": "从竞赛规则看，练习赛碰撞的判罚标准与正赛不同。",
        "cover_blueprint": "深色底 + 赛车剪影",
        "image_prompt": "3d caricature style two race cars colliding",
        "banner_overlay_text": "双双被罚",
        "grounding_records": [
            {"url": SKY_F1, "title": SKY_F1_TITLE},
            {"url": SKY_F1_LIVE, "title": SKY_F1_LIVE_TITLE},
        ],
    }
    data.update(over)
    return data


# ==================== A. 领域白名单 ====================
class TestDomainWhitelist(unittest.TestCase):
    """只有 Football 允许进入足球流程；Sports ≠ Football。"""

    def test_only_football_is_allowed(self):
        self.assertEqual(ss.ALLOWED_CATEGORIES, frozenset({ss.FOOTBALL}))
        for cat in ss.ALL_CATEGORIES:
            if cat == ss.FOOTBALL:
                self.assertTrue(ss.is_football(cat))
            else:
                self.assertFalse(ss.is_football(cat), f"{cat} 不得被视为足球")

    def test_unknown_is_not_football(self):
        """判不出领域 ≠ 足球。默认拒绝，不是默认放行。"""
        verdict = ss.classify_sport("Club statement on ticket allocation",
                                    "", "https://example.com/news/2")
        self.assertEqual(verdict.category, ss.UNKNOWN)
        self.assertFalse(verdict.allowed)


# ==================== 审计要求的 10 条用例 ====================
class TestAuditCases(unittest.TestCase):
    """审计 §五 列出的 10 条回归用例，逐条对应。"""

    def test_1_sky_sports_f1_rejected(self):
        verdict = ss.classify_sport(SKY_F1_TITLE, "", SKY_F1)
        self.assertFalse(verdict.allowed, "Sky Sports 的 F1 新闻必须拒绝")
        self.assertEqual(verdict.category, ss.FORMULA1)

    def test_2_sky_sports_football_allowed(self):
        verdict = ss.classify_sport(SKY_FB_TITLE, "", SKY_FB)
        self.assertTrue(verdict.allowed, "Sky Sports 的足球新闻必须放行")
        self.assertEqual(verdict.category, ss.FOOTBALL)

    def test_3_bbc_f1_rejected(self):
        verdict = ss.classify_sport(BBC_F1_TITLE, "", BBC_F1)
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.category, ss.FORMULA1)

    def test_4_bbc_football_allowed(self):
        verdict = ss.classify_sport(BBC_FB_TITLE, "", BBC_FB)
        self.assertTrue(verdict.allowed)
        self.assertEqual(verdict.category, ss.FOOTBALL)

    def test_5_fifa_football_allowed(self):
        verdict = ss.classify_sport(FIFA_FB_TITLE, "", FIFA_FB)
        self.assertTrue(verdict.allowed, "FIFA 官方足球新闻必须放行")
        self.assertEqual(verdict.category, ss.FOOTBALL)

    def test_6_fia_f1_rejected(self):
        """官方域名不等于足球：FIA 是赛车管理机构。"""
        verdict = ss.classify_sport(FIA_F1_TITLE, "", FIA_F1)
        self.assertFalse(verdict.allowed, "FIA 的 F1 新闻必须拒绝")
        self.assertEqual(verdict.category, ss.FORMULA1)

    def test_7_club_name_plus_f1_keyword_is_conflict(self):
        """标题同时出现足球俱乐部与 F1 关键词 → 不能凭俱乐部名放行。"""
        verdict = ss.classify_sport(
            "Arsenal owners attend Mexico City Grand Prix as Verstappen wins",
            "", "https://example.com/sport/story/1")
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.category, ss.CONFLICT,
                         "足球与非足球同时命中必须判冲突，而不是判足球")

    def test_8_ai_claiming_football_cannot_bypass(self):
        """AI 把 F1 新闻标成 Football，最终推送仍然拒绝。"""
        data = scan_payload(sport="Football")     # 模型错误申报
        ok, finalized, reason, code = bot._finalize_result(data, [])
        self.assertFalse(ok, "模型自报 Football 不得改变程序判定")
        self.assertEqual(code, "OUT_OF_SCOPE_SPORT")
        self.assertEqual(finalized["sport_category"], ss.FORMULA1)

    def test_9_mixed_sport_feed_gets_no_football_whitelist(self):
        """综合体育 RSS 不自动获得足球白名单资格，必须逐条分类。"""
        self.assertEqual(feeds.scope_of(SKY_FEED), feeds.MIXED_SPORT)
        self.assertTrue(feeds.requires_item_check(SKY_FEED))
        self.assertEqual(feeds.scope_of(BBC_FEED), feeds.FOOTBALL_ONLY)
        self.assertFalse(feeds.requires_item_check(BBC_FEED))

        # 同一个综合体育源里，足球留下、F1 丢掉
        items = [
            {"title": SKY_FB_TITLE, "link": SKY_FB, "desc": ""},
            {"title": SKY_F1_TITLE, "link": SKY_F1, "desc": ""},
        ]
        kept = bot._filter_items_by_sport(items, SKY_FEED)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["title"], SKY_FB_TITLE)

    def test_10_unknown_or_conflict_rejected_at_push(self):
        for title, url, expect in (
            ("Club statement on ticket allocation", "https://example.com/news/2", ss.UNKNOWN),
            ("Arsenal owners attend Mexico City Grand Prix as Verstappen wins",
             "https://example.com/sport/story/1", ss.CONFLICT),
        ):
            ok, why, code = bot._sport_gate(title, "", url)
            self.assertFalse(ok, f"{expect} 必须拒绝自动推送")
            self.assertEqual(code, "OUT_OF_SCOPE_SPORT")
            self.assertIn(expect, why)


# ==================== B. 来源级过滤 ====================
class TestFeedRegistry(unittest.TestCase):
    def test_registry_is_valid(self):
        problems = feeds.validate_registry()
        self.assertEqual(problems, [], f"登记表自检未通过：{problems}")

    def test_every_rss_feed_is_registered(self):
        for url in bot.RSS_FEEDS:
            self.assertIsNotNone(feeds.entry_for(url), f"{url} 未登记却出现在采集列表")

    def test_unregistered_feed_is_dropped_entirely(self):
        """未登记来源不得采集——宁可少一个源，也不赌它是足球源。"""
        items = [{"title": SKY_FB_TITLE, "link": SKY_FB, "desc": ""}]
        kept = bot._filter_items_by_sport(items, "https://feed.test/rss")
        self.assertEqual(kept, [], "未登记的源必须整源丢弃")

    def test_football_only_feed_passes_through(self):
        items = [{"title": "Anything at all", "link": "https://example.com/x", "desc": ""}]
        kept = bot._filter_items_by_sport(items, BBC_FEED)
        self.assertEqual(len(kept), 1, "已核实为足球专属的源不做逐条分类")


# ==================== C. 新闻级过滤 ====================
class TestNewsLevelFilter(unittest.TestCase):
    def test_real_f1_titles_are_all_rejected(self):
        """生产快照里的 5 条 F1 标题必须全部判为非足球。"""
        cases = [
            (SKY_F1_TITLE, SKY_F1),
            ("Leclerc seals Mexico City practice double as Hamilton suffers "
             "technical issue",
             "https://www.skysports.com/f1/news/12040/13605831/leclerc"),
            ("Mexico City Grand Prix | Practice highlights",
             "https://www.skysports.com/watch/video/13606000/mexico-city-gp"),
            ("'Every weekend!' - Lawson fumes at Verstappen as pair clash during P2!",
             "https://www.skysports.com/watch/video/13605977/mexico-city-gp"),
            (SKY_F1_LIVE_TITLE, SKY_F1_LIVE),
        ]
        for title, url in cases:
            verdict = ss.classify_sport(title, "", url)
            self.assertFalse(verdict.allowed, f"未拦住：{title}")

    def test_real_football_titles_are_not_over_blocked(self):
        """反向保护：真实足球标题不能被误杀。

        每条都带**真实形态**的 URL——生产里不会有「无路径、无域名」的链接，
        用 example.com 测纯标题等于测一个不存在的场景。
        """
        cases = [
            ("Fernandes restored to Man Utd line-up as Estevao starts for Chelsea LIVE!",
             "https://www.skysports.com/football/news/11095/12345/fernandes"),
            ("Papers: Martinez seeking summer Man Utd departure",
             "https://www.skysports.com/football/news/11095/12346/papers"),
            ("Arteta warns Dowman: This isn't Disneyland",
             "https://www.arsenal.com/news/arteta-warns-dowman"),
            ("Well Done, FA: Football Is Finally Safe - Gunners Town",
             "https://www.arsenal.com/news/well-done-fa"),
            ("Lionel Messi to Newell's in 2027? Fabrizio Romano Reveals the Latest",
             "https://www.bbc.co.uk/sport/football/articles/c777"),
            ("What now for the City Football Group project?",
             "https://www.bbc.co.uk/sport/football/articles/c888"),
            # 纯文本路径也要能判出来（Google News 跳转链接就是这种形态）
            ("Arsenal lose appeal against Dowman red card",
             "https://news.google.com/rss/articles/CBMi123"),
        ]
        for title, url in cases:
            verdict = ss.classify_sport(title, "", url)
            self.assertTrue(verdict.allowed,
                            f"误杀足球新闻：{title} → {verdict.category}（{verdict.reason}）")


# ==================== D. 最终推送硬校验 ====================
class TestPushGate(unittest.TestCase):
    def test_sport_gate_blocks_f1(self):
        ok, why, code = bot._sport_gate(SKY_F1_TITLE, "", SKY_F1)
        self.assertFalse(ok)
        self.assertEqual(code, "OUT_OF_SCOPE_SPORT")
        self.assertIn("Formula 1", why)

    def test_sport_gate_allows_football(self):
        ok, why, code = bot._sport_gate(SKY_FB_TITLE, "", SKY_FB)
        self.assertTrue(ok)
        self.assertEqual((why, code), ("", ""))

    def test_scan_path_rejects_f1_even_with_multi_source(self):
        """即使证据判定为多源印证，领域闸门仍然先拦下。"""
        data = scan_payload()
        ok, finalized, reason, code = bot._finalize_result(
            data, [{"url": SKY_F1_LIVE, "title": SKY_F1_LIVE_TITLE}])
        self.assertFalse(ok)
        self.assertEqual(code, "OUT_OF_SCOPE_SPORT")
        # 领域与证据分开存储：领域被拒，但证据状态仍如实记录
        self.assertEqual(finalized["sport_category"], ss.FORMULA1)

    def test_football_still_passes_full_scan_gate(self):
        """回归保护：正常足球新闻走完整判定链仍然能通过。"""
        data = scan_payload(
            evidence_url=BBC_FB,
            event_title="阿森纳红牌申诉被驳回",
            fact_summary="英足总确认驳回阿森纳关于道曼红牌的申诉。",
            grounding_records=[
                {"url": BBC_FB, "title": BBC_FB_TITLE},
                {"url": SKY_FB, "title": "FA rejects Arsenal appeal over Dowman dismissal"},
            ],
        )
        ok, finalized, reason, code = bot._finalize_result(data, [])
        self.assertTrue(ok, f"足球新闻不该被拦：{code} {reason}")
        self.assertEqual(finalized["sport_category"], ss.FOOTBALL)

    def test_morning_path_blocks_f1(self):
        """早报是第二条通道，必须同样拦下。"""
        pool = [
            {"title": SKY_F1_TITLE, "link": SKY_F1, "desc": "",
             "source": SKY_FEED, "feed_origin": SKY_FEED},
        ]
        events, excluded = bot._build_morning_events(pool)
        self.assertEqual(events, [], "F1 不得进入早报候选")
        self.assertEqual(excluded[0]["gate_code"], "OUT_OF_SCOPE_SPORT")
        self.assertEqual(excluded[0]["sport_category"], ss.FORMULA1)

    def test_morning_path_allows_football(self):
        pool = [
            {"title": SKY_FB_TITLE, "link": SKY_FB, "desc": "",
             "source": SKY_FEED, "feed_origin": SKY_FEED},
        ]
        events, _excluded = bot._build_morning_events(pool)
        self.assertEqual(len(events), 1, "足球新闻必须仍能进入早报")


# ==================== E. 评级独立性 ====================
class TestRatingIndependence(unittest.TestCase):
    def test_non_football_cannot_reach_tier1(self):
        key, downgraded, why = ev.enforce_rating(
            "Tier 1", ev.MULTIPLE_INDEPENDENT_SOURCES, ["TIER1"], ss.FORMULA1)
        self.assertEqual(key, "Tier 3")
        self.assertTrue(downgraded)

    def test_football_multi_source_keeps_tier1(self):
        key, downgraded, _why = ev.enforce_rating(
            "Tier 1", ev.MULTIPLE_INDEPENDENT_SOURCES, ["TIER1"], ss.FOOTBALL)
        self.assertEqual(key, "Tier 1")
        self.assertFalse(downgraded)

    def test_authority_alone_does_not_grant_tier1(self):
        """只有一家一线权威媒体 → 最高 Tier 2。媒体知名度不等于事实等级。"""
        key, downgraded, _why = ev.enforce_rating(
            "Tier 1", ev.SINGLE_SOURCE, ["TIER1"], ss.FOOTBALL)
        self.assertEqual(key, "Tier 2")
        self.assertTrue(downgraded)

    def test_sport_category_is_stored_separately(self):
        rec = ev.build_records([{"url": SKY_F1, "title": SKY_F1_TITLE}])[0]
        self.assertEqual(rec["sport_category"], ss.FORMULA1)
        self.assertEqual(rec["source_class"], "TIER1")
        # 两个维度互不影响：权威度是 TIER1，领域是 Formula 1
        self.assertNotEqual(rec["source_class"], rec["sport_category"])


# ==================== R4. 同域假多源 ====================
class TestSameDomainNotIndependent(unittest.TestCase):
    """同一家媒体的两篇稿子不是两个独立来源（V1.3 §F1 审计 R4）。"""

    def test_two_sky_articles_about_f1_count_once(self):
        records = [
            {"url": SKY_F1, "title": SKY_F1_TITLE},
            {"url": SKY_F1_LIVE, "title": SKY_F1_LIVE_TITLE},
        ]
        verdict = ev.evaluate(SKY_F1, records)
        self.assertEqual(verdict["count"], 1,
                         "同一家媒体的 news 稿 + live-blog 不能算 2 个独立来源")
        self.assertNotEqual(verdict["status"], ev.MULTIPLE_INDEPENDENT_SOURCES)

    def test_two_different_publishers_still_count_twice(self):
        """修复不能变成一律拦死：不同发布方仍必须算 2 个来源。"""
        records = [
            {"url": BBC_FB, "title": BBC_FB_TITLE},
            {"url": SKY_FB, "title": "FA rejects Arsenal appeal over Dowman dismissal"},
        ]
        verdict = ev.evaluate(BBC_FB, records)
        self.assertEqual(verdict["count"], 2)
        self.assertEqual(verdict["status"], ev.MULTIPLE_INDEPENDENT_SOURCES)

    def test_reprint_across_domains_still_counts_once(self):
        """bbc + dailymail + mirror 转载同一篇稿仍然是 1 个来源。"""
        title = "Arsenal lose appeal against Dowman red card"
        records = [
            {"url": BBC_FB, "title": title},
            {"url": "https://www.dailymail.com/sport/article-999", "title": title},
            {"url": "https://www.mirror.co.uk/sport/football/news/arsenal-123",
             "title": title + " after FA hearing"},
        ]
        verdict = ev.evaluate(BBC_FB, records)
        self.assertEqual(verdict["count"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
