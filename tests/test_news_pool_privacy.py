"""PR #1 整改 P0：公开新闻池与审核状态的**关联泄露**。

审查发现
--------
`_merge_pool()` 在 AI 审核分类**之前**就把 RSS 标题、URL、描述写进了公开
`state.json` 的 `news_pool`，而审核记录的公开键是：

    event_id = sha1(cleaned_url)[:12]

输入是**已经公开**的新闻池 URL，函数是纯函数。于是任何拿到公开 state.json
的人都能自己算一遍：

    新闻池里的 URL  →  sha1(clean_url)[:12]  →  在 state["events"] 里
                                                精确匹配到
                                                delivery_status=PENDING_REVIEW

泄露的不是内容，是「**哪一条**我们抓到了但压着没发」。这比 payload 泄露更
隐蔽：不需要看到任何一个字，只需要「这一条被挂起了」这个事实，就等于公开
宣布「我们手上有未核实的独家」。

修复
----
让公开 id 与 URL 之间**不存在任何可计算的映射**：审核记录的公开键改成随机值
（`state_store.new_review_public_id()`），公开 stub 里也不再写 `event_id`。
新闻池一个字都不删——删掉反而更糟（见 `_merge_pool` 的说明）。

本文件验证的正是这条链路已经断开，而且没有把该保留的东西一起弄坏：
  · 普通新闻照常进池，早报素材不受影响
  · 同一事件不会反复入队（去重靠私有存储的唯一索引）
  · 公开序列化结果里没有任何「URL → 审核标记」的可重算路径
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bot
import evidence as ev
import review_store
import state_store as store

# 一条真实结构的 RSS 候选：字段与 `fetch_rss_sources()` 的产出一致
RSS_URL = "https://www.thesun.co.uk/sport/football/ronaldo-portugal-retirement"
RSS_TITLE = "Ronaldo retires from Portugal duty"
RSS_DESC = "Cristiano Ronaldo has decided to end his international career."
ORDINARY_URL = "https://www.bbc.co.uk/sport/football/ordinary-match-report"
ORDINARY_TITLE = "Arsenal beat Chelsea in London derby"


def rss_item(url=RSS_URL, title=RSS_TITLE, desc=RSS_DESC, source="The Sun"):
    """真实结构的 RSS 条目（与 `_merge_pool` 读取的字段一一对应）。"""
    return {
        "title": title,
        "link": url,
        "desc": desc,
        "source": source,
        "feed_origin": source,
        "publisher": "thesun.co.uk",
        "published": "2026-11-05T20:00:00+00:00",
    }


def scan_event_for(item, importance="S", cred="E4"):
    """按 `run_scan_mode()` 的构造方式还原一个送审事件。"""
    evidence_url = item["link"]
    evidence_key = bot.clean_url(evidence_url)
    article_title = item["title"]
    return {
        "event_id": store.event_id_for(evidence_key or evidence_url, article_title),
        "title": article_title,
        "importance": importance,
        "cred": cred,
        "content_type": "BREAKING_NEWS",
        "event_type": "TRANSFER",
        "fact_summary": "球员已在个人账号宣布退出国家队。",
        "evidence_status": ev.UNVERIFIED,
        "evidence_url": evidence_url,
        "evidence_refs": [{"url": evidence_url, "title": article_title}],
    }


def review_stubs(state):
    """公开 state 里所有审核记录（连同它们的键）。"""
    out = []
    for key, rec in (state.get("events") or {}).items():
        if isinstance(rec, dict) and store.is_review_record(rec):
            out.append((key, rec))
    return out


class _PoolCase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._saved_tg = (bot.TELEGRAM_BOT_TOKEN, bot.TELEGRAM_CHAT_ID,
                          bot.TELEGRAM_REVIEW_CHAT_ID)
        bot.TELEGRAM_BOT_TOKEN = "TEST_TOKEN"
        bot.TELEGRAM_CHAT_ID = "123456"
        self.client = review_store.LocalReviewStore(":memory:")
        bot.set_review_store(self.client)
        self.state = store._default_state()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def tearDown(self):
        bot.set_review_store(None)
        self.client.close()
        (bot.TELEGRAM_BOT_TOKEN, bot.TELEGRAM_CHAT_ID,
         bot.TELEGRAM_REVIEW_CHAT_ID) = self._saved_tg
        super().tearDown()

    # ---- 走完「新闻池 → 分类 → 入队 → 落盘」的完整链路 ----

    def run_full_cycle(self):
        item = rss_item()
        bot._merge_pool(self.state, [item])          # ① 新闻池先落（早报素材）
        event = scan_event_for(item)
        route, reason = bot._review_route(event)      # ② E4 + S 分类
        self.assertEqual(route, "REVIEW", "前置条件：该候选必须被判为需人工审核")
        view = bot._enqueue_review(self.state, event, reason)   # ③ 入队
        self.assertIsNotNone(view, "前置条件：必须能入队")
        return item, event, view

    def save_and_read(self):
        path = os.path.join(self.tmp, "state.json")
        store.save_state_atomic(self.state, path)
        with open(path, encoding="utf-8") as f:
            return path, f.read()


# =============================================================================
# 1. 核心：公开新闻池里的 URL 无法与审核标记建立任何关联
# =============================================================================
class TestNoUrlToReviewLink(_PoolCase):
    def test_derived_id_never_matches_a_review_record(self):
        """★ 直接复现审查发现的攻击手法：自己算一遍 sha1，看能不能撞上。

        这条断言在修复前**必然失败**——那时的键就是 `event_id_for(url)`。
        """
        item, _event, _view = self.run_full_cycle()
        derived = store.event_id_for(bot.clean_url(item["link"]), item["title"])
        keys = [k for k, _ in review_stubs(self.state)]
        self.assertNotIn(
            derived, keys,
            "公开 URL 的派生 id {} 命中了审核记录 —— 关联泄露".format(derived))

    def test_derived_id_absent_from_whole_public_state(self):
        """派生 id 不得出现在公开 state 的任何位置（不只是 events 的键）。"""
        item, _event, _view = self.run_full_cycle()
        derived = store.event_id_for(bot.clean_url(item["link"]), item["title"])
        _, text = self.save_and_read()
        self.assertNotIn(derived, text,
                         "派生 id 出现在公开 state.json 里，仍可被反推")

    def test_review_ref_is_not_derived_from_url(self):
        """句柄本身也不能是 URL 的函数——否则等于把键换个字段再泄一次。"""
        item, _event, view = self.run_full_cycle()
        ref = view["review_ref"]
        derived = store.event_id_for(bot.clean_url(item["link"]), item["title"])
        self.assertNotEqual(ref, derived)
        self.assertNotIn(derived, ref)
        self.assertNotIn(ref, derived)

    def test_public_keys_are_random_per_event(self):
        """两条不同事件的公开键互不相关，且都不等于各自的派生 id。"""
        item_a = rss_item()
        item_b = rss_item(url="https://www.mirror.co.uk/sport/other-story",
                          title="Another exclusive story about a manager")
        for it in (item_a, item_b):
            bot._merge_pool(self.state, [it])
            evt = scan_event_for(it)
            route, reason = bot._review_route(evt)
            self.assertEqual(route, "REVIEW")
            bot._enqueue_review(self.state, evt, reason)
        keys = [k for k, _ in review_stubs(self.state)]
        self.assertEqual(len(keys), 2)
        for it in (item_a, item_b):
            derived = store.event_id_for(bot.clean_url(it["link"]), it["title"])
            self.assertNotIn(derived, keys)

    def test_public_id_shape_is_opaque(self):
        """公开键必须是 `rv` + 16 位十六进制的随机串，不是 12 位哈希前缀。"""
        _item, _event, _view = self.run_full_cycle()
        for key, _rec in review_stubs(self.state):
            self.assertTrue(key.startswith("rv"), key)
            self.assertEqual(len(key), 18, key)
            self.assertNotEqual(len(key), 12, "12 位 = event_id_for 的形状")

    def test_stub_carries_no_event_id_field(self):
        """stub 里不得出现 event_id 字段（键随机了，字段还会泄一次）。"""
        _item, _event, _view = self.run_full_cycle()
        for _key, rec in review_stubs(self.state):
            self.assertNotIn("event_id", rec)


# =============================================================================
# 2. 标题 / 正文同样不得进入审核标记
# =============================================================================
class TestNoTitleInReviewMarker(_PoolCase):
    def test_stub_has_no_title(self):
        _item, _event, _view = self.run_full_cycle()
        for _key, rec in review_stubs(self.state):
            self.assertNotIn("title", rec)
            self.assertNotIn("article_title", rec)
            self.assertNotIn("fact_summary", rec)
            self.assertNotIn("evidence_url", rec)
            self.assertNotIn("evidence_refs", rec)

    def test_serialized_review_records_contain_no_news_text(self):
        """逐条检查序列化后的审核记录，确认没有正文残留。

        注意不能对整份 state.json 断言「不含标题」——标题本来就该在新闻池里
        （早报要用）。要证明的是「审核记录这条**记录本身**不含正文」。
        """
        _item, _event, _view = self.run_full_cycle()
        _, text = self.save_and_read()
        data = json.loads(text)
        for key, rec in data["events"].items():
            if not store.is_review_record(rec):
                continue
            blob = json.dumps(rec, ensure_ascii=False)
            self.assertNotIn(RSS_TITLE, blob, key)
            self.assertNotIn(RSS_DESC, blob, key)
            self.assertNotIn("thesun.co.uk", blob, key)

    def test_public_violations_empty(self):
        self.run_full_cycle()
        self.assertEqual(store.public_review_violations(self.state), [])


# =============================================================================
# 3. 普通新闻不受影响：新闻池照常工作
# =============================================================================
class TestOrdinaryNewsStillWorks(_PoolCase):
    def test_ordinary_item_stays_in_pool(self):
        """普通新闻必须照常进池，早报素材不能因为这次修复被削掉。"""
        bot._merge_pool(self.state, [rss_item(url=ORDINARY_URL,
                                              title=ORDINARY_TITLE)])
        pool = self.state["news_pool"]
        self.assertEqual(len(pool), 1)
        self.assertEqual(pool[0]["link"], ORDINARY_URL)
        self.assertEqual(pool[0]["title"], ORDINARY_TITLE)

    def test_reviewed_item_also_stays_in_pool(self):
        """送审的那条**也**留在池里。

        刻意不删：新闻池每轮都提交，URL 在上一版 commit 里已经公开，
        删掉只是让它「凭空少一条」——diff 里的消失比哈希撞上还显眼。
        而且删了就等于把早报素材一起扔掉。真正的修法是让 id 算不出来。
        """
        item, _event, _view = self.run_full_cycle()
        links = [p.get("link") for p in self.state["news_pool"]]
        self.assertIn(item["link"], links)

    def test_pool_feeds_morning_report(self):
        """池子仍然能被早报聚合读到。"""
        bot._merge_pool(self.state, [rss_item(url=ORDINARY_URL,
                                              title=ORDINARY_TITLE)])
        recent = bot._pool_recent(self.state, 24)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["link"], ORDINARY_URL)

    def test_non_review_route_creates_no_stub(self):
        """E2/A 级新闻不该进审核队列，也不该留下审核标记。"""
        item = rss_item(url=ORDINARY_URL, title=ORDINARY_TITLE)
        bot._merge_pool(self.state, [item])
        event = scan_event_for(item, importance="A", cred="E2")
        route, _reason = bot._review_route(event)
        self.assertEqual(route, "PUSH")
        self.assertEqual(review_stubs(self.state), [])

    def test_pool_dedupe_still_works(self):
        """同一 URL 重复抓取不会把池子撑大。"""
        bot._merge_pool(self.state, [rss_item(url=ORDINARY_URL,
                                              title=ORDINARY_TITLE)])
        added = bot._merge_pool(self.state, [rss_item(url=ORDINARY_URL,
                                                      title=ORDINARY_TITLE)])
        self.assertEqual(added, 0)
        self.assertEqual(len(self.state["news_pool"]), 1)


# =============================================================================
# 4. 去重：同一事件不会反复入队、不会长出第二条审核标记
# =============================================================================
class TestDedupPreserved(_PoolCase):
    def test_second_enqueue_reuses_the_same_stub(self):
        """同一事件第二轮再被抓到 → 复用同一条记录，不新增。"""
        item, event, first = self.run_full_cycle()
        again = bot._enqueue_review(self.state, scan_event_for(item), "E4 级 S 事件")
        self.assertIsNotNone(again)
        self.assertEqual(again["review_ref"], first["review_ref"],
                         "必须复用同一个句柄（私有存储的唯一索引）")
        self.assertTrue(again.get("_duplicate"), "重复入队必须被标记出来")
        self.assertEqual(len(review_stubs(self.state)), 1, "公开侧不得长第二条")

    def test_duplicate_flag_prevents_card_spam(self):
        """`_duplicate` 是「别再往审核群发一遍卡片」的信号。"""
        item, _event, first = self.run_full_cycle()
        self.assertFalse(first.get("_duplicate"))
        again = bot._enqueue_review(self.state, scan_event_for(item), "E4 级 S 事件")
        self.assertTrue(again.get("_duplicate"))

    def test_stub_blocks_rediscovery(self):
        """待审记录在 blocks_rediscovery 里返回 True，不会被当成新事件再推。"""
        _item, _event, _view = self.run_full_cycle()
        for _key, rec in review_stubs(self.state):
            self.assertTrue(store.blocks_rediscovery(rec))

    def test_dedup_survives_random_keys(self):
        """去重靠私有存储的 event_id 唯一索引，与公开键是否随机无关。"""
        item, event, first = self.run_full_cycle()
        # 私有存储里按事件 id 查得到同一条
        found = self.client.enqueue(bot._build_review_payload(
            scan_event_for(item), "E4 级 S 事件", "2026-11-05T20:00:00+00:00"))
        self.assertEqual(found["review_ref"], first["review_ref"])
        self.assertTrue(found.get("duplicate"))


# =============================================================================
# 5. 守卫本身有效：故意制造泄露，必须被拦下
# =============================================================================
class TestGuardCatchesPlantedLeak(_PoolCase):
    """如果守卫抓不到故意种下的泄露，它就只是一段装饰代码。"""

    def test_violation_detected_for_derived_key(self):
        item = rss_item()
        bot._merge_pool(self.state, [item])
        derived = store.event_id_for(bot.clean_url(item["link"]), item["title"])
        # 故意按修复前的方式种一条审核记录
        self.state["events"][derived] = {
            "delivery_status": store.PENDING_REVIEW,
            "review_ref": "0123456789abcdef",
        }
        violations = store.public_review_violations(self.state)
        self.assertTrue(violations, "守卫没有发现可反推的审核记录")
        self.assertTrue(any("关联泄露" in v for v in violations), violations)

    def test_save_is_refused_for_planted_leak(self):
        item = rss_item()
        bot._merge_pool(self.state, [item])
        derived = store.event_id_for(bot.clean_url(item["link"]), item["title"])
        self.state["events"][derived] = {
            "delivery_status": store.PENDING_REVIEW,
            "review_ref": "0123456789abcdef",
        }
        path = os.path.join(self.tmp, "state.json")
        with self.assertRaises(store.StateError) as ctx:
            store.save_state_atomic(self.state, path)
        self.assertIn("审核私有资料", str(ctx.exception))
        self.assertFalse(os.path.exists(path), "守卫触发时绝不能落盘")

    def test_violation_detected_when_pool_carries_review_field(self):
        """新闻池里出现审核私有字段也要拦（这是 P0 扩展的覆盖面）。"""
        self.state["news_pool"] = [dict(rss_item(), review_payload={"title": "x"})]
        violations = store.public_review_violations(self.state)
        self.assertTrue(any("news_pool" in v for v in violations), violations)

    def test_event_id_field_in_review_record_is_a_violation(self):
        """审核记录只要带上 event_id 字段，就算越界（哪怕键是随机的）。"""
        item = rss_item()
        bot._merge_pool(self.state, [item])
        derived = store.event_id_for(bot.clean_url(item["link"]), item["title"])
        _item, _event, view = self.run_full_cycle()
        stub = store.find_review_stub(self.state, view["review_ref"])
        stub["event_id"] = derived
        violations = store.public_review_violations(self.state)
        self.assertTrue(any("event_id" in v for v in violations), violations)


# =============================================================================
# 6. 历史 state 迁移：老的派生键必须被换掉
# =============================================================================
# =============================================================================
# 7. 端到端：真的跑一轮 scan，公开产物里没有任何可反推的痕迹
# =============================================================================
class TestFullScanCycleEndToEnd(_PoolCase):
    """前面的用例都是手工拼 state；这一条走完整的 `run_scan_mode`。

    只断言一件事：**跑完之后，所有会公开出去的东西（state.json 与
    Actions 日志）都不含那个可重算的派生 id。**
    """

    def _run_scan(self):
        import contextlib
        import datetime
        import io as _io
        from unittest import mock

        item = {
            "title": RSS_TITLE,
            "link": RSS_URL,
            "desc": RSS_DESC,
            "source": "The Sun",
            "published": datetime.datetime.now(datetime.timezone.utc),
        }
        model = {
            "has_valid_news": True,
            "var_rating": "⚡【闪电突袭·跟队独家】(Tier 2)",
            "var_reason": "跟队记者爆料",
            "event_title": RSS_TITLE,
            "event_time": "2小时前",
            "fact_summary": "球员已在个人账号宣布退出国家队。",
            "evidence_url": RSS_URL,
            "draft_a": "a", "draft_b": "b", "draft_c": "c",
            "cover_blueprint": "x", "image_prompt": "y", "banner_overlay_text": "z",
            "grounding_records": [{"url": RSS_URL, "title": RSS_TITLE}],
            "verification_status": "GEMINI_GROUNDED",
            "evidence_status": ev.UNVERIFIED,
        }

        def fake_finalize(result, records, **kw):
            # 绕开证据闸门，只把「E4 + S」这一条送到审核分支上。
            # 闸门本身（单一来源不推送）有独立的测试，不在本文件范围内。
            return True, dict(result, evidence_status=ev.UNVERIFIED), "", ""

        state_path = os.path.join(self.tmp, "state.json")
        log_path = os.path.join(self.tmp, "run.log")
        buf = _io.StringIO()
        saved = (bot.STATE_FILE, bot.LOG_FILE, bot.IMPORTANCE_SORT)
        bot.STATE_FILE, bot.LOG_FILE, bot.IMPORTANCE_SORT = state_path, log_path, True
        try:
            with mock.patch.object(bot, "fetch_rss_sources",
                                   return_value=([item], {})), \
                 mock.patch.object(bot, "evaluate_with_gemini",
                                   return_value=(model, None)), \
                 mock.patch.object(bot, "_finalize_result",
                                   side_effect=fake_finalize), \
                 mock.patch.object(bot, "send_telegram_ex",
                                   return_value=(True, [4242], "")), \
                 mock.patch.object(bot, "send_review_card", return_value=999), \
                 contextlib.redirect_stdout(buf):
                rc = bot.run_scan_mode(store._default_state(), bot._now_iso())
        finally:
            bot.STATE_FILE, bot.LOG_FILE, bot.IMPORTANCE_SORT = saved
        return rc, state_path, log_path, buf.getvalue()

    def test_scan_routes_to_review_without_publishing(self):
        rc, state_path, _log, _out = self._run_scan()
        self.assertEqual(rc, 0)
        with open(state_path, encoding="utf-8") as f:
            data = json.load(f)
        stubs = [(k, v) for k, v in data["events"].items()
                 if store.is_review_record(v)]
        self.assertEqual(len(stubs), 1, "E4+S 必须落成一条待审记录")
        self.assertTrue(stubs[0][0].startswith("rv"), stubs[0][0])

    def test_derived_id_absent_from_public_state_file(self):
        _rc, state_path, _log, _out = self._run_scan()
        derived = store.event_id_for(bot.clean_url(RSS_URL), RSS_TITLE)
        with open(state_path, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn(derived, text, "公开 state.json 里出现了可反推的派生 id")

    def test_derived_id_absent_from_actions_log(self):
        """Actions 日志是公开的：打出来等于换个渠道再泄一次。"""
        _rc, _state_path, log_path, out = self._run_scan()
        derived = store.event_id_for(bot.clean_url(RSS_URL), RSS_TITLE)
        self.assertNotIn(derived, out, "stdout / ::notice 里出现了派生 id")
        with open(log_path, encoding="utf-8") as f:
            self.assertNotIn(derived, f.read(), "遥测日志里出现了派生 id")

    def test_reviewed_article_still_in_pool(self):
        _rc, state_path, _log, _out = self._run_scan()
        with open(state_path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn(RSS_URL, [p.get("link") for p in data["news_pool"]])

    def test_saved_state_passes_public_guard(self):
        _rc, state_path, _log, _out = self._run_scan()
        with open(state_path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(store.public_review_violations(data), [])


# =============================================================================
# 6. 历史 state 迁移：老的派生键必须被换掉
# =============================================================================
class TestLegacyStateMigration(unittest.TestCase):
    def test_legacy_derived_key_is_rekeyed(self):
        """历史版本用 sha1(url)[:12] 当键，载入时必须换成随机键。"""
        url = RSS_URL
        derived = store.event_id_for(bot.clean_url(url), RSS_TITLE)
        state = store._default_state()
        state["news_pool"] = [rss_item()]
        state["events"] = {derived: {
            "delivery_status": store.PENDING_REVIEW,
            "review_ref": "0123456789abcdef",
            "created_at": "2026-11-05T20:00:00+00:00",
            "updated_at": "2026-11-05T20:00:00+00:00",
        }}
        store.ensure_shape(state)
        keys = list(state["events"].keys())
        self.assertEqual(len(keys), 1)
        self.assertNotEqual(keys[0], derived, "派生键必须被换掉")
        self.assertTrue(keys[0].startswith("rv"), keys[0])
        self.assertEqual(store.public_review_violations(state), [])

    def test_migration_keeps_handle_and_status(self):
        derived = store.event_id_for(bot.clean_url(RSS_URL), RSS_TITLE)
        state = store._default_state()
        state["events"] = {derived: {
            "delivery_status": store.PENDING_REVIEW,
            "review_ref": "0123456789abcdef",
        }}
        store.ensure_shape(state)
        rec = list(state["events"].values())[0]
        self.assertEqual(rec["review_ref"], "0123456789abcdef",
                         "换键不能动句柄，否则审核动作找不到记录")
        self.assertEqual(rec["delivery_status"], store.PENDING_REVIEW)

    def test_migration_is_idempotent(self):
        derived = store.event_id_for(bot.clean_url(RSS_URL), RSS_TITLE)
        state = store._default_state()
        state["events"] = {derived: {
            "delivery_status": store.PENDING_REVIEW,
            "review_ref": "0123456789abcdef",
        }}
        store.ensure_shape(state)
        first = json.dumps(state["events"], sort_keys=True)
        store.ensure_shape(state)
        self.assertEqual(json.dumps(state["events"], sort_keys=True), first)


if __name__ == "__main__":
    unittest.main()
