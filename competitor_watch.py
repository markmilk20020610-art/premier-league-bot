"""对手发布时间采集（V2.0 §5「用真实时间戳证明速度优势」）。

## 为什么必须有这个模块

`lineup_monitor.speed_proof()` 要求 `competitors_seen_at` 才能算「领先多少秒」。
没有它，所有推送只能走「不宣称领先」分支——这不是谦虚，是没证据。

本模块负责填上这个字段：定期抓虎扑 / 懂球帝 / 直播吧的足球资讯流，
找出**提到本场首发**的最早一条，把它的发布时间作为 `competitors_seen_at`。

## 三个源的可采集性（2026-10-31 实测）

| 源 | 入口 | 时间精度 | 实测 |
|---|---|---|---|
| 懂球帝 | `www.dongqiudi.com/api/app/tabs/web/1.json` | **秒**（`published_at`） | 46KB JSON，含 title/url/时间 |
| 虎扑 | `soccer.hupu.com` | **秒**（`publishTime`） | 页面内嵌 JSON，正则可取 |
| 直播吧 | `news.zhibo8.cc/zuqiu/` | 列表只有**日期**（在 URL 路径里） | 需要再请求文章页拿 `YYYY-MM-DD HH:MM:SS` |

## 三条必须守的纪律

1. **时间戳必须校验。** 懂球帝的置顶文章实测出现过 `published_at = 2030-10-30`（未来时间），
   直接拿来算会得出「对手比我们早 4 年」。因此所有时间戳都要落在
   `[now-24h, now+10min]` 内，越界一律标 `suspect` 并**排除出速度计算**。
2. **北京时间 → UTC。** 三家都是 UTC+8，统一减 8 小时，不依赖本机时区。
3. **匹配要保守。** 必须同时命中「首发类关键词」和「队名」。宁可漏判（退回不宣称领先），
   也不要把一条无关新闻当成对手已经发了首发。

## 合规

采集仅用于**内部计时对照**，不转载、不二次分发对方内容，只保留「标题 + 发布时间 + 链接」。
请求间隔由调用方控制（见 `polite_delay`），避免给对方造成压力。
"""

import datetime
import json
import re
import time
import urllib.request

UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept-Language": "zh-CN,zh;q=0.9",
}

SOURCE_DONGQIUDI = "dongqiudi"
SOURCE_HUPU = "hupu"
SOURCE_ZHIBO8 = "zhibo8"

URL_DONGQIUDI = "https://www.dongqiudi.com/api/app/tabs/web/1.json"
URL_HUPU = "https://soccer.hupu.com/"
URL_ZHIBO8 = "https://news.zhibo8.cc/zuqiu/"

SOURCES = {
    SOURCE_DONGQIUDI: {"name": "懂球帝", "url": URL_DONGQIUDI, "precision": "second"},
    SOURCE_HUPU: {"name": "虎扑", "url": URL_HUPU, "precision": "second"},
    SOURCE_ZHIBO8: {"name": "直播吧", "url": URL_ZHIBO8, "precision": "day_then_page"},
}

# 首发类关键词（命中任一即视为「这条新闻在讲首发」）
LINEUP_KEYWORDS = (
    "首发", "先发", "阵容", "首发名单", "公布首发", "确认首发",
    "line-up", "lineup", "starting xi", "team news",
)

# 队名 → 中文写法（三个源都用中文队名，必须能对上）
TEAM_CN = {
    "arsenal": ("阿森纳", "枪手"),
    "aston villa": ("阿斯顿维拉", "维拉"),
    "bournemouth": ("伯恩茅斯", "樱桃"),
    "brentford": ("布伦特福德",),
    "brighton": ("布莱顿",),
    "chelsea": ("切尔西", "蓝军"),
    "coventry": ("考文垂",),
    "crystal palace": ("水晶宫",),
    "everton": ("埃弗顿",),
    "fulham": ("富勒姆",),
    "hull": ("赫尔城",),
    "ipswich": ("伊普斯维奇",),
    "leeds": ("利兹联", "利兹"),
    "liverpool": ("利物浦", "红军"),
    "man city": ("曼城", "蓝月亮"),
    "man utd": ("曼联", "红魔"),
    "newcastle": ("纽卡斯尔", "纽卡"),
    "nottm forest": ("诺丁汉森林",),
    "sunderland": ("桑德兰",),
    "tottenham": ("热刺",),
    "west ham": ("西汉姆", "西汉姆联"),
    "wolves": ("狼队", "伍尔弗汉普顿"),
}

# 时间戳合理区间
MAX_AGE_HOURS = 24
MAX_FUTURE_MINUTES = 10

BEIJING_OFFSET = datetime.timedelta(hours=8)

