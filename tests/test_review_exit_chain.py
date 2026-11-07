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
import contextlib
import datetime
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

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
        # 进程内标题缓存必须从干净状态开始：它按 review_ref 索引，
        # 上一个用例的标题若残留，下一个用例可能读到错标题而不报错。
        # 不依赖 set_review_store() 的副作用，显式清一次。
        bot._REVIEW_TITLE_CACHE.clear()
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
        bot._REVIEW_TITLE_CACHE.clear()
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


# ============================================================================
# 真正执行 run_scan_mode() 的端到端回归
# ============================================================================

ORIGINAL_TITLE = "Arsenal manager confirms Ronaldo transfer plan"
ORIGINAL_URL = "https://www.thesun.co.uk/sport/ronaldo-arsenal"
RSS_TITLE_A = "Arsenal boss confirms plan to sign Ronaldo"
RSS_TITLE_B = "Ronaldo set for Arsenal switch as manager speaks out"
RSS_URL_A = "https://www.bbc.co.uk/sport/ronaldo-arsenal"
RSS_URL_B = "https://www.skysports.com/football/ronaldo-arsenal"


def model_result_for(evidence_url, evidence_title):
    """一份能过 schema 与领域闸门的最小模型输出。"""
    return {
        "has_valid_news": True,
        "event_title": ORIGINAL_TITLE,
        "evidence_url": evidence_url,
        "evidence_title": evidence_title,
        "fact_summary": "阿森纳主帅确认引进 C 罗的计划，两家独立媒体交叉印证。",
        "verification_status": "SINGLE_SOURCE",
        "evidence_status": ev.SINGLE_SOURCE,
        "sport": "football",
        "var_rating": "⚡【闪电突袭·跟队独家】",
        "draft_a": "阿森纳主帅在赛前发布会上确认了这笔引援计划",
        "draft_b": "两家独立媒体均报道同一消息，可作交叉印证",
        "draft_c": "转会窗口规则与俱乐部财政公平政策的双重约束",
        "cover_blueprint": "酋长球场夜景，红色烟火",
        "image_prompt": "stadium at night with red flares",
        "banner_overlay_text": "阿森纳确认引援计划",
    }


