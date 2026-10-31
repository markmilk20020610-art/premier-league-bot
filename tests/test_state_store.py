"""阶段 2.1 / 2.2 验收：原子持久化 + 投递状态机 + 恢复。"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import state_store as store


class TestAtomicSave(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "state.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_roundtrip(self):
        state = store.ensure_shape({"processed_urls": ["https://a.com/1"]})
        store.save_state_atomic(state, self.path)
        back = store.load_state(self.path)
        self.assertEqual(back["processed_urls"], ["https://a.com/1"])
        self.assertEqual(back["schema_version"], store.SCHEMA_VERSION)

    def test_missing_file_returns_default(self):
        state = store.load_state(os.path.join(self.dir, "nope.json"))
        self.assertEqual(state["processed_urls"], [])
        self.assertEqual(state["events"], {})

    def test_corrupted_file_raises(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write('{"processed_urls": [')      # 半截 JSON
        with self.assertRaises(store.StateError):
            store.load_state(self.path)

    def test_write_failure_raises(self):
        state = store.ensure_shape({})
        with mock.patch("state_store.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(store.StateError) as ctx:
                store.save_state_atomic(state, self.path)
        self.assertIn("disk full", str(ctx.exception))
        # 失败后不能留下半截文件
        self.assertFalse(os.path.exists(self.path))

    def test_verify_failure_raises(self):
        """落盘内容与内存不一致时必须报错，而不是静默成功。"""
        state = store.ensure_shape({"processed_urls": ["a"]})
        real_replace = os.replace

        def tampering_replace(src, dst):
            with open(src, "w", encoding="utf-8") as f:
                json.dump({"processed_urls": ["tampered"]}, f)
            real_replace(src, dst)

        with mock.patch("state_store.os.replace", side_effect=tampering_replace):
            with self.assertRaises(store.StateError) as ctx:
                store.save_state_atomic(state, self.path)
        self.assertIn("回读校验失败", str(ctx.exception))

    def test_unserializable_state_raises(self):
        with self.assertRaises(store.StateError):
            store.save_state_atomic({"bad": {1, 2, 3}}, self.path)


class TestDeliveryStateMachine(unittest.TestCase):
    def setUp(self):
        self.state = store.ensure_shape({})

    def test_happy_path(self):
        event = store.record_event(self.state, "https://bbc.co.uk/a", "Arsenal news", "MULTIPLE_INDEPENDENT_SOURCES")
        self.assertEqual(event["delivery_status"], store.DISCOVERED)
        store.transition(event, store.VERIFIED)
        store.begin_delivery(event)
        self.assertEqual(event["delivery_status"], store.PENDING_DELIVERY)
        self.assertEqual(event["attempts"], 1)
        store.mark_delivered(event, 12345)
        self.assertEqual(event["delivery_status"], store.DELIVERED)
        self.assertEqual(event["telegram_message_id"], 12345)
        store.transition(event, store.COMMITTED)
        self.assertEqual(event["delivery_status"], store.COMMITTED)

    def test_illegal_transition_rejected(self):
        event = store.record_event(self.state, "https://bbc.co.uk/a", "Arsenal news", "SINGLE_SOURCE")
        with self.assertRaises(store.StateError):
            store.transition(event, store.COMMITTED)      # 不能跳过投递直接提交

    def test_event_id_is_stable(self):
        a = store.event_id_for("https://bbc.co.uk/a?utm=1")
        b = store.event_id_for("https://bbc.co.uk/a?utm=1")
        self.assertEqual(a, b)
        self.assertEqual(len(a), 12)

    def test_recovery_commit_only(self):
        """Telegram 已发成功、但状态没提交 → 只补提交，绝不重发。"""
        event = store.record_event(self.state, "https://bbc.co.uk/a", "Arsenal news", "SINGLE_SOURCE")
        store.transition(event, store.VERIFIED)
        store.begin_delivery(event)
        store.mark_delivered(event, 999)

        actions = store.recovery_actions(self.state)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action"], "COMMIT_ONLY")

        store.apply_recovery(self.state)
        self.assertEqual(event["delivery_status"], store.COMMITTED)

    def test_recovery_uncertain_pending(self):
        """卡在 PENDING_DELIVERY 且已尝试过 → 结果未知，不自动重发。"""
        event = store.record_event(self.state, "https://bbc.co.uk/b", "Chelsea news", "SINGLE_SOURCE")
        store.transition(event, store.VERIFIED)
        store.begin_delivery(event)      # attempts=1，随后进程被杀

        actions = store.recovery_actions(self.state)
        self.assertEqual(actions[0]["action"], "UNCERTAIN")
        store.apply_recovery(self.state)
        self.assertEqual(event["delivery_status"], store.DELIVERY_FAILED)
        self.assertIn("送达结果未知", event["last_error"])

    def test_delivered_event_not_resurrected(self):
        event = store.record_event(self.state, "https://bbc.co.uk/c", "Spurs news", "SINGLE_SOURCE")
        store.transition(event, store.VERIFIED)
        store.begin_delivery(event)
        store.mark_delivered(event, 1)
        store.transition(event, store.COMMITTED)
        self.assertEqual(store.recovery_actions(self.state), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