# 请求间隔（秒）。三个源依次请求，不并发轰炸。
POLITE_DELAY = 1.0


# =============================================================================
# 一、时间解析与校验
# =============================================================================
def beijing_to_utc(text):
    """'2026-10-31 20:56:19' → naive UTC datetime。解析失败返回 None。"""
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(str(text).strip(), fmt) - BEIJING_OFFSET
        except ValueError:
            continue
    return None


def is_plausible(ts, now):
    """时间戳是否落在合理区间。越界 = suspect（典型：置顶文章的假未来时间）。"""
    if ts is None:
        return False
    if ts > now + datetime.timedelta(minutes=MAX_FUTURE_MINUTES):
        return False
    if ts < now - datetime.timedelta(hours=MAX_AGE_HOURS):
        return False
    return True


# =============================================================================
# 二、各源解析
# =============================================================================
def parse_dongqiudi(payload, now=None):
    """懂球帝 JSON → 条目列表。

    ⚠️ `published_at` 实测存在脏数据（置顶文章写成 2030 年）。因此优先用
    `created_at`（真实入库时间），`published_at` 只在不越界时采用。
    """
    now = now or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    items = []
    for a in (payload.get("articles") or []):
        title = (a.get("title") or a.get("share_title") or "").strip()
        if not title:
            continue
        pub = beijing_to_utc(a.get("published_at"))
        created = beijing_to_utc(a.get("created_at"))
        ts, suspect = None, False
        if is_plausible(pub, now):
            ts = pub
        elif is_plausible(created, now):
            ts = created
            suspect = pub is not None        # published_at 有值但不可信
        items.append({
            "source_id": SOURCE_DONGQIUDI,
            "title": title,
            "url": (a.get("share") or a.get("url") or "").strip(),
            "published_at": ts.strftime("%Y-%m-%dT%H:%M:%SZ") if ts else "",
            "timestamp_suspect": suspect,
            "precision": "second",
        })
    return items


_HUPU_TIME_RE = re.compile(r'"publishTime":"(?P<time>[^"]+)"')
_HUPU_LINK_RE = re.compile(r'"link":"(?P<link>https?://[^"]+)"')
_HUPU_TITLE_RE = re.compile(r'"title":"(?P<title>(?:[^"\\]|\\.)*)"')
_HUPU_ADD_RE = re.compile(r'"addTime":(?P<ts>\d{9,11})')

# 懂球帝文章页上显示的是「发布于 2026-10-31 21:49」（北京时间）。
# 列表接口的 published_at 实测会偏早：同一篇文章列表给 20:24、页面显示 21:49，
# 差 85 分钟。不校正就会误判「对手比我们快一个半小时」——速度证明直接失效。
_DQD_PAGE_TIME_RE = re.compile(
    r"发布于\s*(?P<t>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?)")


def parse_hupu(html, now=None):
    """虎扑足球首页内嵌 JSON → 条目列表。

    字段顺序实测是 `title … link … publishTime`（publishTime 在 link 之后，
    不是之前）。所以按 publishTime 定位，再**向前**找最近的 link 与 title——
    同一个对象内的字段必然都在 publishTime 之前，向前找最近的即是本条。
    `addTime`（epoch 秒）作为交叉校验：两者不一致时以 publishTime 为准，
    并标记 suspect，因为 epoch 更可能是服务端时间而 publishTime 是展示时间。
    """
    now = now or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    html = html or ""
    items = []
    for m in _HUPU_TIME_RE.finditer(html):
        head = html[max(0, m.start() - 2000):m.start()]
        links = list(_HUPU_LINK_RE.finditer(head))
        titles = list(_HUPU_TITLE_RE.finditer(head))
        if not links or not titles:
            continue
        link = links[-1].group("link")
        title = _unescape(titles[-1].group("title")).strip()
        ts = beijing_to_utc(m.group("time"))
        plausible = is_plausible(ts, now)

        add = _HUPU_ADD_RE.search(html[m.end():m.end() + 200])
        epoch_ts = None
        if add:
            epoch_ts = datetime.datetime.fromtimestamp(
                int(add.group("ts")), tz=datetime.timezone.utc).replace(tzinfo=None)
            if ts and abs((epoch_ts - ts).total_seconds()) > 120:
                plausible = False        # 两个时间自相矛盾 → 不可信

        items.append({
            "source_id": SOURCE_HUPU,
            "title": title,
            "url": link,
            "published_at": ts.strftime("%Y-%m-%dT%H:%M:%SZ") if plausible else "",
            "timestamp_suspect": bool(ts) and not plausible,
            "precision": "second",
        })
    return items