class _ScanModeFixture(unittest.TestCase):
    """跑 `run_scan_mode()` 的共享夹具：真实主流程 + 五个外部依赖桩。

    单独抽成基类，是为了让「正常退出」与「崩溃恢复」两组测试共用同一套夹具，
    同时避免测试方法被子类重复收集。
    """

    def setUp(self):
        super().setUp()
        # 与 _ChainCase 同样的理由：标题缓存按 review_ref 索引，
        # 用例之间必须互相隔离，不能靠上一条用例顺手清干净。
        bot._REVIEW_TITLE_CACHE.clear()
        self.client = review_store.LocalReviewStore(":memory:")
        bot.set_review_store(self.client)
        self.state = store._default_state()
        self.now = datetime.datetime.now(datetime.timezone.utc)

        # ---- 上一轮：E4 + S 已入队，公开侧只剩一条随机键 stub ----
        self.old_event = {
            "event_id": store.event_id_for(bot.clean_url(ORIGINAL_URL), ORIGINAL_TITLE),
            "title": ORIGINAL_TITLE,
            "importance": "S",
            "cred": "E4",
            "content_type": "BREAKING_NEWS",
            "event_type": "TRANSFER",
            "fact_summary": "据某小报报道，阿森纳主帅确认了引进 C 罗的计划。",
            "evidence_status": ev.UNVERIFIED,
            "evidence_url": ORIGINAL_URL,
            "evidence_refs": [{"url": ORIGINAL_URL, "title": ORIGINAL_TITLE}],
        }
        route, reason = bot._review_route(self.old_event)
        self.assertEqual(route, "REVIEW", "前置条件：上一轮必须被判为需人工审核")
        view = bot._enqueue_review(self.state, self.old_event, reason)
        self.assertIsNotNone(view)
        self.review_ref = view["review_ref"]
        self.raw_event_id = self.old_event["event_id"]
        self.stub_key = next(iter(self.state["events"]))

        # ---- 本轮 RSS：两家独立媒体，标题措辞不同（不是转载）----
        self.rss_items = [
            {"title": RSS_TITLE_A, "link": RSS_URL_A, "desc": "d",
             "source": "BBC", "feed_origin": "BBC", "publisher": "bbc.co.uk",
             "published": self.now},
            {"title": RSS_TITLE_B, "link": RSS_URL_B, "desc": "d",
             "source": "Sky", "feed_origin": "Sky", "publisher": "skysports.com",
             "published": self.now},
        ]

        # ---- 五个外部依赖打桩 ----
        self._patch(bot, "fetch_rss_sources",
                    lambda: (self.rss_items, {"https://feed.example/rss": 200}))
        self.telegram_calls = []
        self._patch(bot, "evaluate_with_gemini",
                    lambda blob, is_morning=False, state=None:
                    (model_result_for(RSS_URL_A, RSS_TITLE_A), ""))
        self._patch(bot, "save_state", lambda st: None)
        self._patch(bot, "log_telemetry", lambda d: None)
        # 只桩发送层，不桩 _deliver_card —— 投递状态机要真跑，否则
        # 「PENDING_DELIVERY → COMMITTED」这种迁移错误会被桩掩盖掉。
        self._patch(bot, "send_telegram_ex",
                    lambda card, prior=None, report=None:
                    (self.telegram_calls.append(card) or (True, ["999"], "")))
        self._patch(bot, "send_review_card", lambda rec, ref: "888")
        self._patch(bot, "IMPORTANCE_SORT", True)

        # ---- 记录 _find_same_event 的入参与命中结果，证明主流程用了哪个集合 ----
        # 只做旁路观察，判定仍由生产函数自己做（不 mock 判断逻辑）。
        self.seen_statuses = []
        self.seen_finds = []
        real_find = bot._find_same_event

        def spy(state, title, url="", statuses=None):
            hit = real_find(state, title, url, statuses=statuses)
            # 命中的是哪一条、什么状态：只记布尔不够，无法排除「命中了别的记录」
            hit_key = ""
            if hit is not None:
                hit_key = next((k for k, v in (state.get("events") or {}).items()
                                if v is hit), "")
            self.seen_statuses.append(statuses)
            self.seen_finds.append({
                "title": title,
                "url": url,
                "hit": hit is not None,
                "hit_key": hit_key,
                "hit_status": (hit or {}).get("delivery_status", ""),
                "statuses": statuses,
            })
            return hit

        self._patch(bot, "_find_same_event", spy)

    def _patch(self, target, name, value):
        p = mock.patch.object(target, name, value)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        bot.set_review_store(None)
        bot._REVIEW_TITLE_CACHE.clear()
        self.client.close()
        super().tearDown()

    def run_scan(self):
        return bot.run_scan_mode(self.state, self.now.isoformat())

    def stub(self):
        return store.find_review_stub(self.state, self.review_ref)

