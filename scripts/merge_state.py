"""state.json 结构化合并（V1.2 第六批）。

为什么不再用 `git merge -X ours`
--------------------------------
旧脚本在 rebase 失败时执行 `git merge -X ours origin/main`，冲突一律保留本地。
对代码文件这没问题，对 state.json 是**数据丢失**：

  · 远端刚记下「某条新闻已送达」，本地这次运行不知道，merge 后这条记录消失
    → 下一轮把它当没发过，重复推送；
  · 远端刚写下今天的早报日期锁，本地 merge 掉 → 同一天再发一次早报。

两边都是「刚跑出来的真相」，谁也不能整份覆盖谁，只能**按语义合并**。

合并规则
--------
events            按 event_id 取并集；同 ID 取「投递进度更靠前」的那份，
                  进度相同取 updated_at 更晚的。已送达/已提交绝不被降级。
processed_urls    并集，保留最后 200 条。
news_pool         按链接取并集，按发布时间倒序截断。
日期锁            取更晚的日期（YYYY-MM-DD 字符串可直接比较）。
时间戳            取更晚的 ISO 时间。
ai_stats          同一天取 calls 更多的那份（不累加，累加会重复计数）。
circuit_breakers  按 provider 取 open_until 更晚的那份。

用法：
    python scripts/merge_state.py <本地 state.json> <远端 state.json>

合并结果写回本地文件；任一侧缺失时按另一侧原样保留。
"""

import json
import os
import sys

# 投递进度排序：数字越大表示越「靠后」，冲突时保留更大的
STATUS_RANK = {
    "DISCOVERED": 0,
    "VERIFIED": 1,
    "PENDING_DELIVERY": 2,
    "RETRYABLE_FAILURE": 2,
    "DELIVERY_FAILED": 3,
    "DELIVERY_UNCERTAIN": 4,
    "DELIVERED": 5,
    "COMMITTED": 6,
}

POOL_MAX_ITEMS = 150
PROCESSED_MAX = 200


def _rank(event):
    return STATUS_RANK.get((event or {}).get("delivery_status"), -1)


def _better_event(a, b):
    """两份同 ID 事件，返回更该保留的那份。"""
    if a is None:
        return b
    if b is None:
        return a
    ra, rb = _rank(a), _rank(b)
    if ra != rb:
        return a if ra > rb else b
    return a if (a.get("updated_at") or "") >= (b.get("updated_at") or "") else b


def _merge_chunks(a, b):
    """分段记录按 chunk_index 合并，已送达的段不允许被降级。"""
    merged = {}
    for rec in (a or []) + (b or []):
        if not isinstance(rec, dict):
            continue
        idx = rec.get("chunk_index")
        if idx is None:
            continue
        old = merged.get(idx)
        if old is None:
            merged[idx] = rec
            continue
        old_done = old.get("delivery_status") == "DELIVERED" and old.get("telegram_message_id")
        new_done = rec.get("delivery_status") == "DELIVERED" and rec.get("telegram_message_id")
        if new_done and not old_done:
            merged[idx] = rec
    return [merged[k] for k in sorted(merged)]


def _merge_event(a, b):
    winner = _better_event(a, b)
    loser = b if winner is a else a
    out = dict(winner)
    out["chunks"] = _merge_chunks((a or {}).get("chunks"), (b or {}).get("chunks"))
    # message_id / last_error 只要有一侧确认过就保留
    if not out.get("telegram_message_id"):
        for src in (a, b):
            if src and src.get("telegram_message_id"):
                out["telegram_message_id"] = src["telegram_message_id"]
                break
    if not out.get("article_title"):
        for src in (a, b):
            if src and src.get("article_title"):
                out["article_title"] = src["article_title"]
                break
    if not out.get("evidence_url"):
        for src in (a, b):
            if src and src.get("evidence_url"):
                out["evidence_url"] = src["evidence_url"]
                break
    return out


def _pool_key(item):
    return str((item or {}).get("link") or (item or {}).get("title") or "").strip().lower()


