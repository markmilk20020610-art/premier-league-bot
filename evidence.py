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
import hashlib
import re

import sport_scope

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

# ==================== Feed 信任边界（V1.2 任务 1） ====================
# RSS item 里的 <source url="..."> 是**发布方自己填的**，任何第三方 feed 都能写
# 一个官方域名进来。因此只有「我们确认过会如实转述发布方」的 feed，
# 才允许用它的 source 元数据去改写文章归属；其余 feed 一律以文章真实 URL 为准。
TRUSTED_PUBLISHER_FEEDS = {
    "news.google.com",     # Google News 的 <source> 来自其索引库，不是投稿方自填
}


def _host(url):
    """取原始主机名（不做可注册域名归并）。

    这里刻意不用 extract_domain：news.google.com 会被归并成 google.com，
    那样任意 google.com 子域都能冒充 Google News feed，等于把白名单放开。
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        host = urlparse(raw).netloc.lower()
    except Exception:
        return ""
    host = host.split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def feed_trusts_publisher(feed_origin):
    """这条 feed 的 <source> 元数据是否可信。"""
    host = _host(feed_origin)
    if not host:
        return False
    return any(host == f or host.endswith("." + f) for f in TRUSTED_PUBLISHER_FEEDS)


def source_id_for(canonical):
    """稳定的程序侧来源 ID：由规范化 URL 派生，模型无法伪造。"""
    if not canonical:
        return ""
    return "src_" + hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]



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
_STOPWORDS = {
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "at", "by",
    "with", "from", "as", "is", "are", "be", "after", "over", "into", "amid",
    "news", "update", "latest", "live", "report", "says", "said",
}

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
        # V1.3 P0-3：英媒写伤情常用 "limp off / limped off"，没有 injury 这个词。
        # 缺了它，这类标题连事件类型都判不出来，后面的人物比对也就无从谈起。
        "limp", "limps", "limped", "limping",
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


# ==================== 人物实体（V1.2 任务 3） ====================
# 没有 NER 可用，因此用「角色词 + 姓名位」的启发式：
#   "…sign winger Michael Olise" 里 winger 后面的连续大写词就是球员主体。
# 只在**人名是事件核心**的类型上启用比对，避免把 "Arsenal Star Suffers Injury"
# 这类标题里的普通词误当人名。
ROLE_WORDS = {
    "winger", "striker", "forward", "midfielder", "defender", "goalkeeper",
    "keeper", "fullback", "full-back", "centreback", "centre-back",
    "leftback", "rightback", "boss", "manager", "coach", "skipper",
    "captain", "signing", "capture", "ace",
}
SIGN_WORDS = {
    "sign", "signs", "signing", "signed", "capture", "captures", "captured",
    "recruit", "recruits", "unveil", "unveils", "appoint", "appoints",
    "appointed", "sack", "sacks", "sacked", "hire", "hires", "hired",
}

# 人名位排除表：这些词即使首字母大写也不是人名
_NAME_STOP = set(_STOPWORDS) | set(CLUB_TOKENS) | {
    "deal", "deals", "talks", "move", "moves", "news", "club", "clubs", "team",
    "teams", "squad", "season", "league", "cup", "game", "games", "match",
    "matches", "injury", "injuries", "window", "loan", "fee", "bid", "bids",
    "offer", "contract", "medical", "premier", "champions", "europa", "world",
    "fa", "uefa", "fifa", "sky", "bbc", "espn", "sport", "sports", "football",
    "united", "city", "town", "rovers", "county", "athletic", "wanderers",
    "albion", "fc", "afc", "suffers", "suffered", "serious", "severe", "injury",
    "signs", "signs", "joins", "joined", "wants", "eyes", "ready", "set",
    "close", "closes", "eyes", "star", "stars", "ace", "ace", "boss",
    "manager", "coach", "striker", "winger", "defender", "midfielder",
    "goalkeeper", "keeper", "forward", "captain", "skipper", "january",
    "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "monday", "tuesday",
    "wednesday", "thursday", "friday", "saturday", "sunday", "face", "faces",
    "win", "wins", "lose", "loses", "beat", "beats", "draw", "draws",
    "final", "semi", "round", "leg", "first", "second", "new", "old",
    "report", "reports", "update", "updates", "latest", "breaking", "exclusive",
    # V1.3 P0-3：这几个词经常紧挨着人名出现，被「相邻大写词」规则误当人名。
    # 一旦两边都含这些假人名，人物比对就恒成立，P0-3 的检查会被中和。
    "man", "utd", "back", "out",
}

# 相邻大写词但明确不是人名的组合（机构 / 赛事 / 球场）
NON_PERSON_BIGRAMS = {
    ("premier", "league"), ("fa", "cup"), ("carabao", "cup"),
    ("champions", "league"), ("europa", "league"), ("world", "cup"),
    ("sky", "sports"), ("old", "trafford"), ("transfer", "window"),
    ("deadline", "day"), ("real", "madrid"), ("bayern", "munich"),
    ("manchester", "united"), ("manchester", "city"), ("nottingham", "forest"),
    ("crystal", "palace"), ("aston", "villa"), ("newcastle", "united"),
    ("tottenham", "hotspur"), ("brighton", "hove"), ("hove", "albion"),
    ("sheffield", "united"), ("sheffield", "wednesday"), ("leeds", "united"),
    ("leicester", "city"), ("west", "ham"), ("west", "brom"),
    ("queens", "park"), ("park", "rangers"), ("st", "james"),
    ("st", "mary"), ("emirates", "stadium"), ("stamford", "bridge"),
    # V1.3 P0-3：缩写写法的俱乐部名。「Man Utd」「Man City」会被当成两个人名，
    # 而两个人名一旦在两边都出现，人物比对就永远成立——等于把 P0-3 的检查废掉。
    ("man", "utd"), ("man", "city"), ("man", "united"),
}

# 人名是事件核心的类型：这些类型下「主体不同」就是两件事
PERSON_CENTRIC_TYPES = {"TRANSFER", "CONTRACT", "APPOINTMENT", "DISCIPLINE", "INJURY"}


def _name_like(token):
    """是否像一个人名：首字母大写、纯字母、不在排除表里。"""
    if not token or len(token) < 3:
        return False
    if not token[0].isupper():
        return False
    if not re.fullmatch(r"[A-Za-z'\-]+", token):
        return False
    return token.lower() not in _NAME_STOP


# 人物动作词：紧跟/紧邻人名的动词。"Havertz limps off" 里没有角色词（winger/striker），
# 只有 limps 能说明前面的 Havertz 是球员。V1.3 P0-3 新增。
PERSON_VERBS = {
    # 伤情
    "limp", "limps", "limped", "limping", "injured", "injures", "suffers",
    "suffered", "sidelined", "doubtful", "fitness", "ruled",
    # 处罚 / 任命
    "banned", "bans", "suspended", "suspends", "sacked", "sacks", "axed",
    "appointed", "appoints", "resigns", "resigned",
    # 转会 / 合同
    "agrees", "agreed", "snubs", "snubbed", "signs", "signed", "joins",
    "joined", "leaves", "left", "returns", "returned", "want", "wants",
    "eye", "eyes", "chase", "chases", "target", "targets", "hijack", "hijacks",
    # 比赛表现
    "scores", "scored", "strikes", "struck", "nets", "netted", "misses",
    "missed", "faces", "facing", "starts", "started", "out", "fit", "back",
}

# 并列人名的连接词："Saka and Martinelli" / "Saka, Martinelli or Havertz"
PERSON_CONNECTORS = {"and", "or", "plus"}


def person_names(title):
    """抽取标题里的人物主体（球员 / 教练）。

    四条路径（V1.3 P0-3 把后两条补上）：
      1. 角色词之后紧跟的连续大写词（"winger Michael Olise"）
      2. 相邻两个大写词，且不是已知机构/赛事组合（"Michael Olise"）
      3. 并列人名：中间只夹一个连接词也算（"Saka and Martinelli"）
      4. 无角色词的单人名：靠紧邻的人物动作词确认（"Havertz limps off"）

    路径 3 / 4 是审计 P0-3 的修复点：缺了它们，"Saka and Martinelli limp off"
    与 "Havertz limps off" 都抽不出人名，于是两条完全不同的伤情被当成
    「同一事件的 2 个独立来源」，把 SINGLE_SOURCE 抬成多源印证并自动推送。
    """
    text = title or ""
    tokens = re.findall(r"[A-Za-z][A-Za-z'\-]*", text)
    if not tokens:
        return set()
    low = [t.lower() for t in tokens]
    n = len(tokens)
    found = set()

    # 路径 1：角色词 / 签约词之后紧跟的连续大写词
    for i, lt in enumerate(low):
        if lt in ROLE_WORDS or lt in SIGN_WORDS:
            j, taken = i + 1, 0
            while j < n and taken < 2:
                if not _name_like(tokens[j]):
                    break
                found.add(low[j])
                j += 1
                taken += 1

    # 路径 2：相邻两个大写词
    for i in range(n - 1):
        if _name_like(tokens[i]) and _name_like(tokens[i + 1]):
            if (low[i], low[i + 1]) not in NON_PERSON_BIGRAMS:
                found.add(low[i])
                found.add(low[i + 1])

    # 路径 3：并列人名——"Saka and Martinelli"。夹一个 and 就会把「相邻」打断，
    # 结果是并列的球员一个都抽不出来。
    for i in range(n - 2):
        if low[i + 1] in PERSON_CONNECTORS:
            if _name_like(tokens[i]) and _name_like(tokens[i + 2]):
                found.add(low[i])
                found.add(low[i + 2])

    # 路径 4：没有角色词的单人名。只在「人名就是事件核心」的类型上启用，
    # 且必须紧邻人物动作词，避免把 "Arsenal beat Chelsea at Emirates"
    # 里的 Emirates 误判成球员（那会让同一场比赛的两篇报道互相判为不同事件）。
    if primary_event_type(text) in PERSON_CENTRIC_TYPES:
        for i in range(n):
            if not _name_like(tokens[i]):
                continue
            if i + 1 < n and low[i + 1] in PERSON_VERBS:
                found.add(low[i])
            elif i > 0 and low[i - 1] in PERSON_VERBS:
                found.add(low[i])

    return found


# 带单位的关键数字：金额 / 年限 / 百分比
_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(m|million|k|bn|billion|%|year|years|yr|yrs)\b")
# 比分：2-1 / 3–0。前后各加一道边界，避免把阵型 4-3-3 和年份 2024-25 当成比分。
_SCORE_RE = re.compile(r"(?<![\d\-–—])(\d{1,2})\s*[-–—]\s*(\d{1,2})(?!\s*[-–—]\s*\d)")
# 中文比分：2比1
_SCORE_ZH_RE = re.compile(r"(\d{1,2})\s*比\s*(\d{1,2})")


def _key_numbers(title):
    """抽取关键数字（比分 / 金额 / 年限 / 百分比），用于发现「关键事实不一致」。

    V1.3 P0-3：原实现只认「数字 + 单位」，比分 2-1 抽不出来，于是
    "Arsenal beat Chelsea 2-1" 与 "Arsenal beat Chelsea 3-0" 被当成同一事件。
    两场不同的比赛互相作证，正是多源印证虚高的典型来源。
    """
    text = (title or "").lower()
    found = {f"{m.group(1)}{m.group(2)[0]}" for m in _NUM_RE.finditer(text)}
    for m in _SCORE_RE.finditer(text):
        found.add(f"score:{m.group(1)}-{m.group(2)}")
    for m in _SCORE_ZH_RE.finditer(text):
        found.add(f"score:{m.group(1)}-{m.group(2)}")
    return found


# ==================== 证据记录 ====================
def build_records(raw_records):
    """把原始记录规范化成可计算的证据条目。

    入参允许两种形态：
      · [{"url", "title", "publisher", "feed_origin", "desc"}, ...]
      · ["https://...", ...]                  纯 URL 列表

    `publisher` 是 RSS 的 <source url="...">，`feed_origin` 是这条记录来自哪个 feed。
    **信任规则（V1.2 任务 1）**：
      · 只有 TRUSTED_PUBLISHER_FEEDS 里的 feed 才能用 publisher 决定文章归属；
      · 其余 feed 的 publisher 一律忽略，归属只看文章真实 URL；
      · 两个域名都保留（url_domain / publisher_domain），便于事后审计；
      · publisher 与 url 域名不一致、而 feed 又不可信时，标记 identity_conflict。
    """
    records, seen = [], set()
    for raw in raw_records or []:
        if isinstance(raw, str):
            url, title, publisher, desc, feed_origin = raw, "", "", "", ""
        elif isinstance(raw, dict):
            url = raw.get("url") or ""
            title = raw.get("title") or ""
            publisher = raw.get("publisher") or ""
            desc = raw.get("desc") or raw.get("description") or ""
            feed_origin = raw.get("feed_origin") or raw.get("source") or ""
        else:
            continue
        canon = canonical_url(url)
        if not canon or canon in seen:
            continue
        seen.add(canon)

        url_domain = extract_domain(url)
        pub_domain = extract_domain(publisher) if publisher else ""
        publisher_trusted = bool(pub_domain) and feed_trusts_publisher(feed_origin)

        if publisher_trusted and pub_domain:
            # 可信 feed（Google News）：publisher 才是真实发布方，文章链接只是跳转
            domain = pub_domain
            identity_conflict = False
        else:
            # 不可信 feed：归属只能看文章真实 URL
            domain = url_domain or pub_domain
            identity_conflict = bool(pub_domain) and bool(url_domain) and pub_domain != url_domain

        source_class = classify_source(domain)
        # 领域与来源权威度是**两个正交维度**（V1.3 §F1 审计 E）：
        # source_class 回答「媒体是谁」，sport_category 回答「写的是哪个项目」。
        # 以前只有前者，于是 skysports.com 下的 F1 稿子照样拿到 TIER1。
        sport_verdict = sport_scope.classify_sport(title, desc, url)
        records.append({
            "url": url,
            "canonical": canon,
            "source_id": source_id_for(canon),
            "domain": domain,
            "url_domain": url_domain,
            "publisher_domain": pub_domain,
            "publisher_trusted": publisher_trusted,
            "identity_conflict": identity_conflict,
            "feed_origin": feed_origin,
            "source_class": source_class,
            "sport_category": sport_verdict.category,
            "sport_reason": sport_verdict.reason,
            "title": (title or "").strip()[:200],
            "title_key": title_fingerprint(title),
            "desc": (desc or "").strip()[:400],
            "tokens": _tokens(title),
            "desc_tokens": _tokens(desc),
            "primary_type": primary_event_type(title),
            "persons": person_names(title),
        })
    return records


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


def _rec_persons(record):
    p = record.get("persons")
    return p if p is not None else person_names(record.get("title"))


# 动作词：用来判断两条标题是否共享「同一个动作事实」
ACTION_WORDS = {
    "agree", "agrees", "agreed", "sign", "signs", "signed", "signing",
    "confirm", "confirms", "confirmed", "reject", "rejects", "rejected",
    "sack", "sacks", "sacked", "appoint", "appoints", "appointed",
    "ban", "bans", "banned", "suspend", "suspends", "suspended",
    "lose", "loses", "lost", "win", "wins", "won", "beat", "beats",
    "close", "closes", "closed", "complete", "completes", "completed",
    "open", "opens", "opened", "suffer", "suffers", "suffered",
    "rule", "rules", "ruled", "deny", "denies", "denied",
    "announce", "announces", "announced", "join", "joins", "joined",
    "leave", "leaves", "left", "return", "returns", "returned",
    "arrest", "arrested", "charge", "charged", "die", "dies", "died",
}


def shared_key_facts(anchor, other):
    """两条记录共享的「具体事实」：同一个人、同一个数字、同一个动作、同一个事件要素。

    V1.2 任务 3 要求：关键事实缺失时，不得仅靠高标题相似度授予官方确认。
    这里的返回值就是「关键事实是否对齐」的证据。
    """
    facts = set()
    for name in _rec_persons(anchor) & _rec_persons(other):
        facts.add(f"person:{name}")
    for num in _key_numbers(anchor.get("title")) & _key_numbers(other.get("title")):
        facts.add(f"num:{num}")

    shared_tokens = _rec_tokens(anchor) & _rec_tokens(other)
    for word in shared_tokens:
        if word in ACTION_WORDS:
            facts.add(f"action:{word}")

    # 共享的「事件要素词」也算关键事实：红牌 / 申诉 / 伤情 / 转会费 …
    # 只看动作词会漏掉「Dowman red card appeal」这类同义改写，导致真官方确认被误杀。
    type_words = set()
    for t in (_rec_type(anchor), _rec_type(other)):
        if t:
            type_words |= EVENT_KEYWORDS.get(t, set())
    for word in shared_tokens & type_words:
        facts.add(f"fact:{word}")
    return facts


def same_event(anchor, other):
    """判断两条记录是否在讲**同一件具体事件**。返回 (bool, 原因)。

    这是 V1.1 任务 1 / V1.2 任务 3 的核心：官方确认必须落在「同一件事」上，
    而不是「同一个俱乐部 + 同一个官方域名」。五个维度都要过：
      主体（俱乐部）· 人物（球员/教练）· 事件类型 · 关键数字 · 标题相关度
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

    # 人物主体：只在「人名就是事件核心」的类型上比对。
    # "Liverpool 签下 Olise" 与 "Liverpool 签下 Gordon" 是两笔转会，不是同一件事。
    na, nb = _rec_persons(anchor), _rec_persons(other)
    if na and nb and not (na & nb) and (pa in PERSON_CENTRIC_TYPES or pb in PERSON_CENTRIC_TYPES):
        return False, f"人物主体不同（{'/'.join(sorted(na))} vs {'/'.join(sorted(nb))}）"

    qa, qb = _key_numbers(anchor.get("title")), _key_numbers(other.get("title"))
    if qa and qb and not (qa & qb):
        return False, f"关键数字不一致（{'/'.join(sorted(qa))} vs {'/'.join(sorted(qb))}）"

    return True, "主体 / 事件类型 / 关键事实一致"


