"""证据核验引擎（阶段 1.1 / 1.2 / 4.1 + V1.1 第一批）

设计原则
--------
1. **程序算状态，模型只提供事实**。模型可以给出 source_claims / evidence_conflict，
   但最终 evidence_status 一律由本模块根据「证据记录 + 域名 + 标题指纹」计算，
   模型自报的 evidence_sources_count / evidence_status 会被丢弃覆盖。
2. **有 grounding 不等于已核验**。必须同时满足：证据 URL 与 evidence_url 可对应、
   且该 URL 不是聚合器跳转链接、且来源等级可识别。
3. **转载不算独立来源**。同一篇稿子被十家媒体转载，只算 1 个来源。
4. **官方确认必须证明是同一件具体事件**（V1.1 任务 1）。仅仅「某个官方域名
   也报道了这家俱乐部」不构成确认——那可能完全是另一件事。
5. **无法识别身份的域名不构成交叉印证**（V1.1 任务 2）。UNKNOWN 域名再多，
   也最多只能算「一个来路不明的来源」。

证据状态（对外唯一枚举）
------------------------
OFFICIAL_CONFIRMED            官方渠道直接确认，且指向同一件具体事件
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

# 「状态层面」允许进入自动推送的集合。
# 注意：这只是第一道门。SINGLE_SOURCE 还要再过 auto_push_allowed() 的单源闸门
# （V1.1 任务 4：单一来源默认不得自动推送，只有白名单内才放行）。
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

# 通讯社：一篇通稿被多少家转载，都只是「一个原始报道」（V1.1 任务 3）
WIRE_DOMAINS = {
    "reuters.com", "apnews.com", "afp.com", "pa.media",
    "pressassociation.com", "dpa.com", "bloomberg.com",
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


def _domain_in(domain, known_set):
    d = (domain or "").lower()
    if not d:
        return False
    for known in known_set:
        if d == known or d.endswith("." + known):
            return True
    return False


def classify_source(domain):
    """把域名归到 OFFICIAL / TIER1 / LOW_TRUST / AGGREGATOR / UNKNOWN 五类之一。

    这里是全项目**唯一的**来源分级白名单入口。任何模块都不得另建一份分级表，
    否则「同一个域名在 A 处可信、在 B 处不可信」，核验结果就无法解释。
    """
    d = (domain or "").lower()
    if not d:
        return "UNKNOWN"
    if _domain_in(d, AGGREGATOR_DOMAINS):
        return "AGGREGATOR"
    if _domain_in(d, OFFICIAL_DOMAINS):
        return "OFFICIAL"
    if _domain_in(d, TIER1_DOMAINS):
        return "TIER1"
    if _domain_in(d, LOW_TRUST_DOMAINS):
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


# ==================== 事件类型词典（V1.1 任务 1） ====================
# 同一家俱乐部的「转会」和「受伤」是两件事。只靠标题词重叠度区分不出来，
# 必须显式识别事件类型，否则官方站上任何一条同俱乐部新闻都会被当成「官方确认」。
EVENT_KEYWORDS = {
    "TRANSFER": {
        "transfer", "transfers", "sign", "signs", "signed", "signing", "bid", "bids",
        "loan", "move", "moves", "fee", "swoop", "approach", "agree", "agrees",
        "agreed", "deal", "deals", "sell", "sold", "buy", "buys", "medical",
        "hijack", "offer", "swap", "capture", "recruit", "target", "talks",
    },
    "CONTRACT": {
        "contract", "extension", "extend", "extends", "extended", "wages",
        "salary", "renew", "renewal", "clause",
    },
    "INJURY": {
        "injury", "injuries", "injured", "hamstring", "knee", "ankle", "groin",
        "calf", "thigh", "surgery", "fitness", "knock", "setback", "crock",
        "scan", "rehab", "sidelined",
    },
    "DISCIPLINE": {
        "ban", "bans", "banned", "suspension", "suspended", "disciplinary",
        "charge", "charges", "appeal", "appeals", "misconduct", "red", "card",
        "fine", "fined", "tribunal",
    },
    "MATCH": {
        "win", "wins", "won", "lose", "loses", "lost", "draw", "draws", "defeat",
        "beat", "beats", "victory", "result", "goal", "goals", "thrashing",
        "comeback", "highlights", "fixture", "clash", "derby",
    },
    "APPOINTMENT": {
        "appoint", "appointed", "manager", "coach", "sacked", "sack", "resign",
        "resigns", "interim", "replace", "replaces", "dugout", "boss",
    },
    "DEATH": {"dies", "died", "death", "dead", "funeral", "tribute", "mourning"},
    "LEGAL": {
        "arrest", "arrested", "court", "police", "jailed", "jail", "trial",
        "allegation", "allegations", "investigation", "probe", "lawsuit", "fraud",
    },
    "ILLNESS": {
        "illness", "cancer", "hospital", "hospitalised", "hospitalized",
        "intensive", "condition",
    },
}

# 中文标题走子串匹配（中文没有空格分词）
EVENT_KEYWORDS_ZH = {
    "TRANSFER": ("转会", "签下", "签约", "租借", "报价", "引援", "买断", "转出"),
    "CONTRACT": ("续约", "合同", "薪资", "解约金"),
    "INJURY": ("受伤", "伤病", "伤缺", "手术", "赛季报销", "拉伤"),
    "DISCIPLINE": ("红牌", "禁赛", "停赛", "处罚", "申诉", "纪律"),
    "MATCH": ("战胜", "击败", "输给", "战平", "比分", "进球"),
    "APPOINTMENT": ("主帅", "教练", "解雇", "下课", "任命", "接任"),
    "DEATH": ("去世", "逝世", "离世", "哀悼", "葬礼"),
    "LEGAL": ("被捕", "起诉", "法院", "警方", "判刑", "调查", "指控"),
    "ILLNESS": ("重病", "癌症", "住院", "病危"),
}

# 俱乐部主体词表。只放**无歧义**的写法：united / city / town 这类后缀太泛，
# 同时对应多家俱乐部，放进来只会制造假匹配，因此刻意排除。
CLUB_TOKENS = {
    "arsenal", "gunners", "chelsea", "liverpool", "everton", "tottenham",
    "spurs", "newcastle", "brighton", "brentford", "fulham", "palace",
    "westham", "wolves", "villa", "leeds", "burnley", "sheffield",
    "nottingham", "forest", "bournemouth", "sunderland", "southampton",
    "leicester", "ipswich", "watford", "norwich", "celtic", "rangers",
    "阿森纳", "切尔西", "利物浦", "曼联", "曼城", "热刺", "埃弗顿", "纽卡斯尔",
}


def _token_list(text):
    """保序分词（用于定位事件类型词出现的先后）。"""
    raw = (text or "").lower()
    return [w for w in re.findall(r"[a-z0-9]{2,}", raw) if w not in _STOPWORDS]


def _event_hits(title):
    """返回 {事件类型: (命中次数, 首次出现位置)}。"""
    text = (title or "").lower()
    if not text:
        return {}
    toks = _token_list(text)
    hits = {}
    for etype, kws in EVENT_KEYWORDS.items():
        positions = [i for i, t in enumerate(toks) if t in kws]
        count = len(positions)
        first = positions[0] if positions else None
        for zh in EVENT_KEYWORDS_ZH.get(etype, ()):
            n = text.count(zh)
            if n:
                count += n
                pos = text.find(zh)
                first = pos if first is None else min(first, pos)
        if count:
            hits[etype] = (count, first if first is not None else 10 ** 6)
    return hits


def primary_event_type(title):
    """主事件类型：命中次数最多者优先；同分取在标题中更早出现者。

    只看「类型集合是否有交集」不够用：
    「Arsenal agree deal for striker as transfer talks advance」与
    「Arsenal confirm striker injury after transfer window opens」都含 transfer，
    集合相交，但一件是转会、一件是伤情，是两件事。主类型能区分开。
    """
    hits = _event_hits(title)
    if not hits:
        return ""
    ordered = sorted(hits.items(), key=lambda kv: (-kv[1][0], kv[1][1], kv[0]))
    return ordered[0][0]


def event_subjects(title):
    """标题里的俱乐部主体（用于判断是不是同一支球队的事）。"""
    text = (title or "").lower()
    found = set()
    for w in re.findall(r"[a-z]{3,}", text):
        if w in CLUB_TOKENS:
            found.add(w)
    for zh in CLUB_TOKENS:
        if re.search(r"[\u4e00-\u9fff]", zh) and zh in text:
            found.add(zh)
    return found


_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(m|million|k|bn|billion|%|year|years|yr|yrs)\b")


def _key_numbers(title):
    """抽取带单位的关键数字（金额/年限/百分比），用于发现「关键事实不一致」。"""
    return {f"{m.group(1)}{m.group(2)[0]}" for m in _NUM_RE.finditer((title or "").lower())}


# ==================== 证据记录 ====================
def build_records(raw_records):
    """把原始记录规范化成可计算的证据条目。

    入参允许两种形态：
      · [{"url": ..., "title": ..., "publisher": ..., "desc": ...}, ...]
      · ["https://...", ...]                  纯 URL 列表

    `publisher` 是 Google News RSS 的 <source url="...">，用来还原真实发布方域名。
    没有它的话，所有走 Google News 降级的稿件都会被判成「聚合器链接不可作为证据」，
    等于把整个降级矩阵的证据能力废掉。

    `desc` 是 RSS 摘要，用于「同一篇稿子被改标题转载」的正文相似度判定。
    """
    records, seen = [], set()
    for raw in raw_records or []:
        if isinstance(raw, str):
            url, title, publisher, desc = raw, "", "", ""
        elif isinstance(raw, dict):
            url = raw.get("url") or ""
            title = raw.get("title") or ""
            publisher = raw.get("publisher") or ""
            desc = raw.get("desc") or raw.get("description") or ""
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
        records.append({
            "url": url,
            "canonical": canon,
            "domain": domain,
            "url_domain": url_domain,
            "source_class": source_class,
            "title": (title or "").strip()[:200],
            "title_key": title_fingerprint(title),
            "desc": (desc or "").strip()[:400],
            "tokens": _tokens(title),
            "desc_tokens": _tokens(desc),
            "primary_type": primary_event_type(title),
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
# 「同一篇稿子被转载」的判定线：标题（或正文）几乎一字不差
REPRINT_THRESHOLD = 0.80


def _rec_tokens(record):
    t = record.get("tokens")
    return t if t is not None else _tokens(record.get("title"))


def _rec_type(record):
    t = record.get("primary_type")
    return t if t is not None else primary_event_type(record.get("title"))


def same_event(anchor, other):
    """判断两条记录是否在讲**同一件具体事件**。返回 (bool, 原因)。

    这是 V1.1 任务 1 的核心：官方确认必须落在「同一件事」上，而不是
    「同一个俱乐部 + 同一个官方域名」。四个维度都要过：
      主体（哪家俱乐部/谁）· 事件类型（转会/伤情/纪律…）· 关键事实（金额等）· 标题相关度
    """
    if anchor is other:
        return True, "同一条记录"
    a_canon, b_canon = anchor.get("canonical"), other.get("canonical")
    if a_canon and a_canon == b_canon:
        return True, "同一 URL"

    a_tok, b_tok = _rec_tokens(anchor), _rec_tokens(other)
    sim = _jaccard(a_tok, b_tok)
    if sim < RELATED_THRESHOLD:
        return False, f"标题相关度 {sim:.2f} 低于 {RELATED_THRESHOLD}"

    pa, pb = _rec_type(anchor), _rec_type(other)
    if pa and pb and pa != pb:
        return False, f"事件类型不同（{pa} vs {pb}）"

    sa, sb = event_subjects(anchor.get("title")), event_subjects(other.get("title"))
    if sa and sb and not (sa & sb):
        return False, f"主体不同（{'/'.join(sorted(sa))} vs {'/'.join(sorted(sb))}）"

    na, nb = _key_numbers(anchor.get("title")), _key_numbers(other.get("title"))
    if na and nb and not (na & nb):
        return False, f"关键数字不一致（{'/'.join(sorted(na))} vs {'/'.join(sorted(nb))}）"

    return True, "主体 / 事件类型 / 关键事实一致"


def filter_related(records, anchor):
    """只保留与锚点报道「说的是同一件具体事件」的记录。

    这一步很关键：新闻池里有几百条互不相关的稿件，如果全算进独立来源，
    任何一条新闻都会被判成「多源印证」。宁可漏掉几个真来源，
    也不能把无关报道算成交叉印证。
    """
    if not _rec_tokens(anchor):
        return [anchor]
    related = []
    for r in records:
        if r is anchor:
            related.append(r)
            continue
        ok, _why = same_event(anchor, r)
        if ok:
            related.append(r)
    return related


# ==================== 转载识别（V1.1 任务 3） ====================
_WIRE_MARKERS = re.compile(
    r"(?<![a-z])(reuters|press association|pa sport|agence france-presse|afp|dpa)(?![a-z])",
    re.I,
)


def _wire_service(record):
    """识别这条记录是不是通讯社通稿（域名或标题里的通稿署名）。"""
    d = (record.get("domain") or "").lower()
    if _domain_in(d, WIRE_DOMAINS):
        return d
    m = _WIRE_MARKERS.search(record.get("title") or "")
    return m.group(1).lower() if m else ""


def _report_similarity(a, b):
    """同一篇报道的相似度：标题为主，正文（RSS 摘要）为辅。

    标题高度一致 → 转载；标题只是相关、但正文几乎一字不差 → 也是转载
    （改标题洗稿是常见操作，「标题不同」不能作为「来源独立」的依据）。

    正文比对有严格前置条件：两边摘要都得有足够多的词（≥8 个不同词）。
    很多站的 RSS 摘要是一模一样的样板句（"Read more on our football page"），
    词少且完全相同，一旦放行就会把任意两篇稿子判成转载，等于废掉多源印证。
    """
    t = _jaccard(_rec_tokens(a), _rec_tokens(b))
    if t >= REPRINT_THRESHOLD:
        return t
    da, db = a.get("desc_tokens") or set(), b.get("desc_tokens") or set()
    if len(da) >= 8 and len(db) >= 8:
        d = _jaccard(da, db)
        if d >= 0.85 and t >= RELATED_THRESHOLD:
            return max(t, d)
    return t


def independent_sources(records):
    """把记录聚成「独立来源组」。

    规则（V1.1 任务 2 / 3）：
      1. 聚合器直接排除——它本身不是来源，只是线索入口。
      2. 通讯社通稿无论被多少家转载，只算 1 个来源。
      3. 其余按「同一篇报道」聚类（标题/正文相似度 ≥ 0.80 视为转载）。
      4. 全部由 UNKNOWN 域名构成的组标记为 trusted=False，不计入交叉印证数量。
    """
    buckets = []
    unknown_bucket = None

    for r in records:
        if r.get("source_class") == "AGGREGATOR":
            continue
        wire = _wire_service(r)

        target = None
        if wire:
            for b in buckets:
                if b["key"] == f"wire:{wire}":
                    target = b
                    break
        if target is None:
            for b in buckets:
                if _report_similarity(b["sample"], r) >= REPRINT_THRESHOLD:
                    target = b
                    break
        if target is None and r.get("source_class") == "UNKNOWN":
            if unknown_bucket is None:
                unknown_bucket = {"key": "unknown", "domains": set(), "sample": r, "trusted": False}
                buckets.append(unknown_bucket)
            target = unknown_bucket
        if target is None:
            key = f"wire:{wire}" if wire else f"story:{r.get('canonical') or r.get('domain')}"
            target = {"key": key, "domains": set(), "sample": r, "trusted": True}
            buckets.append(target)
        target["domains"].add(r.get("domain") or "")

    for b in buckets:
        # 只要组里有一个可识别的域名，这组就算「可信来源组」；
        # 全部 UNKNOWN 的组永远不能用于交叉印证。
        if b["key"] != "unknown" and any(classify_source(d) != "UNKNOWN" for d in b["domains"]):
            b["trusted"] = True

    return [
        {"key": b["key"], "domains": sorted(x for x in b["domains"] if x), "trusted": b["trusted"]}
        for b in buckets
    ]


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
        "count": 独立来源数（程序计算，UNKNOWN 不计入交叉印证）,
        "sources": [{domain, source_class, url}, ...],
        "reason": 一句话说明,
        "evidence_url_matched": bool,
        "official_rejected": [域名, ...],   官方域名指向了别的事件
      }
    """
    # 允许传入原始记录（str / {url,title}）或已规范化的记录，统一成后者
    if records and not (isinstance(records[0], dict) and "canonical" in records[0]):
        records = build_records(records)
    records = records or []

    target = canonical_url(evidence_url)
    matched = [r for r in records if target and r["canonical"] == target]

    def result(status, reason, matched_flag=False, groups=None, sources=None,
               rejected=None, primary=None):
        groups = groups or []
        return {
            "status": status,
            "count": len(groups),
            "sources": sources or [],
            "reason": reason,
            "evidence_url_matched": matched_flag,
            "official_rejected": rejected or [],
            "primary_source": primary or {},
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

    # 只统计「说的是同一件具体事件」的记录，否则新闻池里几百条无关稿件会把一切都算成多源
    related = filter_related(records, anchor)
    non_agg = [r for r in related if r["source_class"] != "AGGREGATOR"]
    if not non_agg:
        return result(UNVERIFIED, "相关报道全部来自聚合器链接，无法定位原始来源")

    # ---- V1.1 任务 1：官方确认必须证明是同一件具体事件 ----
    # 注意这里刻意用「标题相关」（_report_similarity）而不是 filter_related 的结果：
    # 官方域名上那篇「相关的另一件事」已经被 filter_related 滤掉了，如果只在
    # related 里找官方记录，就永远看不到它，也就永远发现不了「官方确认不成立」。
    candidate_official = [
        r for r in records
        if r["source_class"] == "OFFICIAL"
        and _report_similarity(anchor, r) >= RELATED_THRESHOLD
    ]
    confirmed_official, rejected_official = [], []
    for r in candidate_official:
        ok_same, _why = same_event(anchor, r)
        (confirmed_official if ok_same else rejected_official).append(r)

    # 指向别的事件的官方记录，不是本事件的证据，必须从证据池里剔除，
    # 否则它会以「官方域名」的身份把 count 顶到 2，凭空制造交叉印证。
    rejected_ids = {id(r) for r in rejected_official}
    pool = [r for r in non_agg if id(r) not in rejected_ids]
    if not pool:
        return result(UNVERIFIED, "仅有官方域名报道，但指向另一事件，不构成本事件的证据")

    groups_all = independent_sources(pool)
    trusted = [g for g in groups_all if g["trusted"]]
    # UNKNOWN 域名不得计入交叉印证：只有在完全没有可信来源时，才把它算作「1 个来源」
    count = len(trusted) if trusted else (1 if groups_all else 0)
    sources = [
        {"domain": r["domain"], "source_class": r["source_class"], "url": r["url"]}
        for r in pool
    ]
    rejected_domains = sorted({r["domain"] for r in rejected_official})

    def groups_for_count():
        return trusted if trusted else groups_all

    conflict, conflict_reason = detect_conflict(source_claims, model_conflict_flag)
    if conflict and count >= 2:
        return result(CONFLICTING, conflict_reason or "来源之间说法冲突",
                      True, groups_for_count(), sources, rejected_domains)

    if confirmed_official:
        return result(OFFICIAL_CONFIRMED,
                      f"官方渠道确认同一事件（{confirmed_official[0]['domain']}）",
                      True, groups_for_count(), sources, rejected_domains)

    if rejected_official and count <= 1:
        # 任务书要求：官方来源证明不了同一事件时，官方确认不成立 → 降级为 UNVERIFIED
        return result(UNVERIFIED,
                      f"官方来源（{rejected_domains[0]}）指向另一事件，官方确认不成立；"
                      f"剩余证据仅 {count} 个来源",
                      True, groups_for_count(), sources, rejected_domains)

    if count >= 2:
        domains = ", ".join(g["domains"][0] for g in groups_for_count()[:3])
        return result(MULTIPLE_INDEPENDENT_SOURCES, f"{count} 个独立来源交叉印证（{domains}）",
                      True, groups_for_count(), sources, rejected_domains)

    if count == 1:
        only = (groups_for_count()[0]["domains"] or [""])[0]
        cls = classify_source(only)
        if cls == "LOW_TRUST":
            label = "低可信爆料源"
        elif cls == "UNKNOWN":
            label = "来路不明的单一来源"
        else:
            label = "单一来源"
        # 单一来源必须精确指向「那一个来源」的域名与 URL：
        # 新闻池是乱序的，直接取 sources[0] 可能拿到同事件的另一篇转载，
        # 结果卡片上写的出处和实际放行的域名对不上。
        primary_rec = next((r for r in pool if r["domain"] == only), anchor)
        primary = {"domain": only, "url": primary_rec["url"], "source_class": cls}
        return result(SINGLE_SOURCE, f"{label}（{only}），未经交叉印证",
                      True, groups_for_count(), sources, rejected_domains, primary)

    return result(UNVERIFIED, "证据无法归并为有效来源", True, groups_for_count(), sources, rejected_domains)


def is_pushable(status):
    """**状态层面**是否允许自动推送。UNVERIFIED / CONFLICTING 一律不得自动推送。

    注意这只是第一道门。真正决定「能不能发」的是 auto_push_allowed()：
    SINGLE_SOURCE 在状态层面是可推送的，但还要再过单源白名单闸门。
    """
    return status in PUSHABLE_STATUSES


# ==================== 单一来源闸门（V1.1 任务 4） ====================
# 默认策略：SINGLE_SOURCE 不自动推送。只有「人工确认过的可信单一来源」才放行，
# 且必须在卡片上显式标注「未经独立交叉验证」并给出原始来源名与 URL。
SINGLE_SOURCE_LABEL = "单一来源"


def single_source_allowed(verdict, allowlist=None):
    """SINGLE_SOURCE 是否允许自动推送。返回 (bool, 原因)。

    allowlist 是域名集合（来自环境变量 TRUSTED_SINGLE_SOURCES）。
    白名单只对 TIER1 / LOW_TRUST 中的「经人工确认的记者/机构」开放；
    UNKNOWN 域名永远不能进白名单——连是谁在说都不知道，无从谈可信。
    """
    if verdict.get("status") != SINGLE_SOURCE:
        return True, ""
    allow = {(d or "").strip().lower() for d in (allowlist or []) if d}
    primary = verdict.get("primary_source") or {}
    srcs = verdict.get("sources") or []
    if not primary and not srcs:
        return False, "单一来源但无法定位来源域名，默认不自动推送"
    dom = (primary.get("domain") or (srcs[0].get("domain") if srcs else "") or "").lower()
    cls = primary.get("source_class") or classify_source(dom)
    if cls == "UNKNOWN":
        return False, f"单一来源且域名未识别（{dom}），不允许自动推送"
    if dom not in allow:
        return False, f"单一来源（{dom}）不在可信白名单内，默认不自动推送"
    return True, ""


def auto_push_allowed(verdict, allowlist=None):
    """自动推送总闸门：状态门 + 单源门。"""
    status = verdict.get("status")
    if not is_pushable(status):
        return False, f"证据状态 {status_label(status)}（{verdict.get('reason') or ''}），不得自动推送"
    if status == SINGLE_SOURCE:
        return single_source_allowed(verdict, allowlist)
    return True, ""


def single_source_note(verdict):
    """单一来源推送时必须附带的标注。返回 {note, source_name, url}。"""
    if verdict.get("status") != SINGLE_SOURCE:
        return {}
    primary = verdict.get("primary_source") or {}
    srcs = verdict.get("sources") or []
    src = primary or (srcs[0] if srcs else {})
    return {
        "note": "未经独立交叉验证",
        "source_name": src.get("domain") or "未知来源",
        "url": src.get("url") or "",
    }


# ==================== 黑标证据闸门（V1.1 任务 5） ====================
BLACK_LABEL = "黑标"

# 严肃议题：死亡 / 重伤 / 重病 / 违法 / 重大事故。
# 这类内容一旦说错，代价远高于转会传闻，因此必须走最严的证据标准。
STRICT_TOPIC_KEYWORDS = {
    "DEATH": ("dies", "died", "death", "dead", "passed away", "funeral",
              "mourning", "去世", "逝世", "离世", "哀悼", "葬礼"),
    "ILLNESS": ("illness", "cancer", "tumour", "tumor", "hospitalised",
                "hospitalized", "intensive care", "critical condition",
                "重病", "癌症", "住院", "病危"),
    "LEGAL": ("arrest", "arrested", "charged with", "court", "police", "jailed",
              "jail", "trial", "fraud", "被捕", "起诉", "法院", "警方", "判刑", "指控"),
    "ACCIDENT": ("accident", "crash", "heart attack", "cardiac arrest",
                 "ambulance", "车祸", "事故", "心脏骤停"),
    "SEVERE_INJURY": ("serious injury", "serious knee", "serious ankle",
                      "serious head", "career-ending", "career ending",
                      "life-threatening", "intensive care", "重伤", "危及生命"),
}

_INJURY_WORDS = set(EVENT_KEYWORDS["INJURY"]) | {"injury", "injured", "injury-hit"}
_SEVERITY_WORDS = {
    "serious", "severe", "career", "career-ending", "life-threatening",
    "critical", "intensive", "coma", "broken", "fractured", "fracture",
    "hospital", "hospitalised", "hospitalized", "long-term",
}


def _kw_hit(low, kw):
    """关键词命中：英文按词边界（避免 dead 命中 deadline），中文按子串。"""
    if re.search(r"[\u4e00-\u9fff]", kw):
        return kw in low
    return re.search(r"(?<![a-z])" + re.escape(kw) + r"(?![a-z])", low) is not None


def strict_topic(text):
    """识别是否属于「必须严格核验」的严肃议题，返回话题名或空串。"""
    low = (text or "").lower()
    if not low:
        return ""
    for topic, kws in STRICT_TOPIC_KEYWORDS.items():
        for kw in kws:
            if _kw_hit(low, kw):
                return topic
    toks = set(re.findall(r"[a-z]+", low))
    if (toks & _INJURY_WORDS) and (toks & _SEVERITY_WORDS):
        return "SEVERE_INJURY"
    return ""


def black_label_gate(rating_key, verdict, text=""):
    """黑标闸门：返回 (是否放行, 原因)。

    规则：
      · 只要模型给了黑标，或内容本身属于死亡/重伤/重病/违法/重大事故，
        就要求 OFFICIAL_CONFIRMED 或 MULTIPLE_INDEPENDENT_SOURCES；
      · 单一来源（无论多可信）、UNKNOWN、UNVERIFIED、CONFLICTING 一律不得自动推送；
      · **模型自己写 var_rating=黑标 不能绕过本闸门**——本函数只看证据，
        不看模型有多自信，也不接受「模型说是官方」这种自我认证。
    """
    topic = strict_topic(text)
    is_black = (rating_key == BLACK_LABEL) or bool(topic)
    if not is_black:
        return True, ""
    status = verdict.get("status")
    if status in (OFFICIAL_CONFIRMED, MULTIPLE_INDEPENDENT_SOURCES):
        return True, ""
    return False, (
        f"严肃议题（{topic or '黑标'}）证据不足：当前为 {status_label(status)}，"
        f"必须有官方确认或多源独立印证才允许自动推送"
    )


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
    - 单一来源（低可信 / 未识别 / 无来源）→ 最多 Tier 3
    - 单一来源（一线权威）→ 最多 Tier 2
    - 多源 / 官方 → 不限制（由模型按素材判断）
    """
    if status in (UNVERIFIED, CONFLICTING):
        return "Tier 3"
    if status == SINGLE_SOURCE:
        classes = list(source_classes or [])
        if not classes or "LOW_TRUST" in classes or "UNKNOWN" in classes:
            return "Tier 3"
        return "Tier 2"
    return None


def enforce_rating(rating_key, status, source_classes):
    """把模型给的评级压到程序允许的上限，返回 (最终 key, 是否被降级, 原因)。

    黑标是「严肃红线」的内容安全标记，不是可信度分级，任何情况下都不降级，
    否则死亡/违法类新闻会被降成吃瓜黄牌，卡片模板也会切错。
    黑标的**证据门槛**由 black_label_gate() 单独负责，两者不要混在一起。
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
