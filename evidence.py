"""证据核验引擎（阶段 1.1 / 1.2 / 4.1）

设计原则
--------
1. **程序算状态，模型只提供事实**。模型可以给出 source_claims / evidence_conflict，
   但最终 evidence_status 一律由本模块根据「证据记录 + 域名 + 标题指纹」计算，
   模型自报的 evidence_sources_count / evidence_status 会被丢弃覆盖。
2. **有 grounding 不等于已核验**。必须同时满足：证据 URL 与 evidence_url 可对应、
   且该 URL 不是聚合器跳转链接、且来源等级可识别。
3. **转载不算独立来源**。同一篇稿子被十家媒体转载，标题指纹相同 → 只算 1 个来源。

证据状态（对外唯一枚举）
------------------------
OFFICIAL_CONFIRMED            官方渠道直接确认
MULTIPLE_INDEPENDENT_SOURCES  两家以上彼此独立的报道
SINGLE_SOURCE                 仅一个来源
UNVERIFIED                    无证据 / 证据对不上 / 无法判定
CONFLICTING                   来源之间说法冲突
"""

from urllib.parse import urlparse, urlunparse
import re

# ==================== 证据状态 ====================
OFFICIAL_CONFIRMED = "OFFICIAL_CONFIRMED"
MULTIPLE_INDEPENDENT_SOURCES = "MULTIPLE_INDEPENDENT_SOURCES"
SINGLE_SOURCE = "SINGLE_SOURCE"
UNVERIFIED = "UNVERIFIED"
CONFLICTING = "CONFLICTING"

ALL_STATUSES = (
    OFFICIAL_CONFIRMED,
    MULTIPLE_INDEPENDENT_SOURCES,
    SINGLE_SOURCE,
    UNVERIFIED,
    CONFLICTING,
)

# 只有这三种允许进入自动推送；UNVERIFIED / CONFLICTING 必须人工或下一轮再核。
PUSHABLE_STATUSES = (OFFICIAL_CONFIRMED, MULTIPLE_INDEPENDENT_SOURCES, SINGLE_SOURCE)

# ==================== 来源分级 ====================
# 官方：俱乐部 / 联赛 / 足总 / 仲裁机构
OFFICIAL_DOMAINS = {
    "thefa.com", "premierleague.com", "efl.com", "uefa.com", "fifa.com",
    "arsenal.com", "manutd.com", "mancity.com", "liverpoolfc.com",
    "chelseafc.com", "tottenhamhotspur.com", "avfc.co.uk", "evertonfc.com",
    "newcastleunited.com", "whufc.com", "brentfordfc.com", "fulhamfc.com",
    "cpfc.co.uk", "wolves.co.uk", "safc.com", "nufc.co.uk",
}

# 一线权威媒体
TIER1_DOMAINS = {
    "bbc.co.uk", "bbc.com", "skysports.com", "theathletic.com", "theguardian.com",
    "telegraph.co.uk", "independent.co.uk", "reuters.com", "apnews.com",
    "espn.com", "espn.co.uk", "nytimes.com", "ft.com",
}

# 爆料 / 小报：可以采信「有这件事」，但不能单独构成 Tier 1 评级
LOW_TRUST_DOMAINS = {
    "thesun.co.uk", "dailymail.co.uk", "dailymail.com", "mirror.co.uk",
    "express.co.uk", "metro.co.uk", "goal.com", "caughtoffside.com",
    "footballinsider247.com", "thetransfertavern.com", "sun.co.uk",
}

# 聚合器 / 中转层：本身不是证据来源，只用来发现线索
AGGREGATOR_DOMAINS = {
    "news.google.com", "google.com", "rsshub.app", "feeds.feedburner.com",
    "feedproxy.google.com", "flipboard.com", "msn.com", "newsnow.co.uk",
    "apple.news", "smartnews.com",
}

_MULTI_LABEL_SUFFIXES = ("co.uk", "org.uk", "ac.uk", "com.au", "co.jp", "com.br", "co.nz")