def official_confirmation_ok(anchor, other):
    """官方确认的**加严**判定：不仅要是同一事件，还必须能对齐一条具体事实。

    V1.2 任务 3：「关键事实缺失时，不得仅靠高标题相似度授予官方确认」。
    标题长得像不等于同一件事——必须共享至少一个人物 / 数字 / 动作事实。
    """
    ok, why = same_event(anchor, other)
    if not ok:
        return False, why
    if anchor is other:
        return True, "同一记录"
    facts = shared_key_facts(anchor, other)
    if not facts:
        return False, "标题相似但缺少可对齐的关键事实（人物/数字/动作），不予官方确认"
    return True, f"关键事实一致（{', '.join(sorted(facts)[:3])}）"


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

    # ---- V1.3 §F1 审计 R4：同一发布方的多篇稿子只算 1 个来源 ----
    # 以前只按「文章」聚类：Sky Sports 的 news 稿与 live-blog 讲同一件事，
    # 但两篇措辞不同、相似度不到转载阈值，于是成了两个「独立来源」，
    # 把 SINGLE_SOURCE 抬成 MULTIPLE_INDEPENDENT_SOURCES 并自动推送。
    # 实测全池 38 条多源判定里有 8 条（21%）属于这种「同一家媒体自证」。
    #
    # 「独立来源」必须是**独立的发布方**，不是**独立的文章**。
    # 这里只合并「单域名组」，不动转载组：bbc + dailymail + mirror 转载同一篇稿
    # 仍然是 1 个来源（已有逻辑），不会被拆开，也不会被重复计数。
    merged, single_domain_seen = [], {}
    for b in buckets:
        doms = {d for d in b["domains"] if d}
        if len(doms) == 1:
            dom = next(iter(doms))
            if dom in single_domain_seen:
                tgt = single_domain_seen[dom]
                tgt["domains"] |= b["domains"]
                tgt["trusted"] = tgt["trusted"] or b["trusted"]
                continue
            single_domain_seen[dom] = b
        merged.append(b)
    buckets = merged

    for b in buckets:
        # 只要组里有一个可识别的域名，这组就算「可信来源组」；
        # 全部 UNKNOWN 的组永远不能用于交叉印证。
        if b["key"] != "unknown" and any(classify_source(d) != "UNKNOWN" for d in b["domains"]):
            b["trusted"] = True

    return [
        {"key": b["key"], "domains": sorted(x for x in b["domains"] if x), "trusted": b["trusted"]}
        for b in buckets
    ]