_ZHIBO8_ITEM_RE = re.compile(
    r'<a[^>]+href="(?P<link>//news\.zhibo8\.com/zuqiu/(?P<date>\d{4}-\d{2}-\d{2})/'
    r'[^"]+)"[^>]*>(?P<title>[^<]+)</a>')


def parse_zhibo8_list(html):
    """直播吧列表 → 条目列表。列表页**只有日期**（在 URL 路径里），没有时分秒。"""
    items = []
    for m in _ZHIBO8_ITEM_RE.finditer(html or ""):
        items.append({
            "source_id": SOURCE_ZHIBO8,
            "title": m.group("title").strip(),
            "url": "https:" + m.group("link"),
            "published_at": "",              # 待 resolve
            "date_hint": m.group("date"),
            "timestamp_suspect": False,
            "precision": "day",
        })
    return items


_ARTICLE_TIME_RE = re.compile(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})")


def parse_zhibo8_article_time(html):
    """直播吧文章页 → 精确发布时间（UTC）。实测页面里只有一个完整时间戳。"""
    m = _ARTICLE_TIME_RE.search(html or "")
    return beijing_to_utc(m.group(1)) if m else None


def _unescape(s):
    try:
        return json.loads(f'"{s}"')
    except Exception:                       # noqa: BLE001
        return s.replace("\\/", "/").replace('\\"', '"')


