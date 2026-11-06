"""第 4 项验收：IMPORTANCE_SORT 功能开关双模式。

要求：
  · 默认关闭
  · 关闭时走旧路径，且**新逻辑一次都不执行**
  · 开启时走新路径
  · 两种模式都能正常运行
  · 关闭新功能不影响旧版早报逻辑

实现方式：`IMPORTANCE_SORT` 是模块级常量，import 时定死，同进程改环境变量无效，
reload 又不可控。所以用子进程各跑一次 `scripts/switch_probe.py`，比对 JSON。

不改生产环境变量：子进程只带临时 env，不写任何 .env / 系统变量 / 仓库文件。
"""
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE = os.path.join(ROOT, "scripts", "switch_probe.py")


def run_probe(flag, extra_env=None):
    env = dict(os.environ)
    env["IMPORTANCE_SORT"] = str(flag)
    # 清掉可能干扰的外部变量，保证探针结果只由 flag 决定
    for k in ("FORCE_SUMMARY", "RUN_MODE", "TRUSTED_SINGLE_SOURCES"):
        env.pop(k, None)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, PROBE],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise AssertionError(
            "探针失败（IMPORTANCE_SORT={}）：\n{}\n{}".format(
                flag, proc.stdout[-2000:], proc.stderr[-2000:]))
    # 跳过 bot 的 [INFO] 日志，取最后一个 JSON 对象
    text = proc.stdout
    start = text.find("{")
    if start < 0:
        raise AssertionError("探针没有输出 JSON：\n" + text[-2000:])
    return json.loads(text[start:])


class TestSwitchOff(unittest.TestCase):
    """IMPORTANCE_SORT=0：旧路径。"""

    @classmethod
    def setUpClass(cls):
        cls.data = run_probe(0)

    def test_flag_is_off(self):
        self.assertFalse(self.data["importance_sort"])

    def test_new_path_never_called(self):
        # 这是「关闭后恢复原有选材路径」最硬的证据：
        # 不是「调用了但结果一样」，而是**一次都没调用**
        self.assertEqual(self.data["events_for_morning_called"], 0)

    def test_pool_untouched(self):
        self.assertEqual(self.data["final_order"], self.data["old_order"])

    def test_pool_size_unchanged(self):
        self.assertEqual(self.data["pool_count"], self.data["raw_count"])

    def test_no_event_ids_injected(self):
        self.assertFalse(self.data["has_event_ids"])

    def test_no_event_stats(self):
        self.assertNotIn("importance_sort", self.data["meta"])

    def test_old_path_keeps_promotions(self):
        # 旧版不看内容类型，推广照常进池——这正是要修的问题，
        # 但开关关闭时必须原样保留，否则「关闭」就不等于「回滚」
        self.assertGreater(self.data["promo_in_final"], 0)

    def test_stable_across_runs(self):
        again = run_probe(0)
        self.assertEqual(again["final_order"], self.data["final_order"])
        self.assertEqual(again["pool_count"], self.data["pool_count"])


class TestSwitchOn(unittest.TestCase):
    """IMPORTANCE_SORT=1：新路径。"""

    @classmethod
    def setUpClass(cls):
        cls.data = run_probe(1)

    def test_flag_is_on(self):
        self.assertTrue(self.data["importance_sort"])

    def test_new_path_called_once(self):
        self.assertEqual(self.data["events_for_morning_called"], 1)

    def test_pool_replaced(self):
        self.assertNotEqual(self.data["final_order"], self.data["old_order"])

    def test_promotions_filtered(self):
        self.assertEqual(self.data["promo_in_final"], 0)

    def test_event_ids_present(self):
        self.assertTrue(self.data["has_event_ids"])

    def test_event_stats_reported(self):
        stats = self.data["meta"].get("event_stats") or {}
        self.assertEqual(stats.get("input"), 33)
        self.assertGreaterEqual(stats.get("events", 0), 1)
        self.assertEqual(stats.get("low_value_selected"), 0)
        self.assertEqual(stats.get("global_slots_reserved"), 2)

    def test_a_line_coverage_kept(self):
        # 英超覆盖不能因为排序换口径而掉
        self.assertGreaterEqual(self.data["aline_in_final"], 9)


class TestDefaultIsOff(unittest.TestCase):
    def test_default_off_in_clean_env(self):
        env = dict(os.environ)
        env.pop("IMPORTANCE_SORT", None)
        proc = subprocess.run(
            [sys.executable, "-c",
             "import bot; print('FLAG=%s' % bot.IMPORTANCE_SORT)"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        self.assertIn("FLAG=False", proc.stdout)

    def test_truthy_values_accepted(self):
        for v in ("1", "true"):
            env = dict(os.environ)
            env["IMPORTANCE_SORT"] = v
            proc = subprocess.run(
                [sys.executable, "-c",
                 "import bot; print('FLAG=%s' % bot.IMPORTANCE_SORT)"],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
            self.assertIn("FLAG=True", proc.stdout, "值 {} 应被识别为开启".format(v))

    def test_falsy_values_ignored(self):
        for v in ("0", "off"):
            env = dict(os.environ)
            env["IMPORTANCE_SORT"] = v
            proc = subprocess.run(
                [sys.executable, "-c",
                 "import bot; print('FLAG=%s' % bot.IMPORTANCE_SORT)"],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
            self.assertIn("FLAG=False", proc.stdout, "值 {!r} 应被识别为关闭".format(v))


if __name__ == "__main__":
    unittest.main(verbosity=2)