# ==================== 程序侧 claim 重建（V1.3 P0-4） ====================
# 模型给的 source_claims 是**模型自己的说法**，不是证据。V1.3 §6 要求：
# 「模型自报冲突只能作线索，程序必须重新检查实际证据」。
# 所以冲突判定改成两步：先用记录本身重建 claim（唯一能直接判 CONFLICTING 的路径），
# 模型 claim 只保留两个用途——同源自相矛盾（证据链不可用）与线索（保守拒绝）。
_NEGATION_RE = re.compile(
    r"\b(?:deny|denies|denied|reject|rejects|rejected|snub|snubs|snubbed|"
    r"dismiss|dismisses|dismissed|refuse|refuses|refused|"
    r"unlikely|cancel|cancels|cancelled|canceled|"
    r"collapse|collapses|collapsed|block|blocks|blocked|"
    r"not|never|no|ends|ended)\b"
    r"|\b(?:rule|rules|ruled|ruling|pull|pulls|pulled)\s+out\b"
)

# 肯定表述：只有明确写了「签下 / 达成 / 确认 / 受伤」这类断言才算肯定。
# 缺了这道门，任何没有否定词的标题都会被当成肯定，制造大量假冲突。
_AFFIRM_RE = re.compile(
    r"\b(?:sign|signs|signed|signing|agree|agrees|agreed|deal|done|"
    r"confirm|confirms|confirmed|complete|completes|completed|"
    r"join|joins|joined|appoint|appoints|appointed|sack|sacks|sacked|"
    r"ban|bans|banned|suspend|suspend|suspended|"
    r"win|wins|won|beat|beats|injured|injury|suffer|suffers|suffered|"
    r"limp|limps|limped|bid|bids|offer|offers|target|targets|want|wants|"
    r"eye|eyes|set|close|closes|line|lining)\b"
)