def extract_domain(url):
    """取可注册域名：www.bbc.co.uk -> bbc.co.uk；news.google.com -> news.google.com。"""
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        host = urlparse(raw).netloc.lower()
    except Exception:
        return ""
    if not host:
        return ""
    host = host.split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    if ".".join(parts[-2:]) in _MULTI_LABEL_SUFFIXES:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def classify_source(domain):
    """把域名归到 OFFICIAL / TIER1 / LOW_TRUST / AGGREGATOR / UNKNOWN 五类之一。"""
    d = (domain or "").lower()
    if not d:
        return "UNKNOWN"
    for known in AGGREGATOR_DOMAINS:
        if d == known or d.endswith("." + known):
            return "AGGREGATOR"
    for known in OFFICIAL_DOMAINS:
        if d == known or d.endswith("." + known):
            return "OFFICIAL"
    for known in TIER1_DOMAINS:
        if d == known or d.endswith("." + known):
            return "TIER1"
    for known in LOW_TRUST_DOMAINS:
        if d == known or d.endswith("." + known):
            return "LOW_TRUST"
    return "UNKNOWN"


def canonical_url(url):
    """去掉查询串/锚点/末尾斜杠，统一小写 host，用于证据比对与去重。

    注意：Google News 的 /rss/articles/CBMi... 是加密跳转，剥掉参数也无法还原原文，
    因此这类链接不会被当成「可核验证据」，只会被标成 AGGREGATOR。
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        p = urlparse(raw)
    except Exception:
        return raw
    if not p.scheme or not p.netloc:
        return raw
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = p.path.rstrip("/")
    return urlunparse((p.scheme.lower(), host, path, "", "", ""))


def title_fingerprint(title):
    """标题指纹：去掉 Google News 的 ' - 媒体名' 后缀与所有标点。"""
    text = (title or "").strip()
    if " - " in text:
        text = text.rsplit(" - ", 1)[0]
    return "".join(ch for ch in text.lower() if ch.isalnum())


# ==================== 证据记录 ====================
def build_records(raw_records):
    """把原始记录规范化成可计算的证据条目。

    入参允许两种形态：
      · [{"url": ..., "title": ..., "publisher": ...}, ...]
      · ["https://...", ...]                  纯 URL 列表

    `publisher` 是 Google News RSS 的 <source url="...">，用来还原真实发布方域名。
    没有它的话，所有走 Google News 降级的稿件都会被判成「聚合器链接不可作为证据」，
    等于把整个降级矩阵的证据能力废掉。
    """
    records, seen = [], set()
    for raw in raw_records or []:
        if isinstance(raw, str):
            url, title, publisher = raw, "", ""
        elif isinstance(raw, dict):
            url = raw.get("url") or ""
            title = raw.get("title") or ""
            publisher = raw.get("publisher") or ""
        else:
            continue
        canon = canonical_url(url)
        if not canon or canon in seen:
            continue
        seen.add(canon)

        url_domain = extract_domain(url)
        pub_domain = extract_domain(publisher) if publisher else ""
        # 有发布方域名时以它为准（Google News 链接的真实来源），否则看链接本身
        domain = pub_domain or url_domain
        source_class = classify_source(domain)
        if not pub_domain and source_class == "AGGREGATOR":
            source_class = "AGGREGATOR"
        records.append({
            "url": url,
            "canonical": canon,
            "domain": domain,
            "url_domain": url_domain,
            "source_class": source_class,
            "title": (title or "").strip()[:200],
            "title_key": title_fingerprint(title),
        })
    return records


_STOPWORDS = {
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "at", "by",
    "with", "from", "as", "is", "are", "be", "after", "over", "into", "amid",
    "news", "update", "latest", "live", "report", "says", "said",
}


def _tokens(text):
    """标题分词：英文按词、中文按字，去掉停用词与单字符噪声。"""
    raw = (text or "").lower()
    words = {w for w in re.findall(r"[a-z0-9]{2,}", raw) if w not in _STOPWORDS}
    if not words:
        # 纯中文标题退化为单字集合，仍能反映主题重叠度
        words = set(re.findall(r"[\u4e00-\u9fff]", raw))
    return words


def _jaccard(a, b):
    """相似度：Jaccard 与「包含度」取较大值。

    只用 Jaccard 会误杀短标题——「Arsenal set to sign striker」和
    「Arsenal rule out striker signing」的 Jaccard 只有 0.29，但包含度 0.5，
    明显是同一件事的两种说法。取 max 更贴合实际。
    """
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    union = len(a | b)
    jac = inter / union if union else 0.0
    # 只重合 1 个词时包含度会虚高（1/3=0.33），会把完全无关的稿子算成同一事件，
    # 因此至少要有 2 个共同词才启用包含度加成。
    if inter < 2:
        return jac
    return max(jac, inter / min(len(a), len(b)))


# 同一事件的不同报道，标题词重叠度经验值约 0.3~0.6；低于 0.3 基本是另一件事
RELATED_THRESHOLD = 0.30


def filter_related(records, anchor):
    """只保留与锚点报道「说的是同一件事」的记录。

    这一步很关键：新闻池里有几百条互不相关的稿件，如果全算进独立来源，
    任何一条新闻都会被判成「多源印证」。用标题词重叠度做粗筛，
    宁可漏掉几个真来源，也不能把无关报道算成交叉印证。
    """
    anchor_tokens = _tokens(anchor.get("title"))
    if not anchor_tokens:
        return [anchor]
    related = []
    for r in records:
        if r is anchor:
            related.append(r)
            continue
        if _jaccard(anchor_tokens, _tokens(r.get("title"))) >= RELATED_THRESHOLD:
            related.append(r)
    return related


def independent_sources(records):
    """按「标题指纹」聚合，统计真正独立的报道来源。

    同一篇稿子被多家转载时标题几乎一致 → 归为同一组 → 只算 1 个来源。
    没有标题的记录退化为按域名聚合。
    """
    groups = {}
    for r in records:
        if r["source_class"] == "AGGREGATOR":
            continue
        key = r["title_key"] if len(r["title_key"]) >= 12 else f"domain:{r['domain']}"
        groups.setdefault(key, set()).add(r["domain"])
    return [{"key": k, "domains": sorted(v)} for k, v in groups.items()]


def detect_conflict(source_claims, model_conflict_flag=False):
    """判定来源间是否互相矛盾。

    只看结构化事实：同一 claim_key 下出现 supports / denies 两种 stance 即为冲突。
    模型的 evidence_conflict 布尔值只能作为「加一条线索」，不能单独定案。
    """
    if model_conflict_flag:
        return True, "模型报告来源说法冲突"
    stances = {}
    for claim in source_claims or []:
        if not isinstance(claim, dict):
            continue
        key = (claim.get("claim_key") or claim.get("claim") or "").strip().lower()
        stance = (claim.get("stance") or "").strip().lower()
        if not key or stance not in ("supports", "denies"):
            continue
        stances.setdefault(key, set()).add(stance)
    for key, s in stances.items():
        if len(s) > 1:
            return True, f"同一事实出现相反表述：{key}"
    return False, ""


def evaluate(evidence_url, records, source_claims=None, model_conflict_flag=False):
    """计算证据状态，返回结构化结果（供主程序与日志共用）。

    返回：
      {
        "status": <五种状态之一>,
        "count": 独立来源数（程序计算）,
        "sources": [{domain, source_class, url}, ...],
        "reason": 一句话说明,
        "evidence_url_matched": bool,
      }
    """
    # 允许传入原始记录（str / {url,title}）或已规范化的记录，统一成后者
    if records and not (isinstance(records[0], dict) and "canonical" in records[0]):
        records = build_records(records)
    records = records or []

    target = canonical_url(evidence_url)
    matched = [r for r in records if target and r["canonical"] == target]

    def result(status, reason, matched_flag=False, groups=None, sources=None):
        groups = groups or []
        return {
            "status": status,
            "count": len(groups),
            "sources": sources or [],
            "reason": reason,
            "evidence_url_matched": matched_flag,
        }

    if not records:
        return result(UNVERIFIED, "没有任何可用证据记录（既无 grounding，也无抓取到的原始报道）")

    if not target:
        return result(UNVERIFIED, "模型未给出 evidence_url")

    if not matched:
        # 核心规则：有 URL 不等于有关联。evidence_url 必须是我们真正见过的那一篇。
        return result(UNVERIFIED, "evidence_url 不在证据集合中（疑似自行编造或引用旧稿）")

    anchor = matched[0]
    if anchor["source_class"] == "AGGREGATOR":
        return result(UNVERIFIED, "evidence_url 是聚合器跳转链接，无法定位原始报道")

    # 只统计「说的是同一件事」的记录，否则新闻池里几百条无关稿件会把一切都算成多源
    related = filter_related(records, anchor)
    non_agg = [r for r in related if r["source_class"] != "AGGREGATOR"]
    if not non_agg:
        return result(UNVERIFIED, "相关报道全部来自聚合器链接，无法定位原始来源")

    groups = independent_sources(related)
    sources = [
        {"domain": r["domain"], "source_class": r["source_class"], "url": r["url"]}
        for r in non_agg
    ]
    count = len(groups)

    conflict, conflict_reason = detect_conflict(source_claims, model_conflict_flag)
    if conflict and count >= 2:
        return result(CONFLICTING, conflict_reason or "来源之间说法冲突", True, groups, sources)

    official = [r for r in non_agg if r["source_class"] == "OFFICIAL"]
    if official:
        return result(OFFICIAL_CONFIRMED, f"官方渠道确认（{official[0]['domain']}）", True, groups, sources)

    if count >= 2:
        domains = ", ".join(g["domains"][0] for g in groups[:3])
        return result(MULTIPLE_INDEPENDENT_SOURCES, f"{count} 个独立来源交叉印证（{domains}）",
                      True, groups, sources)

    if count == 1:
        only = groups[0]["domains"][0]
        cls = classify_source(only)
        label = "低可信爆料源" if cls == "LOW_TRUST" else "单一来源"
        return result(SINGLE_SOURCE, f"{label}（{only}），未经交叉印证", True, groups, sources)

    return result(UNVERIFIED, "证据无法归并为有效来源", True, groups, sources)


def is_pushable(status):
    """UNVERIFIED / CONFLICTING 一律不得自动推送。"""
    return status in PUSHABLE_STATUSES


def status_label(status):
    return {
        OFFICIAL_CONFIRMED: "官方确认",
        MULTIPLE_INDEPENDENT_SOURCES: "多源独立印证",
        SINGLE_SOURCE: "单一来源",
        UNVERIFIED: "未核验",
        CONFLICTING: "来源冲突",
    }.get(status, status or "未核验")


# ==================== 评级复核（阶段 4.1 的程序侧兜底） ====================
def max_allowed_rating_key(status, source_classes):
    """返回程序允许的最高评级 key（只降不升）。

    - 未核验 / 冲突 → 最多 Tier 3
    - 单一低可信源 → 最多 Tier 3
    - 单一来源（非低可信）→ 最多 Tier 2
    - 多源 / 官方 → 不限制（由模型按素材判断）
    """
    if status in (UNVERIFIED, CONFLICTING):
        return "Tier 3"
    if status == SINGLE_SOURCE:
        if "LOW_TRUST" in (source_classes or []):
            return "Tier 3"
        return "Tier 2"
    return None


def enforce_rating(rating_key, status, source_classes):
    """把模型给的评级压到程序允许的上限，返回 (最终 key, 是否被降级, 原因)。

    黑标是「严肃红线」的内容安全标记，不是可信度分级，任何情况下都不降级，
    否则死亡/违法类新闻会被降成吃瓜黄牌，卡片模板也会切错。
    """
    if rating_key == "黑标":
        return rating_key, False, ""
    limit = max_allowed_rating_key(status, source_classes)
    order = {"Tier 1": 1, "Tier 2": 2, "Tier 3": 3, "黑标": 0}
    if not limit or not rating_key:
        return rating_key, False, ""
    if order.get(rating_key, 9) < order.get(limit, 9):
        return limit, True, f"证据等级为 {status_label(status)}，评级由 {rating_key} 降为 {limit}"
    return rating_key, False, ""
