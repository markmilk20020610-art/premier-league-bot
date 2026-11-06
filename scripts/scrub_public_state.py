"""一次性迁移：把公开 state.json 里残留的审核资料清干净。

## 什么时候需要跑

V1.1 之前，`_enqueue_review` 会把完整审核快照（标题、事实摘要、原始报道标题与
链接、证据组合、审核意见、审核人）写进 `state["events"]`，而本仓库是 Public、
state.json 每轮都被提交。也就是说：**历史上真的可能已经泄露过。**

`state_store.load_state` 现在会在载入时自动 scrub，所以正常运行一轮就会清理。
但「等下一轮扫描」不够——如果那期间仓库被镜像、被缓存、被 fork，
旧提交里的内容依然在。所以提供这个脚本，用于：

  · 部署前主动清理并立刻提交（缩短暴露窗口）
  · 排查「到底泄了什么」时做一次只读盘点

## 用法

    # 只盘点，不写文件
    python scripts/scrub_public_state.py --check

    # 清理并写回（原子替换）
    python scripts/scrub_public_state.py --write

    # 清理另一个副本
    python scripts/scrub_public_state.py --path /tmp/state.json --write

## 安全约束（刻意的）

  · **只动审核相关字段**。其它事件记录的标题、链接、去重状态原样保留——
    静默删掉别的记录会破坏 processed_urls / events 的去重语义。
  · **保留 event_id 与 delivery_status**。审核记录的「这条已经审过了」这个事实
    必须留住，否则同一事件会被反复送审、反复发卡片。
  · **幂等**。重复执行结果一致。
  · 不联网、不推送、不提交。Git 操作交给人。
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import state_store as store  # noqa: E402


def audit(state):
    """盘点公开 state.json 里的审核泄漏面。不修改任何东西。"""
    events = state.get("events") or {}
    report = {
        "events_total": len(events),
        "review_records": 0,
        "records_with_payload": 0,
        "records_with_title": 0,
        "records_with_evidence": 0,
        "violations": [],
        "review_refs_missing": 0,
    }
    for eid, event in events.items():
        if not isinstance(event, dict):
            continue
        if store.is_review_record(event):
            report["review_records"] += 1
            if not event.get("review_ref"):
                report["review_refs_missing"] += 1
        if "review_payload" in event:
            report["records_with_payload"] += 1
        if store.is_review_record(event) and event.get("title"):
            report["records_with_title"] += 1
        if store.is_review_record(event) and (event.get("evidence_url")
                                              or event.get("evidence_refs")):
            report["records_with_evidence"] += 1
    report["violations"] = store.public_review_violations(state)
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description="清理公开 state.json 中的审核私有资料")
    ap.add_argument("--path", default=os.path.join(ROOT, "state.json"))
    ap.add_argument("--check", action="store_true", help="只盘点，不写文件（默认行为）")
    ap.add_argument("--write", action="store_true", help="清理并原子写回")
    args = ap.parse_args(argv)

    if not os.path.exists(args.path):
        print("[ERROR] 找不到 {}".format(args.path))
        return 2

    with open(args.path, "r", encoding="utf-8") as f:
        state = json.load(f)

    before = audit(state)
    print("=========== 迁移前盘点 ===========")
    print("事件总数：{}".format(before["events_total"]))
    print("审核记录数：{}".format(before["review_records"]))
    print("  含 review_payload：{}".format(before["records_with_payload"]))
    print("  含标题：{}".format(before["records_with_title"]))
    print("  含证据链接：{}".format(before["records_with_evidence"]))
    print("  缺 review_ref（私有存储句柄）：{}".format(before["review_refs_missing"]))
    print("越界字段总数：{}".format(len(before["violations"])))
    for v in before["violations"][:20]:
        print("  - {}".format(v))
    if len(before["violations"]) > 20:
        print("  ...（其余 {} 处省略）".format(len(before["violations"]) - 20))

    if not args.write:
        print("\n[CHECK] 只盘点，未修改文件。加 --write 才会清理。")
        return 0

    scrubbed = store.scrub_review_records(state)
    after = audit(state)
    print("\n=========== 清理结果 ===========")
    print("清理记录数：{}".format(scrubbed))
    print("清理后越界字段：{}".format(len(after["violations"])))
    if after["violations"]:
        print("[ERROR] 清理后仍有越界字段，拒绝写回：")
        for v in after["violations"][:10]:
            print("  - {}".format(v))
        return 1

    # 幂等自检：再跑一次不应再有任何变化
    again = store.scrub_review_records(state)
    if again != 0:
        print("[ERROR] 迁移不幂等：第二次仍改动了 {} 条".format(again))
        return 1

    store.save_state_atomic(state, args.path)
    print("已原子写回 {}".format(args.path))
    if after["review_refs_missing"]:
        print("\n[WARN] 有 {} 条审核记录缺 review_ref。".format(after["review_refs_missing"]))
        print("       它们的完整资料从未进入私有存储（旧版本只写了公开 state），")
        print("       清理后已不可恢复：需要人工确认是重新送审还是直接丢弃。")
    print("\n下一步（由人执行，脚本不碰 Git）：")
    print("  git add state.json && git commit -m 'chore(p0): 清理公开 state.json 中的审核私有资料'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