def _stance_of(title):
    """从标题判断立场：True=肯定，False=否定，None=看不出来。

    看不出来就不参与冲突判定。宁可漏判一个真冲突，
    也不能凭「没写否定词」就认定它是肯定表述。
    """
    text = (title or "").lower()
    if not text:
        return None
    if _NEGATION_RE.search(text):
        return False
    if _AFFIRM_RE.search(text):
        return True
    return None


def rebuild_claims(records):
    """从真实记录重建结构化 claim（V1.3 P0-4）。

    只做一件很窄的事：同一俱乐部主体 + 同一事件类型的报道里，
    如果一条明确肯定、另一条明确否定，那就是程序能自己证明的冲突。
    """
    claims = []
    for r in records or []:
        title = r.get("title") or ""
        subjects = event_subjects(title)
        etype = _rec_type(r)
        if not subjects or not etype:
            continue
        stance = _stance_of(title)
        if stance is None:
            continue
        claims.append({
            "claim_key": f"{'|'.join(sorted(subjects))}#{etype}",
            "stance": "supports" if stance else "denies",
            "source": r.get("domain") or "",
            "url": r.get("url") or "",
        })
    return claims


def detect_conflict(source_claims, model_conflict_flag=False, records=None):
    """判定来源间是否互相矛盾。返回 (是否冲突, 原因, 冲突来源)。

    冲突来源有四种取值：
      · "program" —— 程序从记录本身重建出来的冲突：同一主体 + 同一事件类型下，
        一条明确肯定、另一条明确否定（来自不同 URL）。这是**硬冲突**。
      · "claims"  —— 模型给出的 claim 里，**同一个来源**对同一事实同时说 supports 和
        denies。不管模型是否可信，这份证据链本身已经不可用，因此也按硬冲突处理。
      · "model"   —— 模型自报冲突，或跨来源的模型 claim 而程序无法确证。
        只是待核验线索，按「保守拒绝自动推送」处理，不判 CONFLICTING。
      · ""        —— 未发现冲突。
    """
    # ---- 第一步：程序侧重建（唯一能直接判 CONFLICTING 的路径） ----
    grouped = {}
    for claim in rebuild_claims(records):
        grouped.setdefault(claim["claim_key"], []).append(claim)
    for key, items in grouped.items():
        stances = {c["stance"] for c in items}
        urls = {c["url"] for c in items}
        # 同一篇报道说了两遍不算冲突，必须是两篇不同的稿子在打架
        if len(stances) == 2 and len(urls) >= 2:
            return (True,
                    f"程序核验：{key} 同时出现肯定与否定报道（{len(urls)} 篇）",
                    "program")

    # ---- 第二步：模型 claim 只用于发现「同源自相矛盾」 ----
    by_key = {}
    for claim in source_claims or []:
        if not isinstance(claim, dict):
            continue
        key = (claim.get("claim_key") or claim.get("claim") or "").strip().lower()
        stance = (claim.get("stance") or "").strip().lower()
        if not key or stance not in ("supports", "denies"):
            continue
        src = (claim.get("source") or "").strip().lower()
        by_key.setdefault(key, {}).setdefault(src, set()).add(stance)

    for key, per_source in by_key.items():
        for src, stances in per_source.items():
            if len(stances) == 2:
                who = src or "未署名来源"
                return (True,
                        f"同一来源对同一事实给出相反表述：{key}（{who}）",
                        "claims")

    # ---- 第三步：模型自报 / 跨来源 claim 只作线索 ----
    if model_conflict_flag:
        return True, "模型报告来源说法冲突，程序无法确证", "model"
    cross_source = [k for k, per_source in by_key.items()
                    if len({s for st in per_source.values() for s in st}) == 2]
    if cross_source:
        return (True,
                f"模型称不同来源对同一事实说法相反：{cross_source[0]}，程序无法确证",
                "model")
    return False, "", ""


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
        and not r.get("identity_conflict")      # 身份存疑的记录不得充当官方确认
        and _report_similarity(anchor, r) >= RELATED_THRESHOLD
    ]
    confirmed_official, rejected_official = [], []
    for r in candidate_official:
        # 官方确认走加严判定：必须是同一事件，且能对齐至少一条具体事实
        ok_same, _why = official_confirmation_ok(anchor, r)
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
        {
            "source_id": r.get("source_id") or source_id_for(r["canonical"]),
            "domain": r["domain"],
            "url_domain": r.get("url_domain") or "",
            "publisher_domain": r.get("publisher_domain") or "",
            "source_class": r["source_class"],
            "url": r["url"],
        }
        for r in pool
    ]
    rejected_domains = sorted({r["domain"] for r in rejected_official})

    def groups_for_count():
        return trusted if trusted else groups_all

    # ---- V1.2 任务 4 / V1.3 P0-4：冲突不看来源数量 ----
    # 程序先用记录本身重建 claim；只有程序核验出的冲突（program）与同源自相矛盾（claims）
    # 才直接判 CONFLICTING。模型自报的冲突只是线索，走保守拒绝。
    conflict, conflict_reason, conflict_kind = detect_conflict(
        source_claims, model_conflict_flag, records=pool)
    if conflict_kind in ("program", "claims"):
        return result(CONFLICTING, conflict_reason or "来源之间说法冲突",
                      True, groups_for_count(), sources, rejected_domains)
    if conflict_kind == "model":
        # 模型自报冲突只是线索，程序无法确证 → 保守拒绝自动推送
        return result(UNVERIFIED,
                      f"{conflict_reason}，无法由程序确证，保守拒绝自动推送",
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
def max_allowed_rating_key(status, source_classes, sport_category=None):
    """返回程序允许的最高评级 key（只降不升）。

    - 非足球领域 → 最多 Tier 3（V1.3 §F1 审计 E）
      领域不对的内容不存在「多权威」的问题，它根本不该出现在足球频道里。
      这一条是兜底：正常情况下 sport_gate 已经把它拦在推送之前。
    - 未核验 / 冲突 → 最多 Tier 3
    - 单一来源（低可信 / 未识别 / 无来源）→ 最多 Tier 3
    - 单一来源（一线权威）→ 最多 Tier 2
    - 多源 / 官方 → 不限制（由模型按素材判断）

    注意「一线权威」只影响**上限**，不直接授予等级：
    `skysports.com` 是 TIER1 域名，但只有一条 Sky 稿子时仍被压到 Tier 2，
    拿不到 Tier 1。媒体知名度不等于事实核验等级。
    """
    if sport_category is not None and sport_category != sport_scope.FOOTBALL:
        return "Tier 3"
    if status in (UNVERIFIED, CONFLICTING):
        return "Tier 3"
    if status == SINGLE_SOURCE:
        classes = list(source_classes or [])
        if not classes or "LOW_TRUST" in classes or "UNKNOWN" in classes:
            return "Tier 3"
        return "Tier 2"
    return None


def enforce_rating(rating_key, status, source_classes, sport_category=None):
    """把模型给的评级压到程序允许的上限，返回 (最终 key, 是否被降级, 原因)。

    黑标是「严肃红线」的内容安全标记，不是可信度分级，任何情况下都不降级，
    否则死亡/违法类新闻会被降成吃瓜黄牌，卡片模板也会切错。
    黑标的**证据门槛**由 black_label_gate() 单独负责，两者不要混在一起。
    """
    if rating_key == "黑标":
        return rating_key, False, ""
    limit = max_allowed_rating_key(status, source_classes, sport_category)
    order = {"Tier 1": 1, "Tier 2": 2, "Tier 3": 3, "黑标": 0}
    if not limit or not rating_key:
        return rating_key, False, ""
    if order.get(rating_key, 9) < order.get(limit, 9):
        return limit, True, f"证据等级为 {status_label(status)}，评级由 {rating_key} 降为 {limit}"
    return rating_key, False, ""
