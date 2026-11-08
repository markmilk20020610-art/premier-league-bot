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
focus             焦点战子树：referee_pushed 取或、lineup_pushed 取并集、
                  first_seen 取更早、competitor 取对手时间更早的那份。
                  原则是「宁可多推一次，不能漏推」。
morning_delivery  早报分段账本：跨天取更晚那天；同一天原文一致时分段取并集
                  （已送达永不降级）；原文不一致时整份取进度更靠前的那份。
                  原则是「宁可少发一段，也不能重复发一段」。

审核隐私          合并结果落盘前必须通过 state_store.public_review_violations，
                  与 save_state_atomic 是同一套判定（PR #1 审查 P1-B：
                  这条路径以前完全没调用它）。审核记录只保留白名单字段，
                  绝不把 article_title / evidence_url 补回审核记录；
                  REVIEW_DECLINED 不被任何自动投递状态覆盖；遇到无法
                  安全裁决的矛盾抛 MergeConflict 中止，不写盘（fail-closed）。

用法：
    python scripts/merge_state.py <本地 state.json> <远端 state.json>

合并结果写回本地文件；任一侧缺失时按另一侧原样保留。
"""

import json
import os
import sys

# 复用公开 state 的隐私守卫。合并脚本写的是**公开** state.json，判定规则
# 必须和 state_store.save_state_atomic 同源，否则「落盘前拦截」形同虚设。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import state_store  # noqa: E402

# 投递进度排序：数字越大表示越「靠后」，冲突时保留更大的
# V1.0：审核闸门两个状态必须显式列出。以前缺失 → _rank() 返回 -1，
# 「人工拒绝」比 DISCOVERED 还小，冲突时会被普通投递状态静默覆盖，
# 等于自动重发一条被明确拒绝的内容。
STATUS_RANK = {
    "DISCOVERED": 0,
    "VERIFIED": 1,
    # 已进人工队列：比「已核实」靠后，但还没获准进投递管道
    "PENDING_REVIEW": 2,
    "PENDING_DELIVERY": 3,
    "RETRYABLE_FAILURE": 3,
    "DELIVERY_FAILED": 4,
    "DELIVERY_UNCERTAIN": 5,
    "DELIVERED": 6,
    "COMMITTED": 7,
    # 人工拒绝是终态，排最末：任何自动投递状态都不得覆盖它
    "REVIEW_DECLINED": 8,
}


class MergeConflict(RuntimeError):
    """两份 state 的审核状态矛盾，无法安全自动裁决。

    fail-closed 的含义：抛出 → main() 返回非 0 → commit_state.sh 的
    merge_with_remote 返回 1 → 本轮不写盘、不提交，Job 变红等人来看。
    绝不「挑一个看起来更合理的状态」写下去——那可能让一条被人工拒绝的
    内容重新进入投递队列，或让一条已送达的记录回到待投递。
    """


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


# 真实送达证据：只有这两种状态**且**带 Telegram 回执，才算「消息确实出去了」。
_DELIVERED_EVIDENCE = ("DELIVERED", "COMMITTED")


def _status_of(event):
    return (event or {}).get("delivery_status")


def _resolve_status(a, b):
    """裁决两份同 ID 事件的投递状态（P1-B 的冲突规则）。

    审核两个状态不能交给 STATUS_RANK 盲目比大小，否则「拒绝」会被
    「待投递」当成进度更低而覆盖掉。规则如下：

    1. 一侧是 REVIEW_DECLINED（人工终态）
       · 另一侧**没有**真实送达证据 → 保留 REVIEW_DECLINED。
         PENDING_DELIVERY / RETRYABLE_FAILURE / DELIVERY_FAILED /
         DELIVERY_UNCERTAIN / DISCOVERED / VERIFIED / PENDING_REVIEW 全部让位。
       · 另一侧**有**真实送达证据（DELIVERED/COMMITTED + message_id）
         → 「人说不发，但消息确实出去了」，这是矛盾，抛 MergeConflict 停下。
         静默选任一边都有害：选拒绝会让已发出的记录回到待投递（重复推送），
         选送达会让拒绝决定消失（绕过人工决定）。

    2. 一侧是 PENDING_REVIEW（人工闸门）
       · 另一侧是「已过闸」的投递状态（PENDING_DELIVERY 及之后）→ 取后者。
         状态推进本身就是批准凭据：审核通过的唯一表现就是
         PENDING_REVIEW → PENDING_DELIVERY，没有别的记录方式。
       · 另一侧只是 DISCOVERED / VERIFIED → 保留 PENDING_REVIEW，闸门更严。

    3. 其余情况按 STATUS_RANK 取更靠后的；同级保留本地（a）。
    """
    sa, sb = _status_of(a), _status_of(b)
    if sa is None:
        return sb
    if sb is None:
        return sa
    if sa == sb:
        return sa

    declined = state_store.REVIEW_DECLINED
    if declined in (sa, sb):
        other, other_status = (b, sb) if sa == declined else (a, sa)
        if (other_status in _DELIVERED_EVIDENCE
                and (other or {}).get("telegram_message_id")):
            raise MergeConflict(
                "一边是人工拒绝（{}），另一边是已真实送达（{}，message_id={}）".format(
                    declined, other_status, (other or {}).get("telegram_message_id")))
        return declined

    pending = state_store.PENDING_REVIEW
    if pending in (sa, sb):
        other_status = sb if sa == pending else sa
        if _rank({"delivery_status": other_status}) > _rank({"delivery_status": pending}):
            return other_status
        return pending

    ra, rb = _rank(a), _rank(b)
    if ra != rb:
        return sa if ra > rb else sb
    return sa


def _sanitize_event(event, status=None):
    """保证单条事件符合公开 state 的审核隐私约束。

    P1-B 的第二条缺陷就在这个函数要挡住的地方：`_merge_event` 原来会把
    另一侧的 `article_title` / `evidence_url` 补回**审核记录**，等于用一次
    合并把「哪条新闻在审」重新写回公开仓库。

    规则与 state_store.scrub_review_records 保持一致：
      · 审核记录 → 只保留 REVIEW_STUB_ALLOWED 白名单字段，
        chunks / pending_text 强制清空（stub 里它们永远是空的）；
      · 其他记录 → 删掉黑名单里的审核私有字段，已公开的标题与链接保持原样
        （它们本来就发出去了，删掉反而破坏审计）。
    """
    out = dict(event or {})
    status = status if status is not None else out.get("delivery_status")
    if status in state_store.REVIEW_STATUSES:
        out = {k: v for k, v in out.items() if k in state_store.REVIEW_STUB_ALLOWED}
        out["delivery_status"] = status
        out["chunks"] = []
        out["pending_text"] = ""
        return out
    for key in state_store.PUBLIC_REVIEW_FORBIDDEN:
        out.pop(key, None)
    return out


def _merge_event(a, b):
    a = a if isinstance(a, dict) else None
    b = b if isinstance(b, dict) else None
    if a is None:
        return _sanitize_event(dict(b or {}))
    if b is None:
        return _sanitize_event(dict(a or {}))

    status = _resolve_status(a, b)      # 冲突在这里 fail-closed（抛 MergeConflict）
    winner = _better_event(a, b)
    if status == state_store.REVIEW_DECLINED and _status_of(winner) != status:
        # 拒绝是终态：元数据取「带着拒绝的那份」，别让另一侧把 review_ref 冲掉
        winner = a if _status_of(a) == status else b

    out = dict(winner)
    out["delivery_status"] = status
    out["chunks"] = _merge_chunks((a or {}).get("chunks"), (b or {}).get("chunks"))

    if status not in state_store.REVIEW_STATUSES:
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
    return _sanitize_event(out, status)


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


# =============================================================================
# focus_state.json 的语义合并
# =============================================================================
# 焦点战 job 用独立文件（focus_state.json），但走的是同一个 commit_state.sh，
# rebase 失败时也会进这里。若不处理 `focus` 键，merge() 的 `out = dict(local)`
# 会把远端的整棵焦点子树静默丢掉——重启后重复推送一次。
#
# 合并原则只有一条：**宁可多推一次，也不能漏推**。
#   referee_pushed  取或（任一侧推过就算推过）
#   lineup_pushed   取并集；同队不同指纹时保留本地，最坏结果是重推一次
#   first_seen      取更早的（这才是「我方首次检测时间」的真相）
#   competitor      取对手时间更早的那份（更保守，不会虚报领先）
#   *_at / ticks    取更晚 / 更大
def _merge_focus(local, remote):
    a, b = local or {}, remote or {}
    if not a:
        return dict(b)
    if not b:
        return dict(a)

    out = dict(a)
    out["calendar_at"] = _later(a.get("calendar_at"), b.get("calendar_at"))
    out["official_at"] = _later(a.get("official_at"), b.get("official_at"))
    out["competitor_at"] = _later(a.get("competitor_at"), b.get("competitor_at"))
    out["last_tick_at"] = _later(a.get("last_tick_at"), b.get("last_tick_at"))
    out["ticks"] = max(int(a.get("ticks") or 0), int(b.get("ticks") or 0))

    # 日历与官方赛程：谁更新用谁（时间戳已经比过了）
    for key, stamp in (("calendar", "calendar_at"), ("official_fixtures", "official_at")):
        if (b.get(stamp) or "") > (a.get(stamp) or "") and b.get(key) is not None:
            out[key] = b[key]

    matches = dict(a.get("matches") or {})
    for mid, rms in (b.get("matches") or {}).items():
        cur = matches.get(mid)
        if not isinstance(cur, dict) or not isinstance(rms, dict):
            matches.setdefault(mid, rms)
            continue
        merged = dict(cur)
        merged["referee_pushed"] = bool(cur.get("referee_pushed")
                                       or rms.get("referee_pushed"))
        lp = dict(rms.get("lineup_pushed") or {})
        lp.update(cur.get("lineup_pushed") or {})     # 本地优先
        merged["lineup_pushed"] = lp
        fs = dict(rms.get("first_seen") or {})
        for team, ts in (cur.get("first_seen") or {}).items():
            fs[team] = min(ts, fs[team]) if team in fs else ts
        merged["first_seen"] = fs
        op = dict(rms.get("official_published_at") or {})
        op.update(cur.get("official_published_at") or {})
        merged["official_published_at"] = op
        c1, c2 = cur.get("competitor") or {}, rms.get("competitor") or {}
        if not c1:
            merged["competitor"] = c2
        elif c2:
            t1 = str(c1.get("competitors_seen_at") or "9999")
            t2 = str(c2.get("competitors_seen_at") or "9999")
            merged["competitor"] = c1 if t1 <= t2 else c2
        matches[mid] = merged
    out["matches"] = matches
    return out


def _merge_morning_delivery(a, b):
    """早报分段投递账本的语义合并（V2.2 P0）。

    为什么必须单独处理：`merge()` 是 `out = dict(local)` 起手，只显式合并已知键。
    如果不认识 `morning_delivery`，远端刚记下的「第 1 段已送达」会被本地那份
    更旧的记录盖掉 → 下一轮把第 1 段再发一次。

    合并原则：**宁可少发一段，也不能重复发一段**（重复对读者是可见的打扰，
    少一段至少还能在下一次续上）。
      · 日期不同 → 取更晚那天（今天盖过昨天）
      · 同一天但原文不同 → 两边的分段布局不可混用，整份取「已送达段更多」的那份
      · 同一天且原文一致 → 分段取并集（已送达的永不降级），计数取大，时间取晚
    """
    a, b = a or {}, b or {}
    if not a:
        return dict(b)
    if not b:
        return dict(a)

    da, db = a.get("date") or "", b.get("date") or ""
    if da != db:
        return dict(a if da > db else b)

    if (a.get("pending_text") or "") != (b.get("pending_text") or ""):
        # 原文不同 = 两次生成出来的卡片不一样，分段哈希对不上。
        # 混着合并会让「第 1 段来自旧文、第 2 段来自新文」，读者看到的是半截拼接。
        def progress(rec):
            done = [c for c in (rec.get("chunks") or [])
                    if isinstance(c, dict)
                    and c.get("delivery_status") == "DELIVERED"
                    and c.get("telegram_message_id")]
            return (len(done), rec.get("updated_at") or "")
        return dict(a if progress(a) >= progress(b) else b)

    out = dict(a)
    out["chunks"] = _merge_chunks(a.get("chunks"), b.get("chunks"))
    out["attempts"] = max(int(a.get("attempts") or 0), int(b.get("attempts") or 0))
    out["updated_at"] = _later(a.get("updated_at"), b.get("updated_at"))
    # 状态取更「靠前」的那份：DELIVERED 优先，其次部分送达
    rank = {"DELIVERED": 4, "PARTIALLY_DELIVERED": 3, "DELIVERING": 2,
            "DELIVERY_FAILED": 1, "": 0}
    sa, sb = rank.get(a.get("status") or "", 0), rank.get(b.get("status") or "", 0)
    out["status"] = a.get("status") if sa >= sb else b.get("status")
    return out


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


def _sanitize_events(events):
    """逐条清理 events 字典；非 dict 的畸形值原样保留。"""
    if not isinstance(events, dict):
        return events
    return {
        eid: (_sanitize_event(rec) if isinstance(rec, dict) else rec)
        for eid, rec in events.items()
    }


def _sanitize_state(state):
    """整份 state 的事件级清理（只动 events，其余结构原样保留）。

    早退路径（一侧缺失）也必须过一遍：`merge()` 是公开函数，调用方拿到的
    结果不能「取决于有没有真的走合并逻辑」才干净。
    """
    out = dict(state or {})
    if isinstance(out.get("events"), dict):
        out["events"] = _sanitize_events(out["events"])
    return out


def merge(local, remote):
    """把远端 state 合并进本地 state，返回新字典。"""
    if not local:
        return _sanitize_state(remote)
    if not remote:
        return _sanitize_state(local)

    out = dict(local)

    # 远端独有的记录不会进 _merge_event，必须在这里先清一遍
    events = dict(_sanitize_events(remote.get("events") or {}) or {})
    for eid, event in (local.get("events") or {}).items():
        try:
            events[eid] = _merge_event(event, events.get(eid))
        except MergeConflict as e:
            # 补上事件 id，否则报错只告诉你「有冲突」，不告诉你哪一条
            raise MergeConflict("事件 {}：{}".format(eid, e)) from e
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
    if local.get("focus") or remote.get("focus"):
        out["focus"] = _merge_focus(local.get("focus"), remote.get("focus"))
    # V2.2 P0：早报分段账本。不显式合并就会被本地那份更旧的记录盖掉。
    if local.get("morning_delivery") or remote.get("morning_delivery"):
        out["morning_delivery"] = _merge_morning_delivery(
            local.get("morning_delivery"), remote.get("morning_delivery"))
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


def _structure_violations(state):
    """落盘前的结构 fail-closed 校验。

    `public_review_violations()` 也会把「events 结构异常」判成违规，但它
    只能在拿到 state 之后检查；合并逻辑在它之前就要对 events 调 `.items()`，
    结构一坏就直接抛 AttributeError（进程照样非 0 退出，但那是一段
    traceback，不是一条可读的拒绝判定）。这里提前挡住。
    """
    if not isinstance(state, dict):
        return ["state 根节点不是 JSON 对象（实际 {}）".format(type(state).__name__)]
    events = state.get("events") or {}
    if not isinstance(events, dict):
        return ["events 结构异常（期望 dict，实际 {}），无法做事件级合并"
                .format(type(events).__name__)]
    return []


def _write_checked(merged, local_path):
    """落盘前的统一守卫：清理 + 校验 + 原子写入。

    校验用 state_store.public_review_violations——和 save_state_atomic
    完全同一套判定，不另写一份「差不多的」检查（两份规则迟早会分叉）。
    返回 False 表示**没有写盘**，调用方必须让进程非 0 退出。
    """
    problems = _structure_violations(merged)
    if problems:
        print("[ERROR] 合并结果结构异常，拒绝写入 {}：".format(local_path))
        for item in problems:
            print("        - {}".format(item))
        return False

    try:
        state_store.scrub_review_records(merged)
    except Exception as e:  # noqa: BLE001 —— 清理失败也必须 fail-closed
        print("[ERROR] 审核字段清理失败，拒绝写入：{}: {}".format(type(e).__name__, e))
        return False

    violations = state_store.public_review_violations(merged)
    if violations:
        print("[ERROR] 合并结果未通过审核隐私检查，拒绝写入 {}（共 {} 处）：".format(
            local_path, len(violations)))
        for item in violations[:10]:
            print("        - {}".format(item))
        return False

    tmp = local_path + ".merge.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, local_path)
    return True


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    local_path, remote_path = sys.argv[1], sys.argv[2]
    local, remote = _load(local_path), _load(remote_path)

    if local is None and remote is None:
        print("[ERROR] 本地与远端 state 都不可读，放弃合并")
        return 1

    # 结构 fail-closed：events 不是 dict 时，合并逻辑会在 .items() 上抛
    # AttributeError，隐私守卫也拿不到可逐条校验的对象。先挡住，让拒绝
    # 来自一条明确判定而不是一段 traceback。
    for label, data in (("本地", local), ("远端", remote)):
        if data is None:
            continue
        problems = _structure_violations(data)
        if problems:
            print("[ERROR] {} state 结构异常，拒绝合并（fail-closed，未写入任何文件）：".format(label))
            for item in problems:
                print("        - {}".format(item))
            return 1

    # 两条早退路径同样要过隐私守卫：最终留在磁盘上的就是这一份。
    if remote is None:
        print("[INFO] 远端没有 state.json，保留本地")
        if not _write_checked(dict(local), local_path):
            return 1
        return 0

    if local is None:
        print("[INFO] 本地没有 state.json，采用远端")
        merged = dict(remote)
    else:
        try:
            merged = merge(local, remote)
        except MergeConflict as e:
            print("[ERROR] 合并中止（fail-closed，未写入任何文件）：{}".format(e))
            return 1

    if not _write_checked(merged, local_path):
        return 1

    print(f"[SUCCESS] state 已合并：events={len(merged.get('events') or {})} "
          f"processed={len(merged.get('processed_urls') or [])} "
          f"pool={len(merged.get('news_pool') or [])} "
          f"morning_lock={merged.get('last_morning_report_date') or '无'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
