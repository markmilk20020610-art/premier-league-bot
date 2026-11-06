"""PR #1 第二轮审查：自动退出审核的调用链在生产路径上是断的。

问题不是一处，是三层
--------------------
审查报告指出「`same_event.get("event_id")` 已经查不到记录」。实测下来，
它只是最后一层；上游还有两层，任意一层不通，链路都等于不存在：

  第 1 层  `_find_same_event()` 用 `BLOCKING_DELIVERY_STATUSES` 过滤，
           这个集合里**没有** `PENDING_REVIEW` → 审核 stub 在循环里被
           `continue` 掉，`same_event` 恒为 `None`，
           `_exit_review_if_upgraded()` 连函数体都进不去。

  第 2 层  审核 stub 在公开侧**没有标题**（P0 的代价：标题会泄露「哪一条」）。
           就算第 1 层放行，`other_title` 仍是空串，`same_event()` 无法比对。

  第 3 层  `event_id` 已从 `REVIEW_STUB_ALLOWED` 移除，stub 上
           `same_event.get("event_id")` 恒为 `None`；而公开键又改成了随机值，
           `_find_review_rec(state, None)` 什么也找不到。

为什么现有测试没发现
--------------------
`tests/test_review_queue.py::TestAutoExitOnSecondSource` 全部直接传
`review_ref`（`self.ref`），那是**句柄**，P0 之后它仍是可用索引，
所以一直绿。生产走的是「标题 → `_find_same_event` → 钩子」这条完全不同的路。

本文件因此只走生产路径：入队用真实 `_enqueue_review()`，找同事件用
`_find_same_event()`，退出用 `_exit_review_if_upgraded()`，
额外再覆盖「调用方手里只有原始 event_id」的情形（走私有库 by-event 索引）。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bot
import evidence as ev
import review_store
import state_store as store

TITLE = "Ronaldo announces Portugal retirement"
SUN_URL = "https://www.thesun.co.uk/sport/ronaldo-retires?utm_source=rss"


def rss_item(url=SUN_URL, title=TITLE):
    """真实结构的 RSS 条目，与 `_merge_pool` 读取的字段一一对应。"""
    return {
        "title": title,
        "link": url,
        "desc": "球员已在个人账号宣布退出国家队。",
        "source": "The Sun",
        "feed_origin": "The Sun",
        "publisher": "thesun.co.uk",
        "published": "2026-11-05T20:00:00+00:00",
    }


def scan_event_for(item, importance="S", cred="E4"):
    """按 `run_scan_mode()` 的构造方式还原送审事件（含原始 event_id）。"""
    evidence_url = item["link"]
    evidence_key = bot.clean_url(evidence_url)
    title = item["title"]
    return {
        "event_id": store.event_id_for(evidence_key or evidence_url, title),
        "title": title,
        "importance": importance,
        "cred": cred,
        "content_type": "BREAKING_NEWS",
        "event_type": "TRANSFER",
        "fact_summary": "球员已在个人账号宣布退出国家队。",
        "evidence_status": ev.UNVERIFIED,
        "evidence_url": evidence_url,
        "evidence_refs": [{"url": evidence_url, "title": title}],
    }


def second_source_rows():
    """第二个**独立**来源：域名不同、标题措辞不同（不是转载）。"""
    return [
        {"url": "https://www.bbc.co.uk/sport/ronaldo-retirement",
         "title": "Ronaldo retires from Portugal duty"},
        {"url": "https://www.skysports.com/football/ronaldo-international",
         "title": "Ronaldo announces international retirement on Instagram"},
    ]


class _ChainCase(unittest.TestCase):
    """每个用例一份全新的内存私有存储 + 一条已在审的事件。"""

    def setUp(self):
        super().setUp()
        self._saved_tg = (bot.TELEGRAM_BOT_TOKEN, bot.TELEGRAM_CHAT_ID,
                          bot.TELEGRAM_REVIEW_CHAT_ID)
        self._saved_sort = bot.IMPORTANCE_SORT
        bot.TELEGRAM_BOT_TOKEN = "TEST_TOKEN"
        bot.TELEGRAM_CHAT_ID = "123456"
        bot.TELEGRAM_REVIEW_CHAT_ID = "654321"
        bot.IMPORTANCE_SORT = True
        self.client = review_store.LocalReviewStore(":memory:")
        bot.set_review_store(self.client)

        self.state = store._default_state()
        self.item = rss_item()
        bot._merge_pool(self.state, [self.item])       # 早报素材先落（正常路径）
        self.event = scan_event_for(self.item)
        self.raw_event_id = self.event["event_id"]     # 原始 id，公开侧不存
        route, reason = bot._review_route(self.event)
        self.assertEqual(route, "REVIEW", "前置条件：该候选必须被判为需人工审核")
        view = bot._enqueue_review(self.state, self.event, reason)
        self.assertIsNotNone(view, "前置条件：必须能入队")
        self.review_ref = view["review_ref"]

    def tearDown(self):
        bot.set_review_store(None)
        self.client.close()
        bot.IMPORTANCE_SORT = self._saved_sort
        (bot.TELEGRAM_BOT_TOKEN, bot.TELEGRAM_CHAT_ID,
         bot.TELEGRAM_REVIEW_CHAT_ID) = self._saved_tg
        super().tearDown()

    # ---- 断言辅助 ----

    def stub(self):
        return store.find_review_stub(self.state, self.review_ref)

    def same_event(self):
        """按生产代码的调用方式取「同一件事」的记录。"""
        return bot._find_same_event(self.state, TITLE, self.item["link"],
                                    statuses=bot.DEDUPE_DELIVERY_STATUSES)

    def private_status(self):
        return self.client.get(self.review_ref)["status"]


class TestProductionHook(_ChainCase):
    """走生产钩子：_find_same_event → _exit_review_if_upgraded。"""

    def test_same_event_is_found_through_review_stub(self):
        """第 1 层：审核 stub 必须能被当成「同一件事」找出来。

        旧实现用 BLOCKING_DELIVERY_STATUSES 过滤，PENDING_REVIEW 不在其中，
        这条断言会直接拿到 None——后面所有步骤都无从谈起。
        """
        found = self.same_event()
        self.assertIsNotNone(found, "审核 stub 没有被 _find_same_event 识别")
        self.assertEqual(found.get("delivery_status"), store.PENDING_REVIEW)
        self.assertEqual(found.get("review_ref"), self.review_ref)

    def test_stub_has_no_event_id_so_hook_must_use_ref(self):
        """第 3 层的前提：stub 上确实没有 event_id（P0 的正确结果）。

        这条不是缺陷断言，而是**约束**：正因为没有 event_id，
        钩子就不能再拿 `same_event.get("event_id")` 当索引。
        """
        found = self.same_event()
        self.assertNotIn("event_id", found)
        self.assertIsNone(found.get("event_id"))

    def test_second_independent_source_exits_through_hook(self):
        """核心回归：第二独立来源到达 → 走生产钩子 → 真的退出审核。"""
        found = self.same_event()
        exited = bot._exit_review_if_upgraded(self.state, found, second_source_rows())
        self.assertTrue(exited, "生产钩子没有触发退出")
        self.assertEqual(self.stub()["delivery_status"], store.PENDING_DELIVERY)
        self.assertEqual(self.private_status(), review_store.EXITED)

    def test_exit_reason_records_independent_groups(self):
        """退出原因写进**私有**日志，公开 stub 一个字都不多。

        `review_exit_*` 是视图字段，只随返回值给调用方，不落公开 state——
        否则「独立发布方 3 组」这种信息又成了新的公开线索。
        """
        found = self.same_event()
        bot._exit_review_if_upgraded(self.state, found, second_source_rows())
        entries = self.client.log(self.review_ref)
        self.assertEqual(entries[-1]["action"], "EXITED")
        self.assertIn("独立来源", entries[-1]["detail"])
        self.assertIn("组", entries[-1]["detail"])
        self.assertNotIn("review_exit_reason", self.stub())
        self.assertNotIn("review_exit_groups", self.stub())

    def test_reprint_does_not_exit_through_hook(self):
        """同一篇稿被转载 → 1 个独立发布方 → 不得退出（防误放行）。"""
        found = self.same_event()
        reprint = [
            {"url": "https://www.bbc.co.uk/sport/r1",
             "title": "Ronaldo announces Portugal retirement"},
            {"url": "https://www.mirror.co.uk/sport/r2",
             "title": "Ronaldo announces Portugal retirement"},
        ]
        self.assertFalse(bot._exit_review_if_upgraded(self.state, found, reprint))
        self.assertEqual(self.stub()["delivery_status"], store.PENDING_REVIEW)
        self.assertEqual(self.private_status(), review_store.PENDING)

    def test_exit_keeps_public_stub_content_free(self):
        """退出之后公开 stub 仍然不含标题/摘要/证据（P0 不能因为修链路而回退）。"""
        found = self.same_event()
        bot._exit_review_if_upgraded(self.state, found, second_source_rows())
        stub = self.stub()
        for leaked in ("title", "article_title", "fact_summary", "evidence_url",
                       "evidence_refs", "review_payload", "event_id"):
            self.assertNotIn(leaked, stub, "退出后公开 stub 出现字段 {}".format(leaked))
        self.assertEqual(store.public_review_violations(self.state), [])

    def test_exit_leaves_pending_list(self):
        found = self.same_event()
        bot._exit_review_if_upgraded(self.state, found, second_source_rows())
        self.assertEqual(bot.list_pending_reviews(self.state), [])


class TestRawEventIdLookup(_ChainCase):
    """调用方手里只有原始 event_id 的情形（走私有库 by-event 索引）。"""

    def test_recheck_by_raw_event_id_exits(self):
        """直接拿原始 event_id 调 recheck_review，必须能定位并退出。

        公开键已经是随机值，`_find_review_rec` 查不到；只能靠私有库的
        `find_active(event_id)` 换出 review_ref，再回公开 state 找 stub。
        """
        rec = bot.recheck_review(self.state, self.raw_event_id, second_source_rows())
        self.assertIsNotNone(rec, "原始 event_id 无法定位审核记录，退出链路仍断")
        self.assertEqual(self.stub()["delivery_status"], store.PENDING_DELIVERY)
        self.assertEqual(self.private_status(), review_store.EXITED)

    def test_raw_event_id_not_in_public_state(self):
        """前提校验：原始 event_id 确实不在公开 state 里（P0 的正确结果）。"""
        self.assertNotIn(self.raw_event_id, self.state.get("events") or {})
        self.assertIsNone(bot._find_review_rec(self.state, self.raw_event_id))

    def test_unknown_event_id_returns_none(self):
        self.assertIsNone(bot.recheck_review(self.state, "deadbeef1234",
                                             second_source_rows()))
        self.assertEqual(self.stub()["delivery_status"], store.PENDING_REVIEW)

    def test_empty_event_id_returns_none(self):
        self.assertIsNone(bot.recheck_review(self.state, "", second_source_rows()))

    def test_store_missing_fails_safe(self):
        """私有库不可用 → 查不到就当作「没有待退出」，绝不误放行。"""
        bot.set_review_store(None)
        bot._REVIEW_STORE = None
        self.assertIsNone(bot.recheck_review(self.state, self.raw_event_id,
                                             second_source_rows()))
        self.assertEqual(self.stub()["delivery_status"], store.PENDING_REVIEW)


class TestFindActiveContract(unittest.TestCase):
    """`find_active()` 是本次唯一新增的存储接口，两端必须同形。"""

    def setUp(self):
        super().setUp()
        self.client = review_store.LocalReviewStore(":memory:")

    def tearDown(self):
        self.client.close()
        super().tearDown()

    def test_returns_ref_for_active_record(self):
        ref = self.client.enqueue({"event_id": "ev_x", "title": "t"})["review_ref"]
        got = self.client.find_active("ev_x")
        self.assertTrue(got["found"])
        self.assertEqual(got["review_ref"], ref)
        self.assertEqual(got["status"], review_store.PENDING)

    def test_unknown_event_id_returns_empty_shape(self):
        got = self.client.find_active("nope")
        self.assertEqual(got, {"found": False, "review_ref": "", "status": ""})

    def test_empty_event_id_returns_empty_shape(self):
        self.assertEqual(self.client.find_active(""),
                         {"found": False, "review_ref": "", "status": ""})

    def test_exited_record_is_not_active(self):
        """退出后不应再被反查到——否则会拿旧句柄重复走退出流程。"""
        ref = self.client.enqueue({"event_id": "ev_y", "title": "t"})["review_ref"]
        self.client.exit_review(ref, "证据达标")
        self.assertFalse(self.client.find_active("ev_y")["found"])


class TestGuardExtensions(unittest.TestCase):
    """本轮附带的两处守卫加固。"""

    def test_s_overflow_nested_private_field_is_caught(self):
        """s_overflow 是嵌套结构，写在里层的私有字段也必须被抓到。"""
        state = store._default_state()
        state["s_overflow"] = {
            "2026-11-05": {"urls": ["https://a.com/x"],
                           "review_payload": {"title": "压着没发的那条"}},
        }
        violations = store.public_review_violations(state)
        self.assertTrue(any("review_payload" in v for v in violations),
                        "s_overflow 里层的私有字段没有被发现：{}".format(violations))

    def test_s_overflow_deep_list_item_is_caught(self):
        state = store._default_state()
        state["s_overflow"] = {
            "2026-11-05": {"urls": [{"url": "https://a.com/x",
                                     "fact_summary": "未核实的说法"}]},
        }
        violations = store.public_review_violations(state)
        self.assertTrue(any("fact_summary" in v for v in violations))

    def test_url_variant_with_fragment_before_query(self):
        """`x#frag?utm=1` 这种顺序：先切 # 再切 ? 才拿得到真正的路径。

        说明：`#` 与 `?` 必有一个是首个分隔符，所以原来的三种变体
        （原样 / 去 # / 去 ?）在数学上已经完备，本用例在旧实现下同样通过。
        加上第四种变体是**显式表达**而非新增覆盖——守卫代码里写清楚
        「两个都去」，比依赖「总有一个 split 先命中」更好审。
        这里保留用例是为了锁住这条不变量，不是回归项。
        """
        state = store._default_state()
        raw = "https://a.com/story#comments?utm_source=rss"
        derived = store.event_id_for("https://a.com/story")
        state["events"] = {derived: {"delivery_status": store.PENDING_REVIEW,
                                     "review_ref": "abc123"}}
        state["news_pool"] = [{"link": raw, "title": "t"}]
        violations = store.public_review_violations(state)
        self.assertTrue(any("关联泄露" in v for v in violations),
                        "去掉 query+fragment 的变体没有覆盖：{}".format(violations))

    def test_clean_state_has_no_violations(self):
        self.assertEqual(store.public_review_violations(store._default_state()), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
