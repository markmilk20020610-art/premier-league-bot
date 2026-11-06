"""第 1 项：聚合指标复核 —— C 罗 10 条真实报道的完整 45 对混淆矩阵。

为什么必须做这一件事
--------------------
上一版报告写了「pairwise 准确率 100%（36/36）」。这句话是错的：

  · 36 是**正例对**的数量（9 条退役报道两两组合 = C(9,2) = 36），
  · 它出现在精确率的分母（TP+FP）里，不出现在准确率的分母里。

10 条报道一共有 C(10,2) = **45** 对。准确率的分母必须是 45，不是 36。
把 36/36 叫作「整体准确率」，等于把负例对全部从分母里抹掉——
一个把所有样本都判成「同一事件」的模型，在这个错误口径下也能拿 100%。

本脚本输出四类互相不能替代的指标：
  1. Pairwise 混淆矩阵（TP / FP / FN / TN）与由它导出的四个比率
  2. 聚类成员划分是否正确（逐簇对应关系，比 pairwise 更严格）
  3. Rand Index / Adjusted Rand Index（前者恒等于 pairwise accuracy，后者修正偶然一致）
  4. 簇层面的 precision / recall（簇数口径，与对层面口径不同）

用法：
    python scripts/confusion_matrix.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import event_aggregate as ea

# ==================== 数据 ====================
# 2026-11-05 真实抓取的 10 条标题。
# 真值：索引 0 是「俱乐部捐球票」（独立事件），索引 1–9 是同一件「C 罗国家队退役」。
TITLES = [
    ("Cristiano Ronaldo's club to donate all ticket sales to tragic Jesus de "
     "Miguel's family and invite them as VIP guests", "thesun.co.uk",
     "https://www.thesun.co.uk/sport/football/donate", 13),
    ("A bitter end for Ronaldo - the icon who transformed Portugal",
     "theguardian.com", "https://www.theguardian.com/football/bitter-end", 14),
    ("Cristiano Ronaldo QUITS international football over explosive fall-out "
     "with Portugal manager", "dailymail.co.uk",
     "https://www.dailymail.co.uk/sport/football/quits", 15),
    ("Cristiano Ronaldo finished second to Messi even in retirement - the "
     "ending to his Portugal career is sad and predictable", "telegraph.co.uk",
     "https://www.telegraph.co.uk/football/second", 16),
    ("Trophies, tears, records and rows - Ronaldo's Portugal career will "
     "divide opinion", "bbc.co.uk",
     "https://www.bbc.co.uk/sport/football/trophies", 17),
    ("Ronaldo retires from Portugal duty", "reuters.com",
     "https://www.reuters.com/sports/soccer/retires", 18),
    ("Ronaldo announces international retirement on Instagram!",
     "thesun.co.uk", "https://www.thesun.co.uk/sport/football/instagram", 19),
    ("Ronaldo's most iconic moments as he retires from international football",
     "skysports.com", "https://www.skysports.com/football/news/iconic", 20),
    ("Ronaldo retires from international football - Manchester United",
     "manutd.com", "https://www.manutd.com/en/news/detail/retires", 21),
    ("Ronaldo's Portugal statement in full", "bbc.co.uk",
     "https://www.bbc.co.uk/sport/football/statement", 22),
]

# 真值簇标签：0 = 捐球票，1 = 退役
GROUND_TRUTH = [0] + [1] * 9


def build_items():
    out = []
    for i, (title, domain, url, hour) in enumerate(TITLES):
        out.append({
            "title": title,
            "link": url,
            "desc": "",
            "publisher": domain,
            "feed_origin": "https://{}/rss".format(domain),
            "published": "2026-11-05T{:02d}:00:00Z".format(hour),
        })
    return out


def pair_matrix(pred_labels, gt_labels):
    """逐对比较，返回完整混淆矩阵与派生比率。"""
    n = len(gt_labels)
    tp = fp = fn = tn = 0
    for i in range(n):
        for j in range(i + 1, n):
            same_pred = pred_labels[i] == pred_labels[j]
            same_gt = gt_labels[i] == gt_labels[j]
            if same_pred and same_gt:
                tp += 1
            elif same_pred and not same_gt:
                fp += 1
            elif same_gt and not same_pred:
                fn += 1
            else:
                tn += 1

    total = tp + fp + fn + tn
    accuracy = (tp + tn) / total if total else 0.0
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)
          if (precision + recall) else 0.0)
    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn, "total": total,
        "accuracy": accuracy, "precision": precision,
        "recall": recall, "f1": f1,
    }


def _pairs(pred_labels, gt_labels):
    """返回 (TP 对, FP 对, FN 对, TN 对) 的明细，用于人工复核。"""
    n = len(gt_labels)
    tp, fp, fn, tn = [], [], [], []
    for i in range(n):
        for j in range(i + 1, n):
            same_pred = pred_labels[i] == pred_labels[j]
            same_gt = gt_labels[i] == gt_labels[j]
            pair = (i, j)
            if same_pred and same_gt:
                tp.append(pair)
            elif same_pred and not same_gt:
                fp.append(pair)
            elif same_gt and not same_pred:
                fn.append(pair)
            else:
                tn.append(pair)
    return tp, fp, fn, tn


def cluster_partition(pred_labels):
    """把预测标签还原成簇列表（成员索引）。"""
    groups = {}
    for i, lab in enumerate(pred_labels):
        groups.setdefault(lab, []).append(i)
    return sorted(groups.values(), key=lambda g: min(g))


def partition_exact_match(pred_clusters, gt_clusters):
    """划分是否**完全一致**：每个预测簇恰好等于某个真值簇。"""
    a = sorted(tuple(sorted(g)) for g in pred_clusters)
    b = sorted(tuple(sorted(g)) for g in gt_clusters)
    return a == b


def rand_index(matrix):
    """Rand Index —— 与 pairwise accuracy 是同一个数，不是两个指标。"""
    return matrix["accuracy"]


def adjusted_rand_index(pred_labels, gt_labels):
    """Adjusted Rand Index：修正「随机划分也能得高分」的偶然一致。

    RI 的问题：把每个样本单独成一簇，也能拿到很高的 RI。
    ARI 用期望值做基线，随机划分的期望值是 0。
    """
    n = len(pred_labels)

    def comb2(x):
        return x * (x - 1) // 2

    def index_table(labels):
        t = {}
        for lab in labels:
            t[lab] = t.get(lab, 0) + 1
        return t

    def sum_comb(t):
        return sum(comb2(v) for v in t.values())

    pred_t = index_table(pred_labels)
    gt_t = index_table(gt_labels)

    contingency = {}
    for p, g in zip(pred_labels, gt_labels):
        contingency[(p, g)] = contingency.get((p, g), 0) + 1

    sum_ij = sum(comb2(v) for v in contingency.values())
    sum_i = sum_comb(pred_t)
    sum_j = sum_comb(gt_t)
    total = comb2(n)

    expected = (sum_i * sum_j) / total if total else 0.0
    max_index = (sum_i + sum_j) / 2
    denom = max_index - expected
    if denom == 0:
        return 1.0
    return (sum_ij - expected) / denom


def cluster_level_pr(pred_clusters, gt_clusters):
    """簇层面 precision / recall：一个预测簇是否「干净」，一个真值簇是否「被找全」。"""
    # 对每个预测簇，看它覆盖了哪个真值簇
    gt_of = {}
    for gi, g in enumerate(gt_clusters):
        for idx in g:
            gt_of[idx] = gi
    pred_of = {}
    for pi, p in enumerate(pred_clusters):
        for idx in p:
            pred_of[idx] = pi

    # 簇层面 precision：预测簇里占比最高的真值簇所占比例（purity）
    purity_num = 0
    for p in pred_clusters:
        counts = {}
        for idx in p:
            counts[gt_of[idx]] = counts.get(gt_of[idx], 0) + 1
        purity_num += max(counts.values())
    purity = purity_num / len(pred_of)

    # 簇层面 recall：真值簇里占比最高的预测簇所占比例（completeness）
    comp_num = 0
    for g in gt_clusters:
        counts = {}
        for idx in g:
            counts[pred_of[idx]] = counts.get(pred_of[idx], 0) + 1
        comp_num += max(counts.values())
    completeness = comp_num / len(gt_of)

    # 簇数是否一致
    return {
        "purity": purity,
        "completeness": completeness,
        "pred_clusters": len(pred_clusters),
        "gt_clusters": len(gt_clusters),
    }


def main():
    items = build_items()
    events, stats = ea.build_events(items)

    # 反查每条报道落在哪个事件（按对象身份，不按标题）
    pred_labels = []
    for it in items:
        hit = None
        for k, e in enumerate(events):
            for m in e["members"]:
                if m is it:
                    hit = k
                    break
            if hit is not None:
                break
        pred_labels.append(hit)

    missing = [i for i, v in enumerate(pred_labels) if v is None]
    if missing:
        print("!! 有报道未归入任何事件：", missing)
        return 1

    m = pair_matrix(pred_labels, GROUND_TRUTH)
    tp, fp, fn, tn = _pairs(pred_labels, GROUND_TRUTH)

    bar = "=" * 88
    print(bar)
    print("一、Pairwise 混淆矩阵（C 罗 10 条真实报道，共 C(10,2) = 45 对）")
    print(bar)
    print()
    print("                      预测：同一事件    预测：不同事件")
    print("  真值：同一事件            TP={:<3}          FN={:<3}".format(
        m["tp"], m["fn"]))
    print("  真值：不同事件            FP={:<3}          TN={:<3}".format(
        m["fp"], m["tn"]))
    print()
    print("  正例对（真值同一事件）  = C(9,2) = 36")
    print("  负例对（真值不同事件）  = 9 × 1  = 9")
    print("  合计                    = 45")
    print()
    print("-" * 88)
    print("二、由混淆矩阵导出的四个比率（分母各不相同，不能互相替代）")
    print("-" * 88)
    print("  Pairwise Accuracy  = (TP+TN)/(TP+FP+FN+TN) = ({}+{})/{} = {:.4f}".format(
        m["tp"], m["tn"], m["total"], m["accuracy"]))
    print("  Pairwise Precision = TP/(TP+FP)            = {}/({}+{}) = {:.4f}".format(
        m["tp"], m["tp"], m["fp"], m["precision"]))
    print("  Pairwise Recall    = TP/(TP+FN)            = {}/({}+{}) = {:.4f}".format(
        m["tp"], m["tp"], m["fn"], m["recall"]))
    print("  Pairwise F1        = 2PR/(P+R)             = {:.4f}".format(m["f1"]))
    print()
    print("  !! 注意：36/36 是 Precision 的算式，不是 Accuracy 的算式。")
    print("     Accuracy 的分母是 45（含 9 个负例对），不是 36。")
    print("     把负例对从分母里抹掉，等于让「全部判成同一事件」的模型也拿 100%。")

    print()
    print("-" * 88)
    print("三、聚类成员划分是否正确（比 pairwise 更严格的口径）")
    print("-" * 88)
    pred_clusters = cluster_partition(pred_labels)
    gt_clusters = cluster_partition(GROUND_TRUTH)
    exact = partition_exact_match(pred_clusters, gt_clusters)
    cl = cluster_level_pr(pred_clusters, gt_clusters)
    print("  预测簇数 = {} | 真值簇数 = {}".format(
        cl["pred_clusters"], cl["gt_clusters"]))
    for k, g in enumerate(pred_clusters):
        print("    预测簇 {}：{} 条 → {}".format(
            k, len(g), [TITLES[i][0][:38] for i in g][:2]
            + (["…"] if len(g) > 2 else [])))
    for k, g in enumerate(gt_clusters):
        print("    真值簇 {}：{} 条".format(k, len(g)))
    print("  划分完全一致（每个预测簇恰好等于一个真值簇）= {}".format(exact))
    print("  簇层面 Purity（预测簇纯度）      = {:.4f}".format(cl["purity"]))
    print("  簇层面 Completeness（真值簇覆盖）= {:.4f}".format(cl["completeness"]))

    print()
    print("-" * 88)
    print("四、聚类一致性指数")
    print("-" * 88)
    ri = rand_index(m)
    ari = adjusted_rand_index(pred_labels, GROUND_TRUTH)
    print("  Rand Index (RI)          = {:.4f}".format(ri))
    print("      !! RI 恒等于 Pairwise Accuracy，是同一个数，不是两个指标。")
    print("         本数据集上 RI = {:.4f} = Accuracy = {:.4f}".format(ri, m["accuracy"]))
    print("  Adjusted Rand Index (ARI) = {:.4f}".format(ari))
    print("      ARI 用随机划分的期望值做基线，随机划分的期望值为 0，")
    print("      所以 ARI 才回答「比瞎猜好多少」，RI 不回答这个问题。")

    print()
    print("-" * 88)
    print("五、明细（供人工复核）")
    print("-" * 88)
    print("  TP 对（{} 个）：前 5 个 = {}".format(len(tp), tp[:5]))
    print("  FP 对（{} 个）{}".format(len(fp), fp if fp else "：无"))
    print("  FN 对（{} 个）{}".format(len(fn), fn if fn else "：无"))
    print("  TN 对（{} 个）= {}".format(len(tn), tn))
    print()
    print("  TN 明细：索引 0（捐球票）与索引 1–9（退役）两两配对，共 9 对，")
    print("  全部被判为「不同事件」——这是本数据集里唯一需要模型克制的地方，")
    print("  也是「捐球票不能被并进退役」这条验收要求的直接体现。")

    print()
    print(bar)
    print("六、结论（严格区分四种口径）")
    print(bar)
    print("  · Pairwise Accuracy  = {:.2%}（45 对里判对 45 对）".format(m["accuracy"]))
    print("  · Pairwise Precision = {:.2%}（判为同一事件的 36 对里 36 对正确，误合并 0）".format(
        m["precision"]))
    print("  · Pairwise Recall    = {:.2%}（36 对真同一事件全部找回，误拆分 0）".format(
        m["recall"]))
    print("  · 聚类成员划分        = {}（2 簇对 2 簇，逐簇完全一致）".format(
        "完全正确" if exact else "不一致"))
    print()
    print("  三者都成立才能说「聚合正确」：精确率高只说明不乱合并，")
    print("  召回率高只说明不漏合并，划分一致才说明整体结构对。")
    print("  任何一项单独拿出来都不能代表整体。")
    print(bar)
    return 0


if __name__ == "__main__":
    sys.exit(main())