class TestRunScanModeEndToEnd(_ScanModeFixture):
    """真正执行 `run_scan_mode()` 主流程，验证自动退出审核。

    上一组 `TestProductionHook` 调用的确实都是生产函数，但**串接关系**是测试
    自己写的（`_find_same_event` 的结果手动喂给 `_exit_review_if_upgraded`）。
    真实调用链藏在 `run_scan_mode()` 的 `elif same_event is not None and not
    _exit_review_if_upgraded(...)` 这一个表达式里，只有跑它才算验证。
    """

    # ---- 七个验证点 ----

    def test_1_stub_starts_pending_review(self):
        """① 开跑前公开 state 里有一条 PENDING_REVIEW stub。"""
        stub = self.stub()
        self.assertIsNotNone(stub)
        self.assertEqual(stub["delivery_status"], store.PENDING_REVIEW)

    def test_2_private_store_keeps_title_and_event_id(self):
        """② 标题与 event_id 只在私有库，公开侧没有。"""
        payload = self.client.peek_payload(self.review_ref)
        self.assertEqual(payload.get("title"), ORIGINAL_TITLE)
        self.assertEqual(payload.get("event_id"), self.raw_event_id)
        self.assertNotIn("event_id", self.stub())
        self.assertNotIn("title", self.stub())

    def test_3_new_rss_is_same_event(self):
        """③ 新报道标题与原事件属于同一事件（用生产判定函数验，不靠猜）。"""
        anchor = bot._event_probe(ORIGINAL_TITLE, ORIGINAL_URL)
        for title in (RSS_TITLE_A, RSS_TITLE_B):
            ok, _why = ev.same_event(anchor, bot._event_probe(title, ""))
            self.assertTrue(ok, "{} 应判为同一事件".format(title))

    def test_4_second_source_is_independent(self):
        """④ 新报道构成第二个独立来源（转载不算）。"""
        rows = [
            {"url": ORIGINAL_URL, "title": ORIGINAL_TITLE, "publisher": "thesun.co.uk"},
            {"url": RSS_URL_A, "title": RSS_TITLE_A, "publisher": "bbc.co.uk"},
            {"url": RSS_URL_B, "title": RSS_TITLE_B, "publisher": "skysports.com"},
        ]
        records = ev.build_records(rows)
        groups = [g for g in ev.independent_sources(records) if g.get("trusted", True)]
        self.assertGreaterEqual(len(groups), 2, "独立来源组数不足，退出条件不成立")

    def test_5_scan_uses_dedupe_status_set(self):
        """⑤ 主流程确实用 DEDUPE_DELIVERY_STATUSES（含 PENDING_REVIEW）去找同事件。

        这是第 1 层断裂的正面证据：旧实现在这里传的是
        BLOCKING_DELIVERY_STATUSES，审核 stub 永远匹配不到。
        """
        self.run_scan()
        hit = [s for s in self.seen_statuses
               if s and store.PENDING_REVIEW in s]
        self.assertTrue(hit, "run_scan_mode 从未用包含 PENDING_REVIEW 的集合查同事件")

    def test_6_review_exits_and_private_status_is_exited(self):
        """⑥ 自动退出被触发，私有状态变 EXITED。"""
        code = self.run_scan()
        self.assertEqual(code, 0)
        self.assertEqual(self.client.get(self.review_ref)["status"],
                         review_store.EXITED)
        entries = [e["action"] for e in self.client.log(self.review_ref)]
        self.assertIn("EXITED", entries)

    def test_7_public_stub_has_no_private_content(self):
        """⑦ 退出后公开 stub 不含标题 / event_id / 摘要 / 证据字段。"""
        self.run_scan()
        stub = self.stub()
        for leaked in ("title", "article_title", "event_id", "fact_summary",
                       "evidence_url", "evidence_refs", "review_payload",
                       "review_exit_reason"):
            self.assertNotIn(leaked, stub, "退出后公开 stub 出现字段 {}".format(leaked))
        self.assertEqual(store.public_review_violations(self.state), [])

    def test_8_exit_reason_not_written_to_public_state(self):
        """退出原因只进私有日志，公开侧一个字都不多。"""
        self.run_scan()
        detail = self.client.log(self.review_ref)[-1]["detail"]
        self.assertIn("独立来源", detail)
        self.assertNotIn("独立来源", json.dumps(self.state, ensure_ascii=False))

    # ---- 修复后：退出必须复用原 stub，不留孤儿 ----

    def test_9_exit_reuses_stub_no_orphan(self):
        """退出后只有一条记录，且**原随机键**走到了 COMMITTED。

        修复前：`record_event(event_id=None, ...)` 因为 stub 上没有 `event_id`
        而新建一条 `sha1(url)[:12]` 记录，原 stub 停在 PENDING_DELIVERY 成为孤儿，
        同一事件在 state 里分裂成两条。
        """
        self.run_scan()
        events = self.state.get("events") or {}
        statuses = {k: v.get("delivery_status") for k, v in events.items()}
        self.assertEqual(len(events), 1,
                         "同一事件不得分裂成多条记录：{}".format(statuses))
        self.assertIn(self.stub_key, events, "原随机键记录必须仍在（不能删了重建）")
        self.assertEqual(events[self.stub_key]["delivery_status"], store.COMMITTED,
                         "原 stub 必须完成 PENDING_DELIVERY → DELIVERED → COMMITTED")

    def test_10_no_sha1_key_record_created(self):
        """不得额外生成 `sha1(url)[:12]` 形状的公开记录。"""
        self.run_scan()
        keys = list((self.state.get("events") or {}).keys())
        self.assertEqual(keys, [self.stub_key],
                         "出现了非原 stub 的键：{}".format(keys))
        for key in keys:
            self.assertFalse(store._looks_like_derived_id(key),
                             "公开记录键 {} 仍是 URL 派生 id".format(key))

    def test_11_rescan_dedupes_against_committed_stub(self):
        """再次扫描同一事件：去重命中的是这条已完成投递的记录。

        用**第三个域名**（新 URL）模拟后续报道，这样 URL 去重拦不住，
        只有事件级去重能拦住——正好检验退出后的 stub 还能不能被认出。
        期望：不进候选、不入队、不重复投递、不新增记录。

        两处夹具陷阱：
          · 第一版模型桩固定返回 BBC，第二轮会被 **URL 去重**拦下（BBC URL
            已在 processed_urls 里），测试照样绿，但测的是 URL 去重，不是事件级
            去重——原用例的假绿就出在这里；
          · 换成同事件标题后，事件级去重发生在**候选阶段**，模型一次都不会被
            调用。所以这里不断言「模型输出的 evidence_url」（它压根不存在），
            而是断言「程序拿去比对同事件的 URL 就是 ESPN」——那才是真正被采纳
            的证据。模型桩仍换成 ESPN 作防御：万一将来去重退到模型之后，
            旧桩会让测试又退回 URL 去重的假绿。
        """
        self.run_scan()
        self.assertEqual(len(self.telegram_calls), 1)
        self.assertEqual(len(self.state.get("events") or {}), 1)

        espn_url = "https://www.espn.com/soccer/ronaldo-arsenal"
        espn_title = "Ronaldo to join Arsenal as clubs reach agreement"

        # 前置①：ESPN 标题必须与原事件同事件——用生产判定函数验，不靠猜
        ok, why = ev.same_event(
            bot._event_probe(ORIGINAL_TITLE, ORIGINAL_URL),
            bot._event_probe(espn_title, espn_url))
        self.assertTrue(ok, "ESPN 报道应判为同一事件：{}".format(why))

        # 前置②：ESPN URL 从未处理过，否则拦住它的是 URL 去重而非事件去重
        processed = list(self.state.get("processed_urls") or [])
        self.assertNotIn(espn_url, processed)
        self.assertNotIn(bot.clean_url(espn_url), processed)

        # 换一批 RSS：同一事件，第三家媒体，URL 从未处理过
        self.rss_items = [
            {"title": espn_title, "link": espn_url, "desc": "d",
             "source": "ESPN", "feed_origin": "ESPN", "publisher": "espn.com",
             "published": self.now},
        ]

        # 模型桩也必须换成 ESPN：沿用 BBC 输出会让 URL 去重先拦下，
        # 测试通过的原因就变成「BBC URL 处理过」了。
        model_outputs = []

        def espn_model(blob, is_morning=False, state=None):
            model_outputs.append(blob)
            return model_result_for(espn_url, espn_title), ""

        self._patch(bot, "evaluate_with_gemini", espn_model)

        self.seen_finds = []
        code = self.run_scan()
        self.assertEqual(code, 0)

        # ★ 关键断言：程序拿去比对同事件的证据是 ESPN，不是第一次的 BBC。
        # 检查**所有** ESPN 标题的调用，而不是只看第一条——只看首条会漏掉
        # 「候选阶段命中、拼卡片前又没命中」这类自相矛盾的情形。
        espn_finds = [f for f in self.seen_finds if f["title"] == espn_title]
        self.assertTrue(espn_finds, "第二轮没有用 ESPN 标题做同事件比对")
        for i, f in enumerate(espn_finds, 1):
            self.assertEqual(f["url"], espn_url,
                             "第 {} 次比对用的 URL 必须是 ESPN，不能沿用 BBC".format(i))
            self.assertTrue(f["hit"],
                            "第 {} 次比对没有命中已完成投递的记录".format(i))
            self.assertEqual(f["hit_key"], self.stub_key,
                             "第 {} 次命中的不是原审核 stub".format(i))
            self.assertEqual(f["hit_status"], store.COMMITTED,
                             "第 {} 次命中的记录不是 COMMITTED".format(i))
        # 所有调用结果必须一致：同一条记录不能一次命中一次不命中
        self.assertEqual(len({(f["hit"], f["hit_key"], f["hit_status"])
                              for f in espn_finds}), 1,
                         "ESPN 标题的多次比对出现了互相矛盾的结果")

        self.assertFalse([f for f in self.seen_finds if f["url"] == RSS_URL_A],
                         "第二轮仍拿第一次的 BBC URL 做比对，模型桩没换干净")

        # 事件级去重在候选阶段就完成，模型一次都没调（省 token 的正确行为）。
        # 若这里非空，说明去重退到了模型之后，是行为变化，需要人看一眼。
        self.assertEqual(model_outputs, [], "事件级去重应发生在模型调用之前")

        # ---- 原有断言 ----
        self.assertEqual(len(self.telegram_calls), 1, "不得重复投递")
        self.assertEqual(len(self.state.get("events") or {}), 1, "不得新增记录")
        self.assertIn(self.stub_key, self.state.get("events") or {},
                      "原随机键必须仍在")
        self.assertEqual(self.client.get(self.review_ref)["status"],
                         review_store.EXITED, "不得重新入队")

    def test_12_delivered_record_carries_no_private_content(self):
        """投递完成后，这条曾是审核 stub 的记录仍不含任何私有内容。

        尤其要盯 `pending_text`：卡片全文含中文标题与事实摘要，如果投递后
        留在公开 state 里，等于把 P0 刚藏起来的内容又写回去。
        """
        self.run_scan()
        rec = (self.state.get("events") or {})[self.stub_key]
        for leaked in ("title", "article_title", "event_id", "fact_summary",
                       "evidence_url", "evidence_refs", "review_payload",
                       "review_reason", "review_log"):
            self.assertNotIn(leaked, rec, "投递后公开记录出现字段 {}".format(leaked))
        self.assertEqual(rec.get("pending_text"), "",
                         "投递完成后不得保留卡片正文")
        self.assertEqual(store.public_review_violations(self.state), [])
        self.assertNotIn(ORIGINAL_TITLE,
                         json.dumps(self.state, ensure_ascii=False),
                         "原始英文标题不得出现在公开 state 的任何位置")


