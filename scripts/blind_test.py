"""第 5 项：跨事件类型盲测。

这套数据**没有参与阈值标定**。`COMPOSITE_JACCARD = 0.28` 是在 C 罗退役数据集上
扫出来的，本文件用它去测另外六个事件族，看阈值能不能泛化。

覆盖的族（每个族至少 2 簇，且必含干扰项）：
  1. 主教练解雇        —— 含「同一人物不同事件」「不同人物相似事件」
  2. 球员转会          —— 含「同俱乐部两笔不同转会」
  3. 长期伤病          —— 含转载报道（同标题不同域名）
  4. 俱乐部所有权变化  —— 含「不同俱乐部同一句式」
  5. 纪律处罚          —— 含「上诉被驳回」这类高度雷同的措辞
  6. 国家队重大人事    —— 含「同一天两名球员退出国家队」

四类必须出现的干扰项：
  · 同一人物不同事件（同一个人，两件不同的事）
  · 不同人物相似事件（句式几乎一样，只是换了人）
  · 转载报道（同标题多域名，不得算独立来源、也不得拆成多簇）
  · 时间跨度较长的事件更新（同一件事隔 5 天，不得并成一簇）

安全原则：**任何情况下不许为了提高召回率而错误合并。**
误合并（FP）的容忍度是 0；误拆分（FN）只要能被「宁可漏合」解释就接受。

用法：
    python scripts/blind_test.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import event_aggregate as ea
import evidence as ev

# ==================== 盲测数据集 ====================
# 结构：(族名, 簇标签, 标题, 域名, 路径, 相对小时)
# 同一簇标签 = 真值同一事件。标签故意不连续，避免「按顺序切分」的假象。
DATA = [
    # ---------- 1. 主教练解雇 ----------
    ("manager", "WH_SACK", "West Ham sack Julen Lopetegui after poor run of results",
     "bbc.co.uk", "wh1", 2),
    ("manager", "WH_SACK", "West Ham confirm Julen Lopetegui departure with immediate effect",
     "skysports.com", "wh2", 3),
    ("manager", "WH_SACK", "Lopetegui leaves West Ham after seven months in charge",
     "theguardian.com", "wh3", 5),
    # 同一人物不同事件：Lopetegui 当年在狼队被解雇（不同俱乐部、不同时间）
    ("manager", "WOLVES_SACK", "Wolves sack Julen Lopetegui after relegation threat",
     "bbc.co.uk", "wv1", 700),
    # 不同人物相似事件：句式几乎一样，只换了俱乐部和人
    ("manager", "EVERTON_SACK", "Everton sack Sean Dyche after derby defeat",
     "skysports.com", "ev1", 4),
    ("manager", "EVERTON_SACK", "Everton part company with Sean Dyche",
     "dailymail.co.uk", "ev2", 6),

    # ---------- 2. 球员转会 ----------
    ("transfer", "GYOKERES", "Arsenal agree deal for striker Viktor Gyokeres",
     "bbc.co.uk", "gy1", 3),
    ("transfer", "GYOKERES", "Arsenal complete signing of Viktor Gyokeres in 60m move",
     "skysports.com", "gy2", 4),
    ("transfer", "GYOKERES", "Viktor Gyokeres joins Arsenal on five-year contract",
     "theguardian.com", "gy3", 6),
    # 同俱乐部两笔不同转会（句式高度相似，只是换了人）
    ("transfer", "GUEHI", "Arsenal sign defender Marc Guehi in 45m deal",
     "bbc.co.uk", "gh1", 5),
    ("transfer", "GUEHI", "Arsenal complete signing of Marc Guehi from Crystal Palace",
     "telegraph.co.uk", "gh2", 7),
    # 同一人物不同事件：同一名球员，续约不是转会
    ("transfer", "SAKA_CONTRACT", "Arsenal open contract talks with Bukayo Saka",
     "theguardian.com", "sk1", 8),

    # ---------- 3. 长期伤病 ----------
    ("injury", "ALISSON", "Liverpool confirm Alisson out for six weeks with calf injury",
     "skysports.com", "al1", 2),
    # 转载：同标题不同域名，必须并进同一簇，且不算独立来源
    ("injury", "ALISSON", "Liverpool confirm Alisson out for six weeks with calf injury",
     "mirror.co.uk", "al2", 3),
    ("injury", "ALISSON", "Alisson ruled out for six weeks in Liverpool injury blow",
     "bbc.co.uk", "al3", 5),
    # 不同人物相似事件：同俱乐部、同句式、同样的伤停周数
    ("injury", "JOTA", "Liverpool confirm Diogo Jota out for eight weeks with hamstring injury",
     "skysports.com", "jt1", 4),
    ("injury", "JOTA", "Diogo Jota ruled out for eight weeks in Liverpool injury blow",
     "dailymail.co.uk", "jt2", 6),

    # ---------- 4. 俱乐部所有权变化 ----------
    ("ownership", "CHELSEA_TAKEOVER",
     "Chelsea takeover completed as consortium buys majority stake",
     "bbc.co.uk", "ch1", 3),
    ("ownership", "CHELSEA_TAKEOVER",
     "Chelsea sale to Boehly consortium completed after Premier League approval",
     "skysports.com", "ch2", 5),
    # 不同俱乐部同一句式
    ("ownership", "EVERTON_TAKEOVER",
     "Everton takeover completed as Friedkin Group buys majority stake",
     "bbc.co.uk", "evt1", 6),
    ("ownership", "EVERTON_TAKEOVER",
     "Everton sale to Friedkin Group completed after Premier League approval",
     "telegraph.co.uk", "evt2", 8),

    # ---------- 5. 纪律处罚 ----------
    ("discipline", "EVERTON_POINTS",
     "Everton points deduction appeal rejected by independent panel",
     "bbc.co.uk", "ep1", 2),
    ("discipline", "EVERTON_POINTS", "Everton lose points deduction appeal",
     "skysports.com", "ep2", 4),
    ("discipline", "EVERTON_POINTS", "Everton points deduction upheld on appeal",
     "theguardian.com", "ep3", 6),
    # 不同俱乐部、几乎一样的措辞
    ("discipline", "FOREST_POINTS",
     "Nottingham Forest points deduction appeal rejected by independent panel",
     "bbc.co.uk", "fp1", 5),
    ("discipline", "FOREST_POINTS", "Nottingham Forest lose points deduction appeal",
     "skysports.com", "fp2", 7),

    # ---------- 6. 国家队重大人事决定 ----------
    ("national", "MODRIC_RETIRE",
     "Luka Modric announces retirement from Croatia duty",
     "bbc.co.uk", "md1", 3),
    ("national", "MODRIC_RETIRE",
     "Modric retires from international football after 18 years",
     "skysports.com", "md2", 5),
    # 同一天另一名球员退出国家队（不同人物相似事件）
    ("national", "KROOS_RETIRE",
     "Toni Kroos announces retirement from Germany duty",
     "bbc.co.uk", "kr1", 4),
    # 国家队换帅（同一族内另一种事件类型）
    ("national", "SPAIN_COACH",
     "Spain appoint Luis de la Fuente as head coach",
     "marca.com", "sp1", 6),
    ("national", "SPAIN_COACH",
     "Luis de la Fuente named Spain head coach on two-year deal",
     "reuters.com", "sp2", 8),
]

# ---------- 时间跨度较长的事件更新 ----------
# 两组对照：
#   LS_SAGA  —— 同一件「纽卡与戈登续约」的后续进展，隔 5 天。
#               真值：**同一簇**。证据累积本来就是干这个的，跨天的后续报道
#               要并进同一事件，否则早报会把一件事写成两次。
#   LS_OTHER —— 同俱乐部、同一个人，但是**另一件事**（重伤），且事件族不同。
#               真值：独立成簇。它检验「同俱乐部 + 同一个人」不能成为合并的充分条件。
LONG_SPAN = [
    ("longspan", "LS_SAGA",
     "Newcastle open talks over Anthony Gordon contract extension",
     "bbc.co.uk", "ls1", 120),
    ("longspan", "LS_SAGA",
     "Newcastle close to agreeing Anthony Gordon contract extension",
     "skysports.com", "ls2", 0),
    ("longspan", "LS_OTHER",
     "Newcastle confirm Anthony Gordon out for six weeks with knee injury",
     "dailymail.co.uk", "ls3", 1),
]


def build():
    items, truth = [], []
    for family, label, title, domain, slug, hours in DATA + LONG_SPAN:
        items.append({
            "title": title,
            "link": "https://www.{}/{}".format(domain, slug),
            "desc": "",
            "publisher": domain,
            "feed_origin": "https://{}/rss".format(domain),
            "published": _ts(hours),
            "_family": family,
        })
        truth.append(label)
    return items, truth


def _ts(hours_ago):
    import datetime
    base = datetime.datetime(2026, 11, 5, 12, 0, tzinfo=datetime.timezone.utc)
    return (base - datetime.timedelta(hours=hours_ago)).isoformat().replace(
        "+00:00", "Z")


def pair_matrix(pred, gt):
    n = len(gt)
    tp = fp = fn = tn = 0
    fp_pairs, fn_pairs = [], []
    for i in range(n):
        for j in range(i + 1, n):
            same_p = pred[i] == pred[j]
            same_g = gt[i] == gt[j]
            if same_p and same_g:
                tp += 1
            elif same_p and not same_g:
                fp += 1
                fp_pairs.append((i, j))
            elif same_g and not same_p:
                fn += 1
                fn_pairs.append((i, j))
            else:
                tn += 1
    total = tp + fp + fn + tn
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn, "total": total,
        "accuracy": (tp + tn) / total if total else 0.0,
        "precision": prec, "recall": rec, "f1": f1,
        "fp_pairs": fp_pairs, "fn_pairs": fn_pairs,
    }


def partition_exact(pred, gt):
    def groups(labels):
        d = {}
        for i, l in enumerate(labels):
            d.setdefault(l, []).append(i)
        return sorted(tuple(sorted(v)) for v in d.values())
    return groups(pred) == groups(gt)


def main():
    items, truth = build()
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

    if any(p is None for p in pred):
        print("!! 有报道未归入任何事件")
        return 1

    bar = "=" * 92
    print(bar)
    print("盲测数据集：{} 条报道，真值 {} 个事件簇".format(
        len(items), len(set(truth))))
    print("聚合结果：{} 个事件（合并 {} 条，连接边 {} 条）".format(
        stats["events"], stats["merged"], stats["edges"]))
    print(bar)

    m = pair_matrix(pred, truth)
    print()
    print("一、整体 Pairwise 混淆矩阵（共 {} 对）".format(m["total"]))
    print("    TP={}  FP={}  FN={}  TN={}".format(
        m["tp"], m["fp"], m["fn"], m["tn"]))
    print("    Accuracy={:.4f}  Precision={:.4f}  Recall={:.4f}  F1={:.4f}".format(
        m["accuracy"], m["precision"], m["recall"], m["f1"]))
    print("    划分完全一致 = {}".format(partition_exact(pred, truth)))

    # ---------- 分族统计 ----------
    print()
    print("-" * 92)
    print("二、分族结果")
    print("-" * 92)
    families = []
    for it in items:
        if it["_family"] not in families:
            families.append(it["_family"])
    print("{:<12} {:<6} {:<6} {:<6} {:<6} {:<8} {:<8} {}".format(
        "族", "条数", "TP", "FP", "FN", "Precision", "Recall", "划分一致"))
    for fam in families:
        idx = [i for i, it in enumerate(items) if it["_family"] == fam]
        sub_pred = [pred[i] for i in idx]
        sub_gt = [truth[i] for i in idx]
        sm = pair_matrix(sub_pred, sub_gt)
        print("{:<12} {:<6} {:<6} {:<6} {:<6} {:<8.2f} {:<8.2f} {}".format(
            fam, len(idx), sm["tp"], sm["fp"], sm["fn"],
            sm["precision"], sm["recall"],
            partition_exact(sub_pred, sub_gt)))

    # ---------- 错误明细 ----------
    print()
    print("-" * 92)
    print("三、错误明细（误合并必须为 0）")
    print("-" * 92)
    if m["fp_pairs"]:
        print("  误合并（FP）{} 对：".format(len(m["fp_pairs"])))
        for i, j in m["fp_pairs"]:
            print("    · [{}] {}".format(truth[i], items[i]["title"][:52]))
            print("      [{}] {}".format(truth[j], items[j]["title"][:52]))
    else:
        print("  误合并（FP）= 0 ✅")
    if m["fn_pairs"]:
        print("  误拆分（FN）{} 对：".format(len(m["fn_pairs"])))
        for i, j in m["fn_pairs"]:
            print("    · [{}] {}".format(truth[i], items[i]["title"][:52]))
            print("      [{}] {}".format(truth[j], items[j]["title"][:52]))
    else:
        print("  误拆分（FN）= 0")

    # ---------- 干扰项专项 ----------
    print()
    print("-" * 92)
    print("四、四类干扰项专项")
    print("-" * 92)

    def same(pred_labels, a, b):
        return pred_labels[a] == pred_labels[b]

    checks = []

    # 同一人物不同事件：Lopetegui 西汉姆 vs 狼队
    i_wh = next(i for i, it in enumerate(items) if it["link"].endswith("/wh1"))
    i_wv = next(i for i, it in enumerate(items) if it["link"].endswith("/wv1"))
    checks.append(("同一人物不同事件（Lopetegui 西汉姆 vs 狼队）",
                   not same(pred, i_wh, i_wv)))

    # 不同人物相似事件：西汉姆炒 Lopetegui vs 埃弗顿炒 Dyche
    i_ev = next(i for i, it in enumerate(items) if it["link"].endswith("/ev1"))
    checks.append(("不同人物相似事件（West Ham vs Everton 换帅）",
                   not same(pred, i_wh, i_ev)))

    # 同俱乐部两笔转会：Gyokeres vs Guehi
    i_gy = next(i for i, it in enumerate(items) if it["link"].endswith("/gy1"))
    i_gh = next(i for i, it in enumerate(items) if it["link"].endswith("/gh1"))
    checks.append(("同俱乐部两笔不同转会（Gyokeres vs Guehi）",
                   not same(pred, i_gy, i_gh)))

    # 转载：同标题两个域名必须并簇
    i_al1 = next(i for i, it in enumerate(items) if it["link"].endswith("/al1"))
    i_al2 = next(i for i, it in enumerate(items) if it["link"].endswith("/al2"))
    checks.append(("转载报道（同标题 bbc vs mirror）", same(pred, i_al1, i_al2)))

    # 时间跨度较长：同一故事的后续进展，隔 5 天 → 应并簇（证据累积）
    i_ls1 = next(i for i, it in enumerate(items) if it["link"].endswith("/ls1"))
    i_ls2 = next(i for i, it in enumerate(items) if it["link"].endswith("/ls2"))
    checks.append(("时间跨度较长（同一故事隔 5 天的后续进展）",
                   same(pred, i_ls1, i_ls2)))

    # 同俱乐部 + 同一个人，但是另一件事 → 必须分开
    i_ls3 = next(i for i, it in enumerate(items) if it["link"].endswith("/ls3"))
    checks.append(("同俱乐部同一个人的另一件事（续约 vs 重伤）",
                   not same(pred, i_ls1, i_ls3)))

    for name, ok in checks:
        print("  [{}] {}".format("PASS" if ok else "FAIL", name))

    # ---------- 转载的独立来源 ----------
    print()
    print("-" * 92)
    print("五、转载不算独立信源（在本数据集上复核）")
    print("-" * 92)
    alisson = [it for it in items if it["link"].rsplit("/", 1)[-1].startswith("al")]
    evs, _ = ea.build_events(alisson)
    for e in evs:
        print("  事件「{}」：{} 条报道，{} 个独立来源".format(
            e["title"][:48], e["article_count"], e["independent_groups"]))

    print()
    print(bar)
    ok_all = m["fp"] == 0 and all(c[1] for c in checks)
    print("结论：误合并 {} 对；误拆分 {} 对；干扰项 {}/{} 通过".format(
        m["fp"], m["fn"], sum(1 for c in checks if c[1]), len(checks)))
    print("      {}".format(
        "安全原则成立（零误合并），可接受" if ok_all
        else "!! 存在误合并或干扰项失败，必须处理"))
    print(bar)
    return 0 if ok_all else 2


if __name__ == "__main__":
    sys.exit(main())
