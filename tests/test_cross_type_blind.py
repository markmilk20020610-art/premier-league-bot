"""第 5 项验收：跨事件类型盲测（固化为断言）。

数据源是 `scripts/blind_test.py`——同一份数据集，不复制粘贴，避免脚本和测试
各改一半。这份数据**没有参与阈值标定**：`COMPOSITE_JACCARD = 0.28` 是在
C 罗退役数据集上扫出来的，这里用它去测另外六个事件族。

必须守住的红线：
  · 误合并（FP）= 0。这是唯一不可妥协的指标。
  · 四类干扰项全部通过：同一人物不同事件 / 不同人物相似事件 /
    转载报道 / 时间跨度较长的事件更新。
  · 误拆分（FN）允许存在，但每一条都必须能解释成「宁可漏合」。
"""
import importlib.util
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import event_aggregate as ea
import evidence as ev


def _load_script():
    path = os.path.join(ROOT, "scripts", "blind_test.py")
    spec = importlib.util.spec_from_file_location("blind_test_data", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


blind = _load_script()


def run_blind():
    items, truth = blind.build()
    events, stats = ea.build_events(items)
    pred = []
    for it in items:
        hit = None
        for k, e in enumerate(events):
            for m in e["members"]:
                if m is it:
                    hit = k
                    break
            if hit is not None:
                break
        pred.append(hit)
    return items, truth, pred, events, stats


class BlindBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (cls.items, cls.truth, cls.pred,
         cls.events, cls.stats) = run_blind()
        cls.m = blind.pair_matrix(cls.pred, cls.truth)

    def idx(self, slug):
        return next(i for i, it in enumerate(self.items)
                    if it["link"].endswith("/" + slug))


class TestNoWrongMerge(BlindBase):
    """零误合并是安全原则的底线，任何情况下都不许破。"""

    def test_zero_false_positive(self):
        if self.m["fp"]:
            detail = "\n".join(
                "  {} || {}".format(self.items[i]["title"][:60],
                                    self.items[j]["title"][:60])
                for i, j in self.m["fp_pairs"])
            self.fail("出现 {} 对误合并：\n{}".format(self.m["fp"], detail))

    def test_precision_is_one(self):
        self.assertEqual(self.m["precision"], 1.0)

    def test_recall_reported(self):
        # 允许误拆分，但必须被记录在案，不能悄悄变差
        self.assertGreaterEqual(self.m["recall"], 0.9,
                                "召回跌破 0.9，需要复查是否阈值过严")


class TestInterferenceCases(BlindBase):
    """四类干扰项 + 两条同族干扰。"""

    def test_same_person_different_event(self):
        # Lopetegui 在西汉姆被解雇 vs 当年在狼队被解雇
        a, b = self.idx("wh1"), self.idx("wv1")
        self.assertNotEqual(self.pred[a], self.pred[b])

    def test_different_person_similar_event(self):
        # 西汉姆炒 Lopetegui vs 埃弗顿炒 Dyche：句式几乎一样
        a, b = self.idx("wh1"), self.idx("ev1")
        self.assertNotEqual(self.pred[a], self.pred[b])

    def test_same_club_two_transfers(self):
        a, b = self.idx("gy1"), self.idx("gh1")
        self.assertNotEqual(self.pred[a], self.pred[b])

    def test_reprint_merges(self):
        # 同标题两个域名：必须并簇
        a, b = self.idx("al1"), self.idx("al2")
        self.assertEqual(self.pred[a], self.pred[b])

    def test_long_span_same_story_merges(self):
        # 同一件续约隔 5 天的后续进展：应并簇（证据累积）
        a, b = self.idx("ls1"), self.idx("ls2")
        self.assertEqual(self.pred[a], self.pred[b])

    def test_long_span_different_event_splits(self):
        # 同俱乐部、同一个人，但是另一件事（续约 vs 重伤）
        a, b = self.idx("ls1"), self.idx("ls3")
        self.assertNotEqual(self.pred[a], self.pred[b])


class TestPerFamilyPartition(BlindBase):
    """分族检查：每个族内部划分必须完全正确。"""

    FAMILIES = ("manager", "transfer", "injury", "ownership",
                "discipline", "national", "longspan")

    def test_each_family_partition_exact(self):
        for fam in self.FAMILIES:
            with self.subTest(family=fam):
                idx = [i for i, it in enumerate(self.items)
                       if it["_family"] == fam]
                sub_pred = [self.pred[i] for i in idx]
                sub_gt = [self.truth[i] for i in idx]
                self.assertTrue(
                    blind.partition_exact(sub_pred, sub_gt),
                    "族 {} 的簇划分与真值不一致".format(fam))

    def test_each_family_zero_fp(self):
        for fam in self.FAMILIES:
            with self.subTest(family=fam):
                idx = [i for i, it in enumerate(self.items)
                       if it["_family"] == fam]
                sm = blind.pair_matrix([self.pred[i] for i in idx],
                                       [self.truth[i] for i in idx])
                self.assertEqual(sm["fp"], 0, "族 {} 出现误合并".format(fam))


class TestReprintIndependence(BlindBase):
    """转载在同一事件里不能算独立信源。"""

    def test_alisson_event_groups(self):
        alisson = [it for it in self.items
                   if it["link"].rsplit("/", 1)[-1].startswith("al")]
        evs, _ = ea.build_events(alisson)
        self.assertEqual(len(evs), 1)
        # 3 条报道（其中 2 条同标题转载）→ 2 个独立发布方，不是 3
        self.assertEqual(evs[0]["article_count"], 3)
        self.assertEqual(evs[0]["independent_groups"], 2)


class TestHardConflictStillAbsolute(BlindBase):
    """阈值可以按族调，硬冲突不行。"""

    def test_club_conflict_detected(self):
        a = ea._prep({"title": "West Ham sack Julen Lopetegui after poor run",
                      "link": "https://www.bbc.co.uk/x"})
        b = ea._prep({"title": "Wolves sack Julen Lopetegui after relegation",
                      "link": "https://www.skysports.com/y"})
        ok, why = ea._hard_conflict(a, b)
        self.assertTrue(ok)
        self.assertIn("俱乐部不同", why)

    def test_club_extraction_multiword(self):
        self.assertIn("westham", ea._clubs("West Ham sack the manager"))
        self.assertIn("manutd", ea._clubs("Manchester United appoint director"))
        self.assertIn("mancity", ea._clubs("Man City win the derby"))

    def test_sevilla_is_not_a_club_token(self):
        # "villa" 是 Aston Villa 的简称，但也是 Sevilla 的子串
        self.assertFalse(ea._clubs("Real Betis and Sevilla draw"))

    def test_same_club_no_conflict(self):
        a = ea._prep({"title": "Everton points deduction appeal rejected",
                      "link": "https://www.bbc.co.uk/a"})
        b = ea._prep({"title": "Everton lose points deduction appeal",
                      "link": "https://www.skysports.com/b"})
        ok, _ = ea._hard_conflict(a, b)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
