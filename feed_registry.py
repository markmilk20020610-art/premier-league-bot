"""RSS 来源登记表（V1.3 §F1 专项审计 B）。

**为什么需要这张表**
F1 新闻之所以能进入足球推送流程，唯一入口是 `bot.py` 的 `RSS_FEEDS` 列表里
那一行 `https://www.skysports.com/rss/12040`。它被注释成「顶级权威主流媒体」，
与 BBC 的 `/sport/football/rss.xml` 并列，但它其实是 Sky Sports 的**全站新闻** feed
（频道标题 `SkySports | News`），同一批 20 条里混着足球、F1、高尔夫、飞镖、
橄榄球、拳击、赛马。

一张 URL 列表表达不了「这个源覆盖哪些体育项目」。所以改成登记表：每个源必须显式
声明 `sport_category` 与 `source_scope`，采集层据此决定要不要逐条分类。

**source_scope 的三种取值**
  · `football_only` —— 已核实只出足球内容的源。条目可直接入池，仍会在推送前复核。
  · `mixed_sport`   —— 综合体育源。**每一条都必须过 `sport_scope.classify_sport()`**，
                        判不出是足球的一律丢弃。综合体育源永远不获得足球白名单资格。
  · `unknown`       —— 无法确认覆盖范围。**不得进入生产采集**。

新增来源时必须填全七个字段，否则 `validate_registry()` 会报错，
而不是像以前那样「加一行 URL 就上线」。
"""

import sport_scope as ss

FOOTBALL_ONLY = "football_only"
MIXED_SPORT = "mixed_sport"
UNKNOWN_SCOPE = "unknown"

VALID_SCOPES = frozenset({FOOTBALL_ONLY, MIXED_SPORT, UNKNOWN_SCOPE})
REQUIRED_FIELDS = (
    "source_id", "feed_url", "publisher", "domain",
    "sport_category", "source_scope", "enabled",
)


def _feed(source_id, feed_url, publisher, domain, sport_category,
          source_scope, enabled=True, note=""):
    return {
        "source_id": source_id,
        "feed_url": feed_url,
        "publisher": publisher,
        "domain": domain,
        "sport_category": sport_category,
        "source_scope": source_scope,
        "enabled": enabled,
        "note": note,
    }


# ==================== 登记表 ====================
# 顺序与 bot.py 原来的 RSS_FEEDS 保持一致，telemetry 里的源顺序不变。
FEEDS = (
    # ---- 1. 官方与裁判仲裁层 ----
    _feed("thefa", "https://www.thefa.com/rss", "The FA", "thefa.com",
          ss.FOOTBALL, FOOTBALL_ONLY, note="无原生 RSS，走站点检索降级"),
    _feed("premierleague", "https://www.premierleague.com/rss/news",
          "Premier League", "premierleague.com",
          ss.FOOTBALL, FOOTBALL_ONLY, note="端点已下线，走站点检索降级"),

    # ---- 2. 顶级权威主流媒体 ----
    _feed("bbc_football", "https://feeds.bbci.co.uk/sport/football/rss.xml",
          "BBC Sport", "bbc.co.uk",
          ss.FOOTBALL, FOOTBALL_ONLY, note="BBC 足球频道专属 feed"),
    # ⚠️ 这一条是 F1 事故的入口。实测 channel title = "SkySports | News"，
    # 20 条里约一半不是足球（F1 / 高尔夫 / 飞镖 / 橄榄球 / 拳击 / 赛马）。
    # 保留它是因为 Sky 的独家视频与实时博客有价值，但必须标 mixed_sport，
    # 由采集层逐条分类，绝不因为它挂在 skysports.com 下就当足球。
    _feed("skysports_news", "https://www.skysports.com/rss/12040",
          "Sky Sports", "skysports.com",
          ss.OTHER, MIXED_SPORT,
          note="全站综合体育 feed（channel: SkySports | News），必须逐条分类"),
    # The Athletic 的 /feed/ 同样是全站 feed，且 403 Cloudflare 无降级方案
    _feed("theathletic", "https://theathletic.com/feed/", "The Athletic",
          "theathletic.com", ss.OTHER, MIXED_SPORT,
          note="全站 feed，403 Cloudflare 风控，无可用降级"),

    # ---- 3. 爆料、八卦与财务舆论场 ----
    _feed("dailymail_football",
          "https://www.dailymail.com/sport/football/index.rss",
          "Daily Mail", "dailymail.com", ss.FOOTBALL, FOOTBALL_ONLY,
          note="足球频道专属；dailymail.co.uk 已 301 到 .com"),
    _feed("thesun_football", "https://www.thesun.co.uk/sport/football/feed/",
          "The Sun", "thesun.co.uk", ss.FOOTBALL, FOOTBALL_ONLY,
          note="注意是 .co.uk；thesun.com SSL 证书错误"),

    # ---- 4. 顶级名记与转会/跟队信源 ----
    _feed("twitter_ornstein", "https://rsshub.app/twitter/user/David_Ornstein",
          "David Ornstein", "rsshub.app", ss.FOOTBALL, FOOTBALL_ONLY),
    _feed("twitter_romano", "https://rsshub.app/twitter/user/FabrizioRomano",
          "Fabrizio Romano", "rsshub.app", ss.FOOTBALL, FOOTBALL_ONLY),
    _feed("twitter_mattlaw", "https://rsshub.app/twitter/user/Matt_Law_DT",
          "Matt Law", "rsshub.app", ss.FOOTBALL, FOOTBALL_ONLY),
    _feed("twitter_samlee", "https://rsshub.app/twitter/user/SamLee",
          "Sam Lee", "rsshub.app", ss.FOOTBALL, FOOTBALL_ONLY),

    # ---- 5. Big 6 官方及核心跟队直连 ----
    _feed("arsenal_site", "https://www.arsenal.com/rss/news", "Arsenal",
          "arsenal.com", ss.FOOTBALL, FOOTBALL_ONLY, note="503，走 site: 降级"),
    _feed("twitter_charleswatts", "https://rsshub.app/twitter/user/CharlesWatts",
          "Charles Watts", "rsshub.app", ss.FOOTBALL, FOOTBALL_ONLY),
    _feed("twitter_manutd", "https://rsshub.app/twitter/user/ManUtd", "Man Utd",
          "rsshub.app", ss.FOOTBALL, FOOTBALL_ONLY),
    _feed("twitter_mancity", "https://rsshub.app/twitter/user/ManCity", "Man City",
          "rsshub.app", ss.FOOTBALL, FOOTBALL_ONLY),
    _feed("twitter_arsenal", "https://rsshub.app/twitter/user/Arsenal", "Arsenal",
          "rsshub.app", ss.FOOTBALL, FOOTBALL_ONLY),
    _feed("twitter_lfc", "https://rsshub.app/twitter/user/LFC", "Liverpool",
          "rsshub.app", ss.FOOTBALL, FOOTBALL_ONLY),
    _feed("twitter_chelsea", "https://rsshub.app/twitter/user/ChelseaFC",
          "Chelsea", "rsshub.app", ss.FOOTBALL, FOOTBALL_ONLY),
    _feed("twitter_spurs", "https://rsshub.app/twitter/user/SpursOfficial",
          "Tottenham", "rsshub.app", ss.FOOTBALL, FOOTBALL_ONLY),
)


