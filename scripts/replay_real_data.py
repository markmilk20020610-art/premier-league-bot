"""发布前真实数据回放：同一份 state.json 快照，分别走旧路径与新路径。

只读。脚本不写任何 state 文件，不联网，不碰生产仓库。
数据来源是 state.json 快照的**副本**（由调用方复制到临时目录后传入）。

旧路径 = 不调用事件聚合（等价于 IMPORTANCE_SORT=0）：
    pool = _pool_for_morning(state)
    verified, excluded = _build_morning_events(pool)
新路径 = 调用事件聚合（等价于 IMPORTANCE_SORT=1）：
    ordered, events, stats = _events_for_morning(state, pool)
    verified, excluded = _build_morning_events(ordered)

两条路径共用同一个「当前时刻」，即快照自身的 last_scan_at。
否则用今天的时间回放 11-01 的池子，所有条目都会因为超过 24 小时被判定过期。

用法：
    python scripts/replay_real_data.py <state.json> [<state.json> ...]
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bot
import content_type as ctype
import event_aggregate as evagg
import evidence as evid
import importance as imp

# ---------- 误杀审查用的分类词表 ----------
# 这些类别是「被新规则过滤后必须人工复核」的高风险区：
# 它们经常长得像商业新闻/常规资讯，实际是俱乐部重大变动。
AUDIT_BUCKETS = [
    ("俱乐部所有权变化", [
        "takeover", "ownership", "consortium", "majority stake", "buyout",
        "sale of the club", "new owner", "owners", "administration",
        "administrator", "insolvency", "take over",
    ]),
    ("财务及治理", [
        "psr", "profit and sustainability", "financial fair play", "ffp",
        "points deduction", "accounts", "wage bill", "spending rules",
        "breach", "hearing", "governance", "board", "sporting director",
        "chief executive", "ceo", "chairman",
    ]),
    ("重大赞助及商业决策", [
        "sponsor", "sponsorship", "kit deal", "shirt deal", "naming rights",
        "commercial", "partnership", "broadcast deal", "sleeve",
        "record deal", "investment",
    ]),
    ("重要纪律事件", [
        "ban", "banned", "suspension", "suspended", "charge", "charged",
        "misconduct", "red card", "appeal", "sanction", "fine", "fa cup charge",
        "investigation",
    ]),
]


def audit_tags(title, desc=""):
    text = ((title or "") + " " + (desc or "")).lower()
    hits = []
    for name, needles in AUDIT_BUCKETS:
        for n in needles:
            if n in text:
                hits.append(name)
                break
    return hits


def scope_of(entry):
    if entry.get("scope"):
        return entry["scope"]
    return imp.classify_scope(
        entry.get("title") or "", entry.get("desc") or "",
        evid.primary_event_type(entry.get("title") or ""))[0]


def ctype_of(entry):
    if entry.get("content_type"):
        return entry["content_type"]
    return ctype.classify(
        entry.get("title") or "", entry.get("desc") or "",
        event_type=evid.primary_event_type(entry.get("title") or ""))["content_type"]


def parse_iso(value):
    import datetime
    if not value:
        return None
    v = value.replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def freeze_now(when):
    """把「现在」钉死在快照时刻。回放必须用当时的时钟。"""
    bot._now_utc = lambda: when


def url_key(u):
    return evid.canonical_url(u or "")


def replay(path):
    raw = json.load(open(path, encoding="utf-8"))
    tag = os.path.basename(path).split(".")[0]
    when = parse_iso(raw.get("last_scan_at")) or parse_iso(
        raw.get("last_summary_sent_at"))
    freeze_now(when)

    state = {"events": {}, "news_pool": raw.get("news_pool") or []}

    # ---------- 候选池（两条路径共用）----------
    pool = bot._pool_for_morning(state)
    pool_dropped = len(state["news_pool"]) - len(pool)

    # 事件真值：对整个 24h 池聚合一次，用于「同一事件重复占名额」与覆盖率核算
    all_events, agg_stats = evagg.build_events(pool)
    url2eid = {}
    eid2event = {}
    for e in all_events:
        eid2event[e["event_id"]] = e
        for m in e["members"]:
            url2eid[url_key(m["link"])] = e["event_id"]

    # ---------- 旧路径 ----------
    verified_old, excluded_old = bot._build_morning_events(pool)

    # ---------- 新路径 ----------
    ordered, morning_events, stats = bot._events_for_morning(state, pool)
    verified_new, excluded_new = bot._build_morning_events(ordered)
    selected, slot_report = bot._select_morning_events(morning_events)
    selected_ids = {e["event_id"] for e in selected}

    # ---------- 指标 ----------
    def pack(entries, is_new):
        rows = []
        for e in entries:
            eid = e.get("agg_event_id") if is_new else url2eid.get(url_key(e.get("url")))
            ev_obj = eid2event.get(eid) or {}
            rows.append({
                "title": e.get("title") or "",
                "url": e.get("url") or "",
                "urls": list(e.get("urls") or []),
                "article_count": int(e.get("article_count") or 1),
                "event_id": eid,
                "event_importance": ev_obj.get("importance", ""),
                "event_scope": ev_obj.get("scope", ""),
                "scope": scope_of(e),
                "content_type": ctype_of(e),
                "low_value": ctype.is_low_value(ctype_of(e)),
                "evidence_status": e.get("status", ""),
                "confirmed": bool(e.get("confirmed")),
            })
        return rows

    old_rows = pack(verified_old, False)
    new_rows = pack(verified_new, True)

    def metric(rows):
        aline = [r for r in rows if r["scope"] == imp.A_LINE]
        aline_events = {r["event_id"] for r in aline if r["event_id"]}
        low = [r for r in rows if r["low_value"]]
        glob = [r for r in rows if r["scope"] == imp.GLOBAL_EVENT_LINE]
        s_rows = [r for r in rows if r["event_importance"] == imp.S]
        a_rows = [r for r in rows if r["event_importance"] == imp.A]
        per_event = {}
        for r in rows:
            key = r["event_id"] or ("solo:" + r["url"])
            per_event.setdefault(key, []).append(r)
        dup = {k: v for k, v in per_event.items() if len(v) > 1}
        return {
            "entries": len(rows),
            "aline_entries": len(aline),
            "aline_events": len(aline_events),
            "low_value_entries": len(low),
            "low_value_list": [r["title"] for r in low],
            "global_line_entries": len(glob),
            "s_entries": len(s_rows),
            "a_entries": len(a_rows),
            "distinct_events": len(per_event),
            "dup_events": len(dup),
            "dup_detail": [
                {"event_id": k, "entries": len(v),
                 "titles": [x["title"] for x in v]}
                for k, v in sorted(dup.items(), key=lambda kv: -len(kv[1]))
            ],
            "max_entries_per_event": max([len(v) for v in per_event.values()] or [0]),
            "event_ids": sorted(per_event.keys()),
        }

    m_old, m_new = metric(old_rows), metric(new_rows)

    # S/A 级事件覆盖率（以整池聚合出的事件为真值）
    sa_ids = {e["event_id"] for e in all_events
              if e["importance"] in (imp.S, imp.A)}
    s_ids = {e["event_id"] for e in all_events if e["importance"] == imp.S}
    a_ids = {e["event_id"] for e in all_events if e["importance"] == imp.A}

    def covered(rows, ids):
        seen = {r["event_id"] for r in rows if r["event_id"]}
        return sorted(ids & seen), sorted(ids - seen)

    old_sa_in, old_sa_out = covered(old_rows, sa_ids)
    new_sa_in, new_sa_out = covered(new_rows, sa_ids)

    # ---------- 被新路径过滤掉的条目（逐条复核）----------
    new_urls = set()
    for r in new_rows:
        for u in (r["urls"] or [r["url"]]):
            new_urls.add(url_key(u))
    filtered = []
    for r in old_rows:
        if url_key(r["url"]) in new_urls:
            continue
        eid = r["event_id"]
        if r["low_value"]:
            why = "低价值过滤（PROMOTION/GOSSIP）"
        elif eid and eid not in selected_ids:
            why = "名额预算未入选"
        elif eid and eid in selected_ids:
            why = "证据截断（同事件已保留 3 篇）"
        else:
            why = "证据核验未通过（两条路径都排除）"
        filtered.append({
            "title": r["title"],
            "domain": evid.extract_domain(r["url"]),
            "event_id": eid,
            "event_importance": r["event_importance"],
            "event_scope": r["event_scope"],
            "scope": r["scope"],
            "content_type": r["content_type"],
            "why": why,
            "audit": audit_tags(r["title"]),
        })

    # ---------- 迁移与旧字段兼容 ----------
    compat = {
        "schema_version": raw.get("schema_version"),
        "pool_keys": sorted({k for it in (raw.get("news_pool") or []) for k in it}),
        "events_keys": sorted({k for v in (raw.get("events") or {}).values()
                               if isinstance(v, dict) for k in v}),
        "has_v1_fields": any(
            k in ("_event_id", "importance", "content_type", "scope")
            for it in (raw.get("news_pool") or []) for k in it),
        "state_untouched": raw == json.load(open(path, encoding="utf-8")),
    }

    return {
        "tag": tag,
        "source": path,
        "frozen_now": when.isoformat(),
        "pool_raw": len(state["news_pool"]),
        "pool_window": len(pool),
        "pool_dropped": pool_dropped,
        "agg_stats": agg_stats,
        "slot_report": slot_report,
        "old": m_old,
        "new": m_new,
        "old_rows": old_rows,
        "new_rows": new_rows,
        "excluded_old": len(excluded_old),
        "excluded_new": len(excluded_new),
        "sa": {
            "total": len(sa_ids), "s_total": len(s_ids), "a_total": len(a_ids),
            "old_in": old_sa_in, "old_missing": old_sa_out,
            "new_in": new_sa_in, "new_missing": new_sa_out,
        },
        "filtered": filtered,
        "compat": compat,
    }


def main(argv):
    paths = [a for a in argv[1:] if not a.startswith("-")]
    if not paths:
        print("用法: python scripts/replay_real_data.py <state.json> ...")
        return 2
    out_dir = os.environ.get("REPLAY_OUT_DIR") or ""
    results = [replay(p) for p in paths]
    # bot 的 [INFO] 日志走 stdout，JSON 不能混在里面：有 out 目录就落文件，
    # 没有就打到 stdout（供人工看）。
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        for r in results:
            dst = os.path.join(out_dir, r["tag"] + ".json")
            with open(dst, "w", encoding="utf-8") as f:
                json.dump(r, f, ensure_ascii=False, indent=2, default=str)
            print("[REPLAY] {} → {}".format(r["tag"], dst))
    else:
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
