"""一键跑全部测试并输出汇总。

用法：
    python tests/run_all.py            # 全部
    python tests/run_all.py --fast     # 跳过真实 git 仓库测试（约 50s）
"""
import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

MODULES = [
    "tests.test_evidence",
    "tests.test_schema",
    "tests.test_state_store",
    "tests.test_ai_provider",
    "tests.test_ai_integration",
    "tests.test_pipeline",
    "tests.test_delivery_v12",
    "tests.test_morning_delivery",
    "tests.test_batch4_v12",
    "tests.test_batch5_v12",
    "tests.test_maintenance_v12",
    "tests.test_sport_scope",
    "tests.test_match_calendar",
    "tests.test_referee_intel",
    "tests.test_lineup_monitor",
    "tests.test_pl_api",
    "tests.test_competitor_watch",
    "tests.test_focus_tick",
    "tests.test_focus_keeper",
    "tests.test_git_commit",
]
FAST_SKIP = {"tests.test_git_commit"}


def main():
    fast = "--fast" in sys.argv
    modules = [m for m in MODULES if not (fast and m in FAST_SKIP)]

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for m in modules:
        suite.addTests(loader.loadTestsFromName(m))

    started = time.time()
    result = unittest.TextTestRunner(verbosity=2, stream=sys.stdout).run(suite)
    elapsed = time.time() - started

    print("\n" + "=" * 60)
    print(f"模块数 {len(modules)} | 用例 {result.testsRun} | "
          f"失败 {len(result.failures)} | 错误 {len(result.errors)} | 耗时 {elapsed:.1f}s")
    print("=" * 60)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