# ==================== 派生索引 ====================
_BY_URL = {f["feed_url"]: f for f in FEEDS}

# 采集层真正使用的列表。关掉某个源只需把 enabled 置 False，不必删行。
RSS_FEEDS = [f["feed_url"] for f in FEEDS if f["enabled"]]

# unknown 范围的源不得进入生产采集——这里直接从采集列表里剔除，
# 避免「登记了但忘了关」变成线上污染。
_BLOCKED = {f["feed_url"] for f in FEEDS
            if f["source_scope"] == UNKNOWN_SCOPE or not f["enabled"]}
RSS_FEEDS = [u for u in RSS_FEEDS if u not in _BLOCKED]


def entry_for(feed_url):
    """按 feed 地址取登记项；未登记返回 None。"""
    return _BY_URL.get((feed_url or "").strip())


def scope_of(feed_url):
    """取采集范围。**未登记的源一律视为 unknown**（= 不得采集）。

    这是刻意的默认值：新增一个源却忘了登记，结果应该是「不采集」，
    而不是「当成足球源全量采集」。
    """
    e = entry_for(feed_url)
    return e["source_scope"] if e else UNKNOWN_SCOPE


def category_of(feed_url):
    e = entry_for(feed_url)
    return e["sport_category"] if e else ss.UNKNOWN


def requires_item_check(feed_url):
    """该源是否必须逐条做领域分类。

    `football_only` 不强制（否则每条都要跑一遍正则，且官方源偶尔发错内容
    由推送前复核兜底）；`mixed_sport` 必须逐条分类；`unknown` 不采集。
    """
    return scope_of(feed_url) == MIXED_SPORT


def validate_registry():
    """自检登记表，返回问题列表（空 = 通过）。测试会直接调它。"""
    problems = []
    seen_ids, seen_urls = set(), set()
    for f in FEEDS:
        missing = [k for k in REQUIRED_FIELDS if k not in f]
        if missing:
            problems.append(f"{f.get('feed_url', '?')} 缺少字段 {missing}")
            continue
        if f["source_id"] in seen_ids:
            problems.append(f"source_id 重复：{f['source_id']}")
        if f["feed_url"] in seen_urls:
            problems.append(f"feed_url 重复：{f['feed_url']}")
        seen_ids.add(f["source_id"])
        seen_urls.add(f["feed_url"])
        if f["source_scope"] not in VALID_SCOPES:
            problems.append(f"{f['source_id']} 的 source_scope 非法：{f['source_scope']}")
        if f["sport_category"] not in ss.ALL_CATEGORIES:
            problems.append(f"{f['source_id']} 的 sport_category 非法：{f['sport_category']}")
        if f["source_scope"] == MIXED_SPORT and f["sport_category"] == ss.FOOTBALL:
            problems.append(
                f"{f['source_id']} 标为 mixed_sport 却声明 sport_category=Football——"
                f"综合体育源不得声称自己是足球源")
        if not isinstance(f["enabled"], bool):
            problems.append(f"{f['source_id']} 的 enabled 必须是布尔值")
    return problems