def _pool_time(item):
    return str((item or {}).get("published") or (item or {}).get("first_seen") or "")


def _merge_pool(a, b):
    """按链接去重，同链接保留发布时间更晚的那份。

    注意：替换必须写回 `out` 里对应的槽位。第一版只更新了 seen 字典，
    列表里留的还是旧对象，结果「更新的那份」被算进去了却没生效 ——
    早报窗口按发布时间过滤，等于把刚抓到的新条目当旧闻丢掉。
    """
    index, out = {}, []
    for item in list(a or []) + list(b or []):
        if not isinstance(item, dict):
            continue
        key = _pool_key(item)
        if not key:
            continue
        if key not in index:
            index[key] = len(out)
            out.append(item)
        elif _pool_time(item) > _pool_time(out[index[key]]):
            out[index[key]] = item
    out.sort(key=_pool_time, reverse=True)
    return out[:POOL_MAX_ITEMS]


def _later(a, b):
    a, b = str(a or ""), str(b or "")
    return a if a >= b else b


def _merge_ai_stats(a, b):
    a, b = a or {}, b or {}
    if not a:
        return b
    if not b:
        return a
    if (a.get("date") or "") != (b.get("date") or ""):
        return a if (a.get("date") or "") > (b.get("date") or "") else b
    # 同一天：取计数更大的那份，绝不累加（两边统计的是同一批调用）
    return a if int(a.get("calls") or 0) >= int(b.get("calls") or 0) else b


def _merge_breakers(a, b):
    a, b = dict(a or {}), b or {}
    for name, data in b.items():
        cur = a.get(name) or {}
        if (data.get("open_until") or "") > (cur.get("open_until") or ""):
            a[name] = data
    return a


def merge(local, remote):
    """把远端 state 合并进本地 state，返回新字典。"""
    if not local:
        return dict(remote or {})
    if not remote:
        return dict(local)

    out = dict(local)

    events = dict(remote.get("events") or {})
    for eid, event in (local.get("events") or {}).items():
        events[eid] = _merge_event(event, events.get(eid))
    out["events"] = events

    urls, seen = [], set()
    for url in list(local.get("processed_urls") or []) + list(remote.get("processed_urls") or []):
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    out["processed_urls"] = urls[-PROCESSED_MAX:]

    out["news_pool"] = _merge_pool(local.get("news_pool"), remote.get("news_pool"))
    out["last_morning_report_date"] = _later(local.get("last_morning_report_date"),
                                             remote.get("last_morning_report_date"))
    for field in ("last_summary_sent_at", "last_scan_at"):
        out[field] = _later(local.get(field), remote.get(field))
    out["ai_stats"] = _merge_ai_stats(local.get("ai_stats"), remote.get("ai_stats"))
    out["circuit_breakers"] = _merge_breakers(local.get("circuit_breakers"),
                                              remote.get("circuit_breakers"))
    out["schema_version"] = max(int(local.get("schema_version") or 0),
                                int(remote.get("schema_version") or 0))
    return out


def _load(path):
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[ERROR] 读取 {path} 失败：{type(e).__name__}: {e}")
        return None


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    local_path, remote_path = sys.argv[1], sys.argv[2]
    local, remote = _load(local_path), _load(remote_path)

    if local is None and remote is None:
        print("[ERROR] 本地与远端 state 都不可读，放弃合并")
        return 1
    if remote is None:
        print("[INFO] 远端没有 state.json，保留本地")
        return 0
    if local is None:
        print("[INFO] 本地没有 state.json，采用远端")
        merged = remote
    else:
        merged = merge(local, remote)

    tmp = local_path + ".merge.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, local_path)
    print(f"[SUCCESS] state 已合并：events={len(merged.get('events') or {})} "
          f"processed={len(merged.get('processed_urls') or [])} "
          f"pool={len(merged.get('news_pool') or [])} "
          f"morning_lock={merged.get('last_morning_report_date') or '无'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