class TestHttpFindActive(unittest.TestCase):
    """`HttpReviewStore.find_active()` 的解析契约。

    LocalReviewStore 那组测的是 SQL，DO 那组测的是返回字段，中间这一层
    （Python 怎么解析 HTTP 响应）此前没有任何测试。它恰恰最容易出问题：
    解析错一个字段名，表现就是「永远 found=False」，而本地全绿。
    """

    REF = "0123456789abcdef"
    STATUS = "pending"

    def client(self, transport):
        return review_store.HttpReviewStore("https://vault.example", "svc_key",
                                            transport=transport)

    def test_found_true_parses_ref_and_status(self):
        c = self.client(lambda path, method="GET", payload=None:
                        {"ok": True, "found": True,
                         "review_ref": self.REF, "status": self.STATUS})
        self.assertEqual(c.find_active("ev_x"),
                         {"found": True, "review_ref": self.REF, "status": self.STATUS})

    def test_found_false_returns_unified_empty_shape(self):
        c = self.client(lambda path, method="GET", payload=None:
                        {"ok": True, "found": False, "review_ref": "", "status": ""})
        self.assertEqual(c.find_active("ev_x"),
                         {"found": False, "review_ref": "", "status": ""})

    def test_missing_fields_do_not_crash(self):
        """DO 少返回字段时按「没找到」处理，而不是抛 KeyError。"""
        c = self.client(lambda path, method="GET", payload=None: {"ok": True})
        self.assertEqual(c.find_active("ev_x"),
                         {"found": False, "review_ref": "", "status": ""})

    def test_store_error_propagates_not_swallowed(self):
        """存储异常必须抛出。吞成 found=False 会让「查不到」和「查不了」
        变成同一件事——自动退出会安静地永不发生，且没人知道为什么。"""
        def boom(path, method="GET", payload=None):
            raise review_store.ReviewStoreError("vault 503")
        with self.assertRaises(review_store.ReviewStoreError):
            self.client(boom).find_active("ev_x")

    def test_http_409_raises_not_found_false(self):
        """真走一遍 urllib 的 HTTPError 分支：409 必须抛，不能伪装成 found=False。"""
        body = json.dumps({"ok": False, "error": "conflict",
                           "conflict": True}).encode()
        err = urllib.error.HTTPError("https://vault.example/internal/review/by-event",
                                     409, "Conflict", {}, io.BytesIO(body))
        c = review_store.HttpReviewStore("https://vault.example", "svc_key")
        with mock.patch.object(review_store.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(review_store.ReviewStoreError):
                c.find_active("ev_x")

    def test_http_500_raises(self):
        err = urllib.error.HTTPError("https://vault.example/x", 500, "Server Error",
                                     {}, io.BytesIO(b"boom"))
        c = review_store.HttpReviewStore("https://vault.example", "svc_key")
        with mock.patch.object(review_store.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(review_store.ReviewStoreError):
                c.find_active("ev_x")

    def test_shape_matches_local_store(self):
        """两个后端返回同一组键——调用方不必区分后端。"""
        local = review_store.LocalReviewStore(":memory:")
        self.addCleanup(local.close)
        remote = self.client(lambda path, method="GET", payload=None:
                             {"ok": True, "found": False, "review_ref": "", "status": ""})
        self.assertEqual(set(local.find_active("nope")), set(remote.find_active("nope")))

    def test_sends_event_id_in_body(self):
        """请求体字段名必须是 event_id（DO 读的是 body.event_id）。"""
        seen = {}

        def transport(path, method="GET", payload=None):
            seen.update({"path": path, "method": method, "payload": payload})
            return {"ok": True, "found": False, "review_ref": "", "status": ""}

        self.client(transport).find_active("ev_body")
        self.assertEqual(seen["path"], "/internal/review/by-event")
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["payload"], {"event_id": "ev_body"})


# ============================================================================
# COMMITTED 落盘失败后的崩溃恢复
# ============================================================================

class TestRecoveryAfterCommitFailure(_ScanModeFixture):
    """审核 stub 投递成功、但 COMMITTED 落盘失败时的崩溃恢复。

    真实时序（`run_scan_mode` 投递成功分支）：
        ① save_state(PENDING_DELIVERY)      成功
        ② _deliver_card → mark_delivered    → DELIVERED，pending_text 被清空
        ③ save_state(DELIVERED)             成功  ← 磁盘停在这里
        ④ transition(COMMITTED)
        ⑤ save_state(COMMITTED)             失败  ← 本轮模拟点

    重启后 `main()` 的恢复段应看到 DELIVERED，只补提交、绝不重发。
    恢复逻辑本身不 mock：`store.recovery_actions()` / `store.apply_recovery()`
    以及 `main()` 里的调用顺序都是生产代码。
    """

    def setUp(self):
        # 父类 setUp 会把 save_state 桩成空操作；先抓一份真身，恢复时要用。
        self._real_save_state = bot.save_state
        super().setUp()

        self.snapshots = []
        self.save_calls = 0
        self.saves_after_delivery = 0
        self.delivered = False

        def fake_send(card, prior=None, report=None):
            self.telegram_calls.append(card)
            self.delivered = True
            return True, ["999"], ""

        self._patch(bot, "send_telegram_ex", fake_send)

        def flaky_save(state):
            self.save_calls += 1
            if self.delivered:
                self.saves_after_delivery += 1
                # 投递成功后的第 1 次落盘是 DELIVERED（必须成功），
                # 第 2 次才是 COMMITTED —— 只让第 2 次失败。
                if self.saves_after_delivery >= 2:
                    raise store.StateError("模拟 COMMITTED 落盘失败")
            # 用真实序列化结果，模拟磁盘上的字节
            self.snapshots.append(
                json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True))

        self._patch(bot, "save_state", flaky_save)
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, True)

    def crash_after_delivered(self):
        """跑一轮扫描，让它在 COMMITTED 落盘处失败，返回磁盘快照。"""
        self.run_scan()
        return json.loads(self.snapshots[-1])

    def restart_and_recover(self):
        """把磁盘快照落成真文件，调用真实 `bot.main()`（恢复段是真代码）。"""
        path = os.path.join(self.tmpdir, "state.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.snapshots[-1])

        self._patch(bot, "STATE_FILE", path)
        self._patch(bot, "RUN_MODE", "scan")
        # 恢复之后 main() 会继续跑扫描；本轮只验证恢复段，桩掉扫描。
        self._patch(bot, "run_scan_mode", lambda st, start: 0)
        # 父类把 save_state 桩成了空操作，恢复结果必须真落盘才叫「恢复」。
        self._patch(bot, "save_state", self._real_save_state)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = bot.main()
        return rc, buf.getvalue(), path

    # ---- 前置条件 ----

    def test_1_crash_leaves_delivered_on_disk(self):
        """崩溃点确实停在 DELIVERED，且磁盘快照里没有卡片正文。"""
        disk = self.crash_after_delivered()
        self.assertEqual(self.saves_after_delivery, 2,
                         "投递成功后应恰好两次落盘尝试：DELIVERED 成功、COMMITTED 失败")
        self.assertEqual(len(self.snapshots), self.save_calls - 1,
                         "失败那次不得留下快照")

        # 快照必须来自真实序列化：能反解、结构完整，不是测试自己拼的字符串
        raw = self.snapshots[-1]
        self.assertIsInstance(disk, dict, "快照必须是可反解的 JSON 对象")
        self.assertEqual(set(disk), set(self.state),
                         "快照顶层字段必须与内存 state 一致")

        events = disk["events"]
        self.assertEqual(list(events), [self.stub_key], "崩溃前不得出现第二条记录")
        rec = events[self.stub_key]
        self.assertEqual(rec["delivery_status"], store.DELIVERED)
        self.assertEqual(rec.get("pending_text"), "",
                         "DELIVERED 时 pending_text 必须已被 mark_delivered 清空")
        self.assertTrue(rec.get("telegram_message_id"),
                        "DELIVERED 必须带着 message_id，恢复才有依据")

        # 字段为空还不够——正文可能被塞进别的字段。直接搜整份快照。
        for leaked in (ORIGINAL_TITLE,
                       "阿森纳主帅确认引进 C 罗的计划",
                       "两家独立媒体交叉印证",
                       "酋长球场夜景"):
            self.assertNotIn(leaked, raw,
                             "磁盘快照里出现了卡片正文片段：{}".format(leaked))

    # ---- 恢复 ----

    def test_2_restart_commits_without_resending(self):
        """重启后由真实恢复入口补提交，且不重发。"""
        self.crash_after_delivered()
        before = len(self.telegram_calls)
        self.assertEqual(before, 1)

        rc, out, path = self.restart_and_recover()

        self.assertEqual(rc, 0)
        self.assertIn("[RECOVER] COMMIT_ONLY", out,
                      "恢复入口必须识别出「已送达、只需补提交」")
        self.assertEqual(len(self.telegram_calls), before,
                         "恢复过程不得重新发送任何 Telegram 消息")

        after = store.load_state(path)
        events = after.get("events") or {}
        self.assertEqual(list(events), [self.stub_key],
                         "恢复后仍只能有原随机键，不得出现 SHA1 记录")
        self.assertFalse(store._looks_like_derived_id(self.stub_key))

        rec = events[self.stub_key]
        self.assertEqual(rec["delivery_status"], store.COMMITTED,
                         "恢复后必须补提交到 COMMITTED")
        self.assertEqual(rec.get("pending_text"), "",
                         "恢复后不得留下卡片正文")
        self.assertEqual(store.public_review_violations(after), [])

        # 恢复后重新写盘的字节里同样不得出现正文片段
        with open(path, "r", encoding="utf-8") as f:
            final_text = f.read()
        for leaked in (ORIGINAL_TITLE, "阿森纳主帅确认引进 C 罗的计划",
                       "酋长球场夜景"):
            self.assertNotIn(leaked, final_text,
                             "恢复后的 state.json 出现了卡片正文片段：{}".format(leaked))

    def test_3_recovery_is_idempotent(self):
        """已恢复的记录再启动一次，不得再次进入恢复队列。"""
        self.crash_after_delivered()
        rc, _out, path = self.restart_and_recover()
        self.assertEqual(rc, 0)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc2 = bot.main()
        self.assertEqual(rc2, 0)
        self.assertNotIn("[RECOVER]", buf.getvalue(),
                         "已恢复的记录不得再次进入恢复队列")
        self.assertEqual(len(self.telegram_calls), 1, "第二次启动同样不得重发")
        self.assertEqual(
            store.load_state(path)["events"][self.stub_key]["delivery_status"],
            store.COMMITTED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
