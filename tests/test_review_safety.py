"""第 3 项验收：E4 审核安全检查（六条）。

1. 第二来源必须真正独立
2. 转载不能触发自动退出
3. 可信度必须根据独立来源和现有规则重新计算
4. E4 状态不能因人工批准而改变
5. ATTRIBUTED 发布必须保留未证实提示
6. E4 级 S 事件不得自动作为确定事实发送

这一组测试的共同主题：**结论不能由调用方声明，只能由程序重算。**
上一版的 `recheck_review(state, event_id, evidence_status)` 让调用方直接传
证据等级，等于让「抓到第二条的人」自己决定能不能发出去。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bot
import evidence as ev
import importance as imp
import state_store as store

TITLE = "Ronaldo retires from Portugal duty"
ALT_TITLE = "Ronaldo announces international retirement on Instagram"


def enqueue(state, eid="ev_s", refs=None, importance="S", cred="E4"):
    """把一个 E4 级 S 事件放进待审队列。"""
    event = {
        "event_id": eid,
        "title": TITLE,
        "importance": importance,
        "cred": cred,
        "content_type": "BREAKING_NEWS",
        "evidence_status": ev.UNVERIFIED,
        "evidence_refs": refs or [
            {"url": "https://www.thesun.co.uk/sport/s1", "title": TITLE},
        ],
    }
    return bot._enqueue_review(state, event, "E4 级 S 事件")


def rows(pairs):
    return [{"url": u, "title": t} for u, t in pairs]


class TestSecondSourceMustBeIndependent(unittest.TestCase):
    """第 1 条：第二来源必须真正独立。"""

    def setUp(self):
        self.state = {"events": {}}
        enqueue(self.state)

    def test_two_different_publishers_exit(self):
        rec = bot.recheck_review(self.state, "ev_s", rows([
            ("https://www.bbc.co.uk/sport/a", TITLE),
            ("https://www.skysports.com/football/b", ALT_TITLE),
        ]))
        self.assertIsNotNone(rec)
        self.assertGreaterEqual(rec["review_exit_groups"], 2)

    def test_unknown_domains_do_not_count(self):
        # 未识别域名的组 trusted=False，不能充当「独立来源」
        rec = bot.recheck_review(self.state, "ev_s", rows([
            ("https://randomblog1.example/x", TITLE),
            ("https://randomblog2.example/y", ALT_TITLE),
        ]))
        self.assertIsNone(rec)

    def test_aggregator_does_not_count(self):
        # 聚合器本身不是来源
        rec = bot.recheck_review(self.state, "ev_s", rows([
            ("https://news.google.com/rss/articles/abc", TITLE),
        ]))
        self.assertIsNone(rec)

    def test_still_pending_after_failed_check(self):
        bot.recheck_review(self.state, "ev_s", rows([
            ("https://randomblog1.example/x", TITLE),
        ]))
        self.assertEqual(len(bot.list_pending_reviews(self.state)), 1)
        rec = self.state["events"]["ev_s"]
        self.assertEqual(rec["review_status"], "PENDING")
        self.assertIn("未达退出条件", rec["review_last_check"]["note"])


class TestReprintCannotTriggerExit(unittest.TestCase):
    """第 2 条：转载不能触发自动退出。"""

    def setUp(self):
        self.state = {"events": {}}
        enqueue(self.state)

    def test_three_outlets_same_article_no_exit(self):
        rec = bot.recheck_review(self.state, "ev_s", rows([
            ("https://www.bbc.co.uk/sport/r1", TITLE),
            ("https://www.mirror.co.uk/sport/r2", TITLE),
            ("https://www.dailymail.co.uk/sport/r3", TITLE),
        ]))
        self.assertIsNone(rec)
        self.assertEqual(len(bot.list_pending_reviews(self.state)), 1)

    def test_same_publisher_two_pieces_no_exit(self):
        state = {"events": {}}
        enqueue(state, eid="ev_sky", refs=[
            {"url": "https://www.skysports.com/news/s0", "title": TITLE}])
        rec = bot.recheck_review(state, "ev_sky", rows([
            ("https://www.skysports.com/news/s1", TITLE),
            ("https://www.skysports.com/live-blog/s2",
             "Ronaldo calls time on Portugal career"),
        ]))
        self.assertIsNone(rec)

    def test_wire_copy_counts_once(self):
        # 通讯社通稿被多家转载只算 1 个来源
        rec = bot.recheck_review(self.state, "ev_s", rows([
            ("https://www.reuters.com/sports/soccer/w1", TITLE),
            ("https://www.bbc.co.uk/sport/w2", TITLE),
            ("https://www.skysports.com/football/w3", TITLE),
        ]))
        self.assertIsNone(rec)


class TestCredRecomputedNotDeclared(unittest.TestCase):
    """第 3 条：可信度必须根据独立来源和现有规则重新计算。"""

    def setUp(self):
        self.state = {"events": {}}
        enqueue(self.state)

    def test_caller_cannot_pass_status(self):
        # 旧签名第二个位置参数是 evidence_status，传字符串必须无效
        self.assertIsNone(bot.recheck_review(
            self.state, "ev_s", ev.MULTIPLE_INDEPENDENT_SOURCES))
        self.assertEqual(len(bot.list_pending_reviews(self.state)), 1)

    def test_caller_cannot_fake_rows(self):
        # 只能追加「证据行」，等级由 evidence.evaluate 重算
        rec = bot.recheck_review(self.state, "ev_s", rows([
            ("https://www.bbc.co.uk/sport/a", TITLE),
        ]))
        self.assertIsNone(rec, "只有一个独立发布方时不该退出")

    def test_exit_evidence_is_computed_value(self):
        rec = bot.recheck_review(self.state, "ev_s", rows([
            ("https://www.bbc.co.uk/sport/a", TITLE),
            ("https://www.skysports.com/football/b", ALT_TITLE),
        ]))
        self.assertIn(rec["review_exit_evidence"],
                      (ev.SINGLE_SOURCE, ev.MULTIPLE_INDEPENDENT_SOURCES,
                       ev.OFFICIAL_CONFIRMED))

    def test_official_domain_exits_as_official(self):
        rec = bot.recheck_review(self.state, "ev_s", rows([
            ("https://www.manutd.com/news/official",
             "Ronaldo retires from international football"),
        ]))
        self.assertEqual(rec["review_exit_evidence"], ev.OFFICIAL_CONFIRMED)


class TestE4SurvivesApproval(unittest.TestCase):
    """第 4 条：E4 状态不能因人工批准而改变。"""

    def setUp(self):
        self.state = {"events": {}}
        enqueue(self.state)

    def test_cred_unchanged(self):
        rec = bot.approve_review(self.state, "ev_s", "editor_01")
        self.assertEqual(rec["review_cred"], "E4")
        self.assertEqual(rec["publish_cred"], "E4")
        self.assertTrue(rec["cred_unchanged_by_approval"])

    def test_not_marked_confirmed(self):
        rec = bot.approve_review(self.state, "ev_s", "editor_01")
        self.assertFalse(rec["publish_is_confirmed"])

    def test_evidence_status_untouched(self):
        before = self.state["events"]["ev_s"].get("evidence_status")
        bot.approve_review(self.state, "ev_s", "editor_01")
        self.assertEqual(self.state["events"]["ev_s"].get("evidence_status"),
                         before)

    def test_log_records_cred_after_equals_before(self):
        rec = bot.approve_review(self.state, "ev_s", "editor_01")
        last = rec["review_log"][-1]
        self.assertEqual(last["prev_cred"], last["cred_after"])


class TestAttributedKeepsUnverifiedNote(unittest.TestCase):
    """第 5 条：ATTRIBUTED 发布必须保留未证实提示。"""

    def setUp(self):
        self.state = {"events": {}}
        enqueue(self.state)

    def test_publish_mode_is_attributed(self):
        rec = bot.approve_review(self.state, "ev_s", "editor_01")
        self.assertEqual(rec["publish_mode"], "ATTRIBUTED")

    def test_unverified_note_present(self):
        rec = bot.approve_review(self.state, "ev_s", "editor_01")
        note = rec.get("unverified_note") or ""
        self.assertTrue(note)
        self.assertIn("独立来源", note)

    def test_note_names_the_source(self):
        rec = bot.approve_review(self.state, "ev_s", "editor_01")
        self.assertIn("thesun.co.uk", rec["unverified_note"])

    def test_note_survives_serialization(self):
        # 提示必须落在**内容层**，不能只落在状态层：
        # 状态机记住了 E4，但正文没有一句「尚未获得第二个独立来源印证」，
        # 读者看到的仍是一条语气笃定的独家。
        rec = bot.approve_review(self.state, "ev_s", "editor_01")
        payload = {
            "title": rec.get("title"),
            "publish_mode": rec.get("publish_mode"),
            "cred": rec.get("publish_cred"),
            "note": rec.get("unverified_note"),
        }
        self.assertEqual(payload["publish_mode"], "ATTRIBUTED")
        self.assertEqual(payload["cred"], "E4")
        self.assertTrue(payload["note"])


class TestE4SNotSentAsFact(unittest.TestCase):
    """第 6 条：E4 级 S 事件不得自动作为确定事实发送。"""

    def test_route_to_review_not_push(self):
        route, reason = bot._review_route({
            "importance": imp.S, "cred": "E4",
            "content_type": "BREAKING_NEWS"})
        self.assertEqual(route, "REVIEW")
        self.assertTrue(reason)

    def test_enqueue_does_not_send(self):
        state = {"events": {}}
        rec = enqueue(state)
        self.assertEqual(rec["delivery_status"], store.PENDING_REVIEW)
        self.assertIsNone(rec["telegram_message_id"])

    def test_review_state_blocks_auto_delivery(self):
        state = {"events": {}}
        rec = enqueue(state)
        # 待审状态被 blocks_rediscovery 视为已处理，不会被再次抓取自动推送
        self.assertTrue(store.blocks_rediscovery(rec))

    def test_approval_requires_explicit_transition(self):
        state = {"events": {}}
        enqueue(state)
        # 没有 approve/decline 就永远停在 PENDING_REVIEW，不存在「自动过期放行」
        for _ in range(3):
            self.assertIsNone(bot.recheck_review(state, "ev_s"))
        self.assertEqual(state["events"]["ev_s"]["delivery_status"],
                         store.PENDING_REVIEW)

    def test_e4_a_level_not_gated_but_not_confirmed(self):
        # A 级 E4 走既有单源规则，不进审核队列；但也不得当成已确认事实
        route, _ = bot._review_route({
            "importance": imp.A, "cred": "E4",
            "content_type": "BREAKING_NEWS"})
        self.assertEqual(route, "PUSH")
        verdict = {"status": ev.SINGLE_SOURCE}
        note = ev.single_source_note(verdict)
        self.assertTrue(note, "单一来源放行时必须带免责标注")


class TestSignatureIsSafe(unittest.TestCase):
    """签名层面的回归防线：防止有人把 evidence_status 参数加回来。"""

    def test_second_param_is_not_a_status(self):
        import inspect
        params = list(inspect.signature(bot.recheck_review).parameters)
        self.assertEqual(params[:3], ["state", "event_id", "extra_rows"])

    def test_exit_requires_rows(self):
        state = {"events": {}}
        enqueue(state, refs=[])
        state["events"]["ev_s"]["evidence_refs"] = []
        state["events"]["ev_s"]["evidence_url"] = ""
        self.assertIsNone(bot.recheck_review(state, "ev_s"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