# =============================================================================
# 三、抓取
# =============================================================================
def http_get(url, timeout=20, headers=None):
    req = urllib.request.Request(url, headers=headers or UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", errors="replace")


def fetch_all(now=None, transport=None, delay=POLITE_DELAY, sources=None):
    """抓三个源，返回 {items, source_status}。

    单个源失败不影响其它源：速度证明本来就是「有对手时间才算」，
    少一个源只会让证明更保守，不会更激进。
    """
    now = now or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    transport = transport or http_get
    wanted = sources or list(SOURCES)
    items, status = [], []

    for i, sid in enumerate(wanted):
        if i and delay:
            time.sleep(delay)
        url = SOURCES[sid]["url"]
        try:
            code, body = transport(url)
            if code != 200:
                status.append({"source_id": sid, "ok": False, "status": code})
                continue
            if sid == SOURCE_DONGQIUDI:
                got = parse_dongqiudi(json.loads(body), now)
            elif sid == SOURCE_HUPU:
                got = parse_hupu(body, now)
            else:
                got = parse_zhibo8_list(body)
            items.extend(got)
            status.append({"source_id": sid, "ok": True, "status": code,
                           "items": len(got)})
        except Exception as exc:            # noqa: BLE001
            status.append({"source_id": sid, "ok": False,
                           "status": type(exc).__name__, "error": str(exc)[:120]})
    return {"items": items, "source_status": status, "fetched_at":
            now.strftime("%Y-%m-%dT%H:%M:%SZ")}


# =============================================================================
# 四、匹配本场首发
# =============================================================================
def team_hits(title, home, away):
    """标题里命中了哪几支球队（返回命中数 0/1/2）。"""
    t = title or ""
    hit = 0
    for name in (home, away):
        variants = TEAM_CN.get((name or "").strip().lower(), ())
        if any(v in t for v in variants):
            hit += 1
    return hit


def is_lineup_news(title):
    t = (title or "").lower()
    return any(k.lower() in t for k in LINEUP_KEYWORDS)


def find_lineup_mentions(news_items, home, away, now,
                         transport=None, resolve_zhibo8=True,
                         require_both_teams=False, max_resolve=5):
    """找出「提到本场首发」的对手条目，按发布时间升序返回。

    `require_both_teams=True` 更严格（标题必须同时出现两队），用于
    防止「阿森纳首发」这种单队新闻被误判成本场。

    `max_resolve` 是**防封保护**：直播吧的列表页有近千条，每条都要单独请求
    文章页才能拿到时分秒。不加限制的话，一次调用可能打出上百个请求，
    轻则被限流，重则 IP 被封——那时连「我方检测时间」都拿不到了。
    因此只解析最可能的 N 条（按日期贴近开球日排序）。
    """
    candidates, unresolved = [], []
    for it in news_items or []:
        if not is_lineup_news(it.get("title")):
            continue
        hits = team_hits(it.get("title"), home, away)
        need = 2 if require_both_teams else 1
        if hits < need:
            continue
        item = dict(it, matched_teams=hits)
        if item.get("published_at"):
            candidates.append(item)
        elif item.get("source_id") == SOURCE_ZHIBO8 and resolve_zhibo8:
            unresolved.append(item)

    # 只对最可能的少数条目补请求
    if unresolved and max_resolve > 0:
        def _distance(it):
            d = it.get("date_hint") or ""
            try:
                return abs((datetime.datetime.strptime(d, "%Y-%m-%d").date()
                            - now.date()).days)
            except ValueError:
                return 99
        unresolved.sort(key=_distance)
        for it in unresolved[:max_resolve]:
            ts = _resolve_zhibo8(it, now, transport)
            if ts:
                candidates.append(dict(it, published_at=ts))

    candidates.sort(key=lambda x: x["published_at"])
    return candidates


def _resolve_zhibo8(item, now, transport):
    """直播吧列表没有时分秒，需要再请求一次文章页。"""
    url = item.get("url")
    if not url:
        return ""
    try:
        code, body = (transport or http_get)(url)
        if code != 200:
            return ""
        ts = parse_zhibo8_article_time(body)
        if ts and is_plausible(ts, now):
            return ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:                       # noqa: BLE001
        return ""
    return ""


def verify_published_at(item, now, transport=None):
    """用文章页上显示的时间校正列表接口给的时间。

    为什么必须校正：实测 2026-10-31 曼城 vs 布莱顿，懂球帝列表接口给 20:24（北京），
    文章页写「发布于 21:49」。列表时间偏早 85 分钟，直接拿来对比就会得出
    「对手比我们快一个半小时」的假结论——而我们真正只是晚了 6 分钟。

    只在能读到页面时间、且新时间通过合理性检查时才替换；否则原样返回，
    并在 `time_correction` 里说明用了哪个来源。
    """
    url = item.get("url") or ""
    if not url or item.get("source_id") != SOURCE_DONGQIUDI:
        return item
    try:
        code, body = (transport or http_get)(url)
    except Exception:                       # noqa: BLE001
        return item
    if code != 200 or not body:
        return item
    m = _DQD_PAGE_TIME_RE.search(body)
    if not m:
        return item
    raw = m.group("t")
    if len(raw) == 16:                      # 只有分钟，补秒
        raw += ":00"
    ts = beijing_to_utc(raw)
    if not is_plausible(ts, now):
        return item
    page_ts = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    if page_ts == item.get("published_at"):
        out = dict(item)
        out["time_correction"] = "list_ok"
        return out
    out = dict(item)
    out["published_at_list"] = item.get("published_at")
    out["published_at"] = page_ts
    out["time_correction"] = "page_overrides_list"
    return out


def earliest_competitor_time(news_items, home, away, now, **kw):
    """本场最早的对手首发发布时间（供 speed_proof 使用）。

    没有匹配 → 返回 ""，调用方据此走「不宣称领先」分支。
    """
    hits = find_lineup_mentions(news_items, home, away, now, **kw)
    return hits[0]["published_at"] if hits else ""


# =============================================================================
# 五、与速度证明对接
# =============================================================================
def competitor_speed_record(news_items, home, away, now, verify_top=2, **kw):
    """生成给 `lineup_monitor.speed_proof` 用的对照信息（含来源与证据）。

    `verify_top` 条最早的候选会额外请求一次文章页校正时间——只校正最靠前的几条，
    因为只有它们可能成为「对手最早时间」，多请求没有收益还会招限流。
    """
    hits = find_lineup_mentions(news_items, home, away, now, **kw)
    if not hits:
        return {
            "competitors_seen_at": "",
            "competitor_source": "",
            "competitor_title": "",
            "evidence_count": 0,
            "note": "未采集到任何对手的首发发布时间，不得宣称领先",
        }

    transport = kw.get("transport")
    corrected = 0
    for i in range(min(verify_top, len(hits))):
        before = hits[i]["published_at"]
        hits[i] = verify_published_at(hits[i], now, transport)
        if hits[i]["published_at"] != before:
            corrected += 1
    if corrected:
        hits.sort(key=lambda x: x["published_at"])

    first = hits[0]
    note = (f"最早由 {SOURCES[first['source_id']]['name']} 于 "
            f"{first['published_at']} 发布（共 {len(hits)} 条匹配）")
    if first.get("time_correction") == "page_overrides_list":
        note += (f"；列表接口原报 {first.get('published_at_list')}，"
                 f"已按文章页时间校正")
    return {
        "competitors_seen_at": first["published_at"],
        "competitor_source": first["source_id"],
        "competitor_title": first["title"],
        "competitor_url": first.get("url", ""),
        "evidence_count": len(hits),
        "all_sources": sorted({h["source_id"] for h in hits}),
        "time_corrected": corrected,
        "note": note,
    }


def validate_news_item(item):
    """条目字段校验。"""
    problems = []
    for f in ("source_id", "title", "url", "published_at", "precision"):
        if f not in item:
            problems.append(f"缺少字段 {f}")
    if item.get("source_id") not in SOURCES:
        problems.append(f"source_id 非法：{item.get('source_id')}")
    if item.get("precision") not in ("second", "day"):
        problems.append(f"precision 非法：{item.get('precision')}")
    return problems
