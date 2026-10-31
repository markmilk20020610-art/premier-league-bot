import os
import sys
import json
import html
import re
import time
import hashlib
import datetime
import email.utils
import requests
import xml.etree.ElementTree as ET
import concurrent.futures
from urllib.parse import urlparse, urlunparse, quote, parse_qs, urlencode

import ai_provider as ai
import evidence as ev
import redact
import schema as out_schema
import state_store as store

# ==================== 配置与初始化 ====================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
# 允许指向自建反代/本地 mock，默认官方地址
TELEGRAM_API_BASE = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")
RUN_MODE = os.environ.get("RUN_MODE", "scan") # scan 或 morning

# 手动强制补发早报：绕过 last_morning_report_date 日期锁。
# 三种触发方式（任一即可）：环境变量 FORCE_SUMMARY=true / 命令行 --force / 命令行 -f
FORCE_SUMMARY = (
    os.environ.get("FORCE_SUMMARY", "").strip().lower() in ("1", "true", "yes", "y", "on")
    or "--force" in sys.argv
    or "-f" in sys.argv
)

# 早报的「今天」必须按北京时间算。原实现用 datetime.utcnow().strftime()，
# 而 UTC 22:00 正好是北京次日 06:00 —— 用 UTC 日期做锁会让「今天」和用户认知差一天，
# 一旦和 cron-job.org 的手动触发混用，就会出现「今天已发过」的误判。
CN_TZ = datetime.timezone(datetime.timedelta(hours=8))

# 早报素材窗口与兜底阈值
# V1.2 任务 7：早报的「事实新闻」只允许过去 24 小时，且**不得放宽**。
# 原实现会在素材不足时把窗口放宽到 48h / 72h，结果提示词说「只用 24 小时」、
# 程序却把三天前的旧闻混进来，模型照着写就成了「昨天的新闻」其实是前天的。
SUMMARY_WINDOW_HOURS = 24      # 硬窗口：早报事实新闻的唯一时间范围
SUMMARY_MIN_MATERIALS = 5      # 低于这个条数就打「低情报量」标记，不再放宽时间
SUMMARY_MAX_MATERIALS = 40     # 喂给模型的素材上限，避免 token 爆炸
# 发布时间在未来的条目：允许几分钟的时钟偏差，超过就判为不可信时间
FUTURE_SKEW_TOLERANCE_MIN = 5
POOL_RETENTION_HOURS = 72      # state.json 里新闻池的保留时长（池子可以留旧闻，早报不许用）
POOL_MAX_ITEMS = 150           # 新闻池条目上限，控制 state.json 体积

# 可信单一来源白名单（V1.1 任务 4）。
# SINGLE_SOURCE 默认不自动推送；只有这里列出的域名（经人工确认的记者/机构）才放行，
# 且推送时必须在卡片上标注「未经独立交叉验证」并给出原始来源名与 URL。
# 注意：UNKNOWN 域名即使写进这里也不会生效——evidence.single_source_allowed() 会拒绝。
TRUSTED_SINGLE_SOURCES = {
    d.strip().lower()
    for d in os.environ.get("TRUSTED_SINGLE_SOURCES", "").split(",")
    if d.strip()
}

STATE_FILE = "state.json"
LOG_FILE = "run_telemetry.log"

# 这些 AI 状态属于「自愈型」：已经用 warning 注解报过，下一轮会自动重试。
# 把它们也判成 Job 失败，只会让真正需要人干预的故障被红叉淹没（V1.2 任务 17）。
SELF_HEALING_AI_STATUSES = {ai.QUOTA_EXHAUSTED, ai.RATE_LIMITED, ai.CIRCUIT_OPEN}

# ==================== 时效策略 ====================
# 实测（2026-10）：走 Google News 降级的源，pubDate 中位年龄高达 40 小时，
# 一半以上超过 24 小时。AI 只按「炸不炸」挑，就会把 20 小时前的旧闻当突发推出去。
# 因此改为「按发布时间硬过滤 + 按时间倒序喂给模型 + Prompt 层二次约束」三重保障。
#
# V1.2 任务 12：突发新闻是 12 小时硬时效，不再有「退回全量」这条后门。
# 旧实现是 8h → 不够就 24h → 还不够就把**整个抓取结果**（可能三天前的）喂给模型，
# 靠 Prompt 里一句「只要突发」来兜底。模型没有时间概念，它只看到一段文字，
# 于是安静时段里最容易被推出去的反而是陈年旧闻——越是没新闻，越容易翻车。
FRESHNESS_HOURS = 8              # 优先窗口：优先喂 8 小时内的素材
FRESHNESS_HARD_CAP_HOURS = 12    # 突发新闻硬上限，任何情况下都不得突破
MIN_FRESH_CANDIDATES = 12        # 低于这个数量就把窗口从 8h 放宽到 12h（到此为止）

# 发布时间未知的条目是否放行。默认**不放行**：无法证明「刚发生」就不能算突发。
# 留成常量而不是硬编码，是为了万一某个重要源长期不给 pubDate 时能一行开关改回，
# 而不是又要改逻辑——但放行前请先看日志里「发布时间未知排除 N 条」这个数字。
FRESHNESS_ALLOW_UNKNOWN = False

# 并发抓取路数。19 个源串行要 40~50 秒，Actions 按分钟计费，串行纯属浪费。
# 不设更高并发，是为了避免同时打 11 个 news.google.com 降级查询被限流。
FETCH_CONCURRENCY = 6

# ==================== 极速与权威 RSS 监控矩阵 (全明星情报网) ====================
RSS_FEEDS = [
    # 1. 官方与裁判仲裁层 (Tier 1 金哨)
    "https://www.thefa.com/rss",                       # 英足总（无原生 RSS，走站点检索降级）
    "https://www.premierleague.com/rss/news",          # 英超官方（端点已下线，走站点检索降级）

    # 2. 顶级权威主流媒体 (Tier 1 权威主流)
    "https://feeds.bbci.co.uk/sport/football/rss.xml",
    "https://www.skysports.com/rss/12040",
    "https://theathletic.com/feed/",                   # 403 Cloudflare 风控，无可用降级

    # 3. 爆料、八卦与财务舆论场 (Tier 3 吃瓜黄牌)
    # 注意：dailymail.co.uk 已 301 跳转到 dailymail.com，直连新域名可省掉一次重定向
    "https://www.dailymail.com/sport/football/index.rss",
    "https://www.thesun.co.uk/sport/football/feed/",   # 注意是 .co.uk；thesun.com 会 SSL 证书错误

    # 4. 顶级名记与转会/跟队信源 (RSSHub 桥接)
    "https://rsshub.app/twitter/user/David_Ornstein",
    "https://rsshub.app/twitter/user/FabrizioRomano",
    "https://rsshub.app/twitter/user/Matt_Law_DT",
    "https://rsshub.app/twitter/user/SamLee",

    # 5. Big 6 官方及核心跟队直连
    "https://www.arsenal.com/rss/news",                # 503，走 site:arsenal.com 降级
    "https://rsshub.app/twitter/user/CharlesWatts",    # 阿森纳跟队名记（The Athletic）
    "https://rsshub.app/twitter/user/ManUtd",
    "https://rsshub.app/twitter/user/ManCity",
    "https://rsshub.app/twitter/user/Arsenal",
    "https://rsshub.app/twitter/user/LFC",
    "https://rsshub.app/twitter/user/ChelseaFC",
    "https://rsshub.app/twitter/user/SpursOfficial",
]

# ==================== 失效源自动降级映射 ====================
# 实测结论（2026-10，全部真实请求验证过）：
#   - rsshub.app 公共实例声明"仅供测试、不用于生产"，Twitter 路由已完全停用，统一返回 404
#   - thefa.com 所有 /rss 路径均返回 200 + text/html（并非 feed），解析必然失败
#   - premierleague.com/rss/news 已下线，返回 404
#   - theathletic.com/feed/ 返回 403（Cloudflare 风控），Google News 亦无索引，暂无降级方案
#   - thesun.com 是错误域名（SSL 证书不匹配），正确为 thesun.co.uk
# 因此主源不可用（非 200 或解析抛异常）时自动切换到 Google News RSS，保证矩阵不空转、不静默丢源。
FEED_FALLBACKS = {
    # 官方与裁判仲裁层
    # thefa.com 无原生 RSS，且 site:thefa.com 会被业余联赛页面（Cheshire Girls Football League
    # 之类）淹没。改用「The FA + statement/disciplinary」检索，实测能直接命中
    # "Arsenal lose appeal against Dowman red card" 这类纪律与申诉事件。
    "https://www.thefa.com/rss":
        "https://news.google.com/rss/search?q=%22The+FA%22+statement+OR+disciplinary+when:1d&hl=en-US&gl=US&ceid=US:en",
    # 加 /news 路径限定，避免被 FPL 选秀类内容占满；实测能抓到 Key Match Incidents Panel 裁判公告
    "https://www.premierleague.com/rss/news":
        "https://news.google.com/rss/search?q=site:premierleague.com+%22Premier+League%22+when:1d&hl=en-US&gl=US&ceid=US:en",
    # 每日邮报：主源若因重定向/TLS 抖动失败，用报道检索兜底
    "https://www.dailymail.com/sport/football/index.rss":
        "https://news.google.com/rss/search?q=%22Daily+Mail%22+football+transfer+when:1d&hl=en-US&gl=US&ceid=US:en",

    # 名记信源
    "https://rsshub.app/twitter/user/David_Ornstein":
        "https://news.google.com/rss/search?q=%22David+Ornstein%22+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://rsshub.app/twitter/user/FabrizioRomano":
        "https://news.google.com/rss/search?q=%22Fabrizio+Romano%22+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://rsshub.app/twitter/user/Matt_Law_DT":
        "https://news.google.com/rss/search?q=%22Matt+Law%22+Chelsea+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://rsshub.app/twitter/user/SamLee":
        "https://news.google.com/rss/search?q=%22Sam+Lee%22+Manchester+City+when:1d&hl=en-US&gl=US&ceid=US:en",

    # 阿森纳官方站：/rss/news 实测 503（Cloudflare 拦截），改用站点检索
    "https://www.arsenal.com/rss/news":
        "https://news.google.com/rss/search?q=site:arsenal.com+when:1d&hl=en-US&gl=US&ceid=US:en",
    # 阿森纳跟队名记 Charles Watts
    "https://rsshub.app/twitter/user/CharlesWatts":
        "https://news.google.com/rss/search?q=%22Charles+Watts%22+Arsenal+when:1d&hl=en-US&gl=US&ceid=US:en",

    # Big 6 官方站：site: 限定后全是官网一手内容（team news / 伤情更新 / 官方声明）
    "https://rsshub.app/twitter/user/ManUtd":
        "https://news.google.com/rss/search?q=site:manutd.com+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://rsshub.app/twitter/user/ManCity":
        "https://news.google.com/rss/search?q=site:mancity.com+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://rsshub.app/twitter/user/Arsenal":
        "https://news.google.com/rss/search?q=site:arsenal.com+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://rsshub.app/twitter/user/LFC":
        "https://news.google.com/rss/search?q=site:liverpoolfc.com+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://rsshub.app/twitter/user/ChelseaFC":
        "https://news.google.com/rss/search?q=site:chelseafc.com+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://rsshub.app/twitter/user/SpursOfficial":
        "https://news.google.com/rss/search?q=site:tottenhamhotspur.com+when:1d&hl=en-US&gl=US&ceid=US:en",
}

# 模型客户端不在这里初始化。所有模型调用统一走 ai_provider.AIRouter，
# 主程序不再直接持有 genai.Client，也不允许直接发 HTTP 调模型。
AI_CFG = ai.AIConfig()
ROUTER = None       # 由 main() 按 state 创建


def get_router(state):
    """取（或惰性创建）全局 router。测试可直接替换 bot.ROUTER。"""
    global ROUTER
    if ROUTER is None:
        ROUTER = ai.AIRouter(state, config=AI_CFG)
    else:
        ROUTER.state = state
    return ROUTER


# ==================== 状态持久化管理 ====================
def load_state():
    """读取状态；文件损坏时抛 StateError，不再静默退回空状态（否则去重记录会全丢）。"""
    return store.load_state(STATE_FILE)


def save_state(state):
    """原子写入 + 回读校验；失败抛 StateError。

    调用方必须显式处理异常：写盘失败绝不能继续当成「已保存」。
    """
    store.save_state_atomic(state, STATE_FILE)

def _now_utc():
    return datetime.datetime.now(datetime.timezone.utc)

def _now_iso():
    return _now_utc().isoformat()

def _cn_date_str():
    """北京时间的日期字符串，用于早报日期锁。"""
    return datetime.datetime.now(CN_TZ).strftime("%Y-%m-%d")

def _skip(code, detail, extra=None):
    """统一的「跳过」出口。

    历史教训：早报没发出来时，Actions 依然是绿勾，因为程序走了静默 return。
    现在所有跳过路径都必须打 [SKIP] + 一条 GitHub 注解，保证在 Actions 页面
    顶部能看到黄色告警，而不是只剩一个「成功」让人干瞪眼。
    """
    msg = f"[SKIP] {code} | {detail}"
    print(msg)
    print(f"::warning title=Skip: {code}::{detail}")
    payload = {"status": "SKIPPED", "code": code, "detail": detail}
    if extra:
        payload.update(extra)
    log_telemetry(payload)

def log_telemetry(data):
    # 脱敏后再落盘：遥测里会带 last_error，而 requests 的异常原文含 Bot Token
    entry = f"[{datetime.datetime.utcnow().isoformat()}] {redact.sanitize(json.dumps(data, ensure_ascii=False))}\n"
    print(entry.strip())
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(entry)
    except Exception:
        pass

# ==================== Telegram 推送（HTML 稳健模式） ====================
_HTML_TAG_RE = re.compile(r"</?([a-zA-Z][a-zA-Z0-9]*)[^>]*>")


def _open_tags(text):
    """返回 text 末尾仍未闭合的标签栈，用于跨块时补闭合/补重开。"""
    stack = []
    for m in _HTML_TAG_RE.finditer(text):
        name = m.group(1).lower()
        if m.group(0).startswith("</"):
            if stack and stack[-1] == name:
                stack.pop()
        else:
            stack.append(name)
    return stack


def _split_html(text, limit=4000):
    """按行聚合切分长消息；既不切断标签本身，也保证每块的标签自闭合（否则必 400）。"""
    chunks, buf = [], ""
    for line in text.split("\n"):
        candidate = f"{buf}\n{line}" if buf else line
        if len(candidate) <= limit:
            buf = candidate
            continue
        if buf:
            chunks.append(buf)
            buf = ""
        while len(line) > limit:
            cut = limit
            lt = line.rfind("<", 0, limit)
            # 若 limit 之前有未闭合的 "<"，把切点回退到该 "<" 之前
            if lt != -1 and ">" not in line[lt:limit]:
                cut = lt
            if cut <= 0:
                cut = limit
            piece, rest = line[:cut], line[cut:]
            # 若切点落在 <code>...</code> 内部，补闭合到本块、补重开到下一块
            opened = _open_tags(piece)
            if opened:
                piece += "".join(f"</{t}>" for t in reversed(opened))
                rest = "".join(f"<{t}>" for t in opened) + rest
            chunks.append(piece)
            line = rest
        buf = line
    if buf:
        chunks.append(buf)
    return chunks


TELEGRAM_RETRY_DELAYS = (2, 4, 8)  # 3 次重试的等待间隔（秒）

# 单次请求的三种结局（V1.2 任务 9）。
# 旧代码只有一个 retryable 布尔值，于是「读超时」和「429」被当成同一类：
# 读超时说明请求**已经发出**、只是没等到回执，消息可能已经进群；
# 当成可重试失败直接重发 → 用户收到两条一样的推送。
TG_OK = "OK"                      # 确认送达（HTTP 200 且 {"ok": true}）
TG_DETERMINISTIC = "DETERMINISTIC"  # 确定没送达，且重试也不会成功（400/401/403）
TG_RETRYABLE = "RETRYABLE"        # 确定没送达，值得再试（429/5xx/连接失败）
TG_UNCERTAIN = "UNCERTAIN"        # 请求已发出，结果未知（读超时/连接中断）

MISSING_CREDENTIALS = "缺少 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID"


def _exc_types(*names):
    """安全地取 requests.exceptions 里的异常类。

    测试环境用桩替换了 requests，未必有 ReadTimeout/ConnectTimeout；
    直接写 `except requests.exceptions.ReadTimeout` 会在桩环境里抛 AttributeError。
    """
    out = []
    for name in names:
        cls = getattr(requests.exceptions, name, None)
        if isinstance(cls, type):
            out.append(cls)
    return tuple(out)


_CONNECT_TIMEOUT_EXC = _exc_types("ConnectTimeout")   # 连接都没建立 → 确定没送达
_READ_TIMEOUT_EXC = _exc_types("ReadTimeout")         # 已发出、等响应超时 → 结果未知
_TIMEOUT_EXC = _exc_types("Timeout")
_REQUEST_EXC = _exc_types("RequestException", "ConnectionError", "SSLError")


def _classify_exception(exc):
    """把网络异常归类成「确定没送达」还是「结果未知」。"""
    if _CONNECT_TIMEOUT_EXC and isinstance(exc, _CONNECT_TIMEOUT_EXC):
        return TG_RETRYABLE
    if _READ_TIMEOUT_EXC and isinstance(exc, _READ_TIMEOUT_EXC):
        return TG_UNCERTAIN
    # 兜底：Timeout 家族里分辨不出子类的，一律按「结果未知」处理（宁可少发不可重发）
    if _TIMEOUT_EXC and isinstance(exc, _TIMEOUT_EXC):
        return TG_UNCERTAIN
    return TG_RETRYABLE


def _post_telegram_once(url, payload):
    """单次推送，返回 (是否成功, 结局类型, 原因描述, message_id)。

    只认 HTTP 200 不够：Telegram 在部分场景会回 200 + {"ok": false}，
    旧代码会把这种失败当成功，然后 state 里记下「已送达」。

    原因描述一律经 redact.sanitize()：requests 的异常原文里带着完整 Bot API URL，
    也就是 Bot Token。这行字会进 stdout、GitHub Actions 注解、run_telemetry.log
    和 state.json 的 last_error，等于把机器人交出去。
    """
    try:
        resp = requests.post(url, json=payload, timeout=10)
    except Exception as e:
        kind = _classify_exception(e)
        reason = redact.sanitize(f"请求异常 {type(e).__name__}: {e}")
        return False, kind, reason, None
    body = redact.sanitize((resp.text or "")[:300].replace("\n", " ").replace("\r", " "))
    try:
        body_json = resp.json()
    except Exception:
        body_json = None
    if resp.status_code == 200 and isinstance(body_json, dict) and body_json.get("ok") is True:
        mid = (body_json.get("result") or {}).get("message_id")
        return True, TG_OK, "OK", mid
    if resp.status_code in (401, 403):
        # 凭证类错误重试一万次也没用，直接红色告警
        print(f"::error title=Telegram Auth Failed::HTTP {resp.status_code}，Bot Token 或 CHAT_ID 无效，请检查仓库 Secrets")
    # 429 限流与 5xx 值得重试；400/403 这类确定性错误重试也不会成功，白等 14 秒
    if resp.status_code == 429 or resp.status_code >= 500:
        return False, TG_RETRYABLE, f"HTTP {resp.status_code}: {body}", None
    return False, TG_DETERMINISTIC, f"HTTP {resp.status_code}: {body}", None


def _chunk_hash(text):
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]


def send_telegram_ex(text, chunk_records=None, report=None):
    """推送并返回 (是否全部成功, [message_id...], 最后一条错误描述)。

    V1.2 任务 11：分段独立记账。
      · chunk_records —— 上一次留下的分段记录。已确认送达的段（且内容哈希一致）
        直接跳过，绝不重发；只有「一段成功」才允许接着发下一段。
      · report —— 出参，本次每一段的完整结果（chunk_index / chunk_total /
        delivery_status / telegram_message_id / attempts / last_error），
        由调用方写进 state.json。传 None 时不回填。

    返回的布尔值只代表「全部段都送达」。部分成功返回 False——绝不冒充成功。
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram 凭证缺失，跳过推送。")
        return False, [], MISSING_CREDENTIALS
    url = f"{TELEGRAM_API_BASE}/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = _split_html(text, 4000)

    # 断点续传的前提是「分段布局没变」：段数或某段内容变了，旧记录就不能复用。
    prior = {}
    if chunk_records:
        if len(chunk_records) == len(chunks):
            prior = {int(r.get("chunk_index") or 0): r for r in chunk_records if isinstance(r, dict)}
        else:
            print(f"::warning title=Chunk Layout Changed::历史分段 {len(chunk_records)} 段"
                  f"与本次 {len(chunks)} 段不一致，放弃断点续传，按新内容整条发送")

    success = True
    message_ids = []
    last_reason = ""
    records = []

    for idx, chunk in enumerate(chunks, 1):
        rec = {
            "chunk_index": idx,
            "chunk_total": len(chunks),
            "chunk_hash": _chunk_hash(chunk),
            "delivery_status": "",
            "telegram_message_id": None,
            "attempts": 0,
            "last_error": "",
        }
        old = prior.get(idx) or {}
        if (old.get("delivery_status") == store.CHUNK_DELIVERED
                and old.get("telegram_message_id")
                and old.get("chunk_hash") == rec["chunk_hash"]):
            # 这一段上次已经确认送达，且内容没变 → 绝不重发
            rec["delivery_status"] = store.CHUNK_DELIVERED
            rec["telegram_message_id"] = old.get("telegram_message_id")
            rec["attempts"] = int(old.get("attempts") or 0)
            records.append(rec)
            message_ids.append(rec["telegram_message_id"])
            print(f"[INFO] 分块 {idx}/{len(chunks)} 上次已送达"
                  f"（message_id={rec['telegram_message_id']}），本次跳过不重发")
            continue

        rec["attempts"] = int(old.get("attempts") or 0)
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",  # 切为 HTML 模式，彻底远离 Markdown 语法崩溃
            "disable_web_page_preview": False
        }
        saw_uncertain = False
        last_kind = TG_RETRYABLE
        # 1 次首发 + 3 次重试，间隔 2s / 4s / 8s
        for attempt in range(len(TELEGRAM_RETRY_DELAYS) + 1):
            ok, kind, reason, mid = _post_telegram_once(url, payload)
            rec["attempts"] += 1
            if ok:
                rec["delivery_status"] = store.CHUNK_DELIVERED
                rec["telegram_message_id"] = mid
                rec["last_error"] = ""
                if mid is not None:
                    message_ids.append(mid)
                break
            last_reason = reason
            last_kind = kind
            rec["last_error"] = reason
            if kind == TG_UNCERTAIN:
                # 一旦出现过「可能已送达」，后面即使报 400 也不能断定没送达
                saw_uncertain = True
            print(f"[ERROR] Telegram 分块 {idx}/{len(chunks)} "
                  f"第 {rec['attempts']} 次发送失败：{reason}")
            if kind == TG_DETERMINISTIC:
                print("[WARN] 确定性失败（如 HTML 格式错误/被拉黑），重试无意义，放弃本块。")
                break
            if attempt < len(TELEGRAM_RETRY_DELAYS):
                wait = TELEGRAM_RETRY_DELAYS[attempt]
                print(f"[INFO] {wait} 秒后重试（{attempt + 1}/{len(TELEGRAM_RETRY_DELAYS)}）...")
                time.sleep(wait)

        if rec["delivery_status"] != store.CHUNK_DELIVERED:
            if saw_uncertain:
                rec["delivery_status"] = store.CHUNK_UNCERTAIN
            elif last_kind == TG_DETERMINISTIC:
                rec["delivery_status"] = store.CHUNK_FAILED
            else:
                rec["delivery_status"] = store.CHUNK_RETRYABLE
            records.append(rec)
            # GitHub Actions 注解语法：会在 Actions 页面顶部以红色高亮显示，便于一眼定位
            print(f"::error title=Telegram Push Failed::分块 {idx}/{len(chunks)} 推送失败"
                  f"（共尝试 {rec['attempts']} 次）| {rec['last_error']}")
            success = False
            # 前一段没发出去就不要继续发后面的，避免半截消息更难恢复；
            # 但剩余段必须留下 NOT_ATTEMPTED 记录，否则汇总里会看不出「还有几段没发」。
            for rest in range(idx + 1, len(chunks) + 1):
                records.append({
                    "chunk_index": rest,
                    "chunk_total": len(chunks),
                    "chunk_hash": _chunk_hash(chunks[rest - 1]),
                    "delivery_status": store.CHUNK_NOT_ATTEMPTED,
                    "telegram_message_id": None,
                    "attempts": 0,
                    "last_error": "",
                })
            break
        records.append(rec)

    delivered = [r for r in records if r["delivery_status"] == store.CHUNK_DELIVERED]
    if delivered and not success:
        sent_idx = ",".join(str(r["chunk_index"]) for r in delivered)
        print(f"::warning title=Partial Delivery::共 {len(chunks)} 段，"
              f"已送达 {len(delivered)} 段（第 {sent_idx} 段，"
              f"message_id={[r['telegram_message_id'] for r in delivered]}），其余未送达")

    if report is not None:
        report.extend(records)
    return success, message_ids, last_reason


def send_telegram(text):
    """兼容旧调用：只关心成功与否。"""
    ok, _ids, _err = send_telegram_ex(text)
    return ok

# ==================== RSS 抓取（带严格超时与安全解析） ====================
_TAG_STRIP_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_ATOM_NS = "{http://www.w3.org/2005/Atom}"
_DC_NS = "{http://purl.org/dc/elements/1.1/}"


def _parse_pub_date(text):
    """把 RSS/Atom 的日期字符串解析成 UTC datetime，失败返回 None。

    兼容三种常见格式：
      · RSS 2.0  <pubDate>Wed, 30 Oct 2026 12:00:00 GMT</pubDate>
      · Atom     <published>2026-10-30T12:00:00Z</published>
      · Dublin   <dc:date>2026-10-30T12:00:00+00:00</dc:date>
    """
    if not text:
        return None
    raw = text.strip()
    try:
        return email.utils.parsedate_to_datetime(raw).astimezone(datetime.timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            d = datetime.datetime.strptime(raw, fmt)
            if d.tzinfo is None:
                d = d.replace(tzinfo=datetime.timezone.utc)
            return d.astimezone(datetime.timezone.utc)
        except Exception:
            continue
    return None


def _age_hours(published):
    """返回发布至今的小时数；无日期返回 None。"""
    if published is None:
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now - published).total_seconds() / 3600.0


def _age_label(published):
    """生成给模型看的时效标注，如「2小时前」。"""
    h = _age_hours(published)
    if h is None:
        return "时间未知"
    if h < 1:
        return f"{int(h * 60)}分钟前"
    return f"{h:.0f}小时前"


def _clean_text(text, limit=160):
    """清洗 RSS 文本：剥 HTML 标签、反转义实体、压缩空白、按需截断。

    部分源（每日邮报 / 太阳报）的 description 是整段 CDATA HTML，单条可达上千字符。
    17 个源 × 10 条不做清洗时 raw_blob 会飙到 96K 字符（≈32K token），
    既烧钱又容易撞免费层 TPM 限制，还会让模型忽略后半段素材。
    """
    if not text:
        return ""
    s = _TAG_STRIP_RE.sub(" ", text)
    s = html.unescape(s)
    s = _WS_RE.sub(" ", s).strip()
    return s[:limit]


def _parse_feed_items(content, source_url, limit=10, feed_origin=None):
    """安全解析 RSS 内容，返回前 limit 条条目。节点缺失或文本为空时均使用兜底值。

    limit 原为 3，实测会导致「素材池固化」：每个源长期只有前 3 条在轮转，
    AI 反复挑中同一条 → evidence_url 命中 processed_urls → 被去重拦截 → 长时间静默。
    放宽到 10 条后候选池从 ~27 条扩到 ~80 条，去重后仍能留下新鲜「次新」新闻。

    `source_url` 是**逻辑来源**（RSS_FEEDS 里的那一项，用于 telemetry 与去重）；
    `feed_origin` 是**实际抓取的 feed 地址**。降级抓取时两者不同：
    比如 thefa.com/rss 走 Google News 降级，逻辑来源还是 thefa.com，
    但内容其实来自 news.google.com —— 只有后者才有资格决定 publisher 是否可信。
    """
    items = []
    root = ET.fromstring(content)
    for item in root.findall(".//item")[:limit]:
        title_elem = item.find("title")
        title = _clean_text(title_elem.text, 140) if title_elem is not None and title_elem.text else "无标题"

        link_elem = item.find("link")
        if link_elem is not None:
            # RSS 2.0 的链接在文本节点里，Atom 的链接在 href 属性上，两者都要兼容
            link = (link_elem.text or "").strip() or link_elem.get("href") or source_url
        else:
            link = source_url

        desc_elem = item.find("description")
        desc = _clean_text(desc_elem.text, 160) if desc_elem is not None and desc_elem.text else ""

        # 依次尝试 RSS / Atom / Dublin Core 三种日期节点，取到即用
        date_elem = item.find("pubDate")
        if date_elem is None:
            date_elem = item.find(_ATOM_NS + "published")
        if date_elem is None:
            date_elem = item.find(_ATOM_NS + "updated")
        if date_elem is None:
            date_elem = item.find(_DC_NS + "date")
        published = _parse_pub_date(date_elem.text if date_elem is not None else None)

        # Google News 的条目带 <source url="https://www.bbc.co.uk">BBC Sport</source>，
        # 这是还原「真实发布方」的唯一线索。没有它，降级源抓来的稿子会被一律判成
        # 聚合器链接、永远无法作为证据，等于废掉整个降级矩阵。
        source_elem = item.find("source")
        publisher = (source_elem.get("url") or "").strip() if source_elem is not None else ""

        items.append({
            "title": title,
            "link": link,
            "desc": desc,
            "source": source_url,
            # 实际抓取地址：V1.2 任务 1 的可信边界就是靠它判定的
            "feed_origin": feed_origin or source_url,
            "publisher": publisher,
            "published": published,
        })
    return items


# ==================== 跨源去重（同一稿件被多源转述时只保留首次出现） ====================
# V1.2 任务 13：只清**跟踪参数**，不清业务参数。
#
# 旧实现把整个 query 抹掉，只留 scheme+netloc+path。对路径式 URL 没问题，
# 但大量站点的文章身份就在查询串里：
#   https://x.com/article?id=1001   →   https://x.com/article
#   https://x.com/article?id=1002   →   https://x.com/article   ← 两条不同的新闻撞成一条
# 后果是「已处理」误判：第二条新闻被判为重复，静默不发（绿勾无消息的又一个来源）。
# 反过来，跟踪参数（utm_*、oc、fbclid…）必须清掉，否则同一篇稿子换个后缀就能绕过拦截。
TRACKING_PARAM_PREFIXES = ("utm_", "at_", "pk_", "mtm_", "hsa_", "_hs", "ns_")
TRACKING_PARAMS = {
    # Google News / 各家社媒投放
    "oc", "ocid", "gclid", "gclsrc", "dclid", "fbclid", "msclkid", "yclid", "twclid",
    "igshid", "igsh", "smid", "s_kwcid", "spm", "scm", "cmpid", "cmp", "ito",
    # 推荐来源 / 渠道标记
    "ref", "ref_src", "ref_url", "referrer", "referer", "source", "src",
    "campaign", "campaign_id", "share", "share_source", "from",
    # 邮件与统计
    "mc_cid", "mc_eid", "_ga", "_gl", "ncid", "sr_share",
}
_ROOT_PATH = "/"


def _is_tracking_param(name):
    low = (name or "").strip().lower()
    if not low:
        return True
    if low in TRACKING_PARAMS:
        return True
    return any(low.startswith(p) for p in TRACKING_PARAM_PREFIXES)


def clean_url(url):
    """去掉跟踪参数与锚点，保留文章身份参数，得到稳定的去重主键。

    处理规则：
      · scheme / host 统一小写，去掉默认端口
      · 去掉 #fragment
      · 查询串里只剔除跟踪参数，其余按参数名排序（保证 `?a=1&b=2` 与 `?b=2&a=1` 等价）
      · 去掉路径末尾多余的 "/"（根路径除外）
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
    except Exception:
        return raw
    if not parsed.scheme or not parsed.netloc:
        return raw

    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path or ""
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or _ROOT_PATH

    kept = []
    try:
        query = parse_qs(parsed.query, keep_blank_values=True)
    except Exception:
        query = {}
    for name in sorted(query):
        if _is_tracking_param(name):
            continue
        for value in query[name]:
            kept.append((name, value))
    new_query = urlencode(kept, doseq=False)
    return urlunparse((scheme, netloc, path, "", new_query, ""))


def _legacy_clean_url(url):
    """V1.2 之前的清洗方式：剥掉全部查询参数。

    只用于兼容 state.json 里已经存下的历史 processed_urls —— 那些键是旧规则
    生成的，不带上它们，升级后的第一次比对会全部失配，把已推过的新闻重推一遍。
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
    except Exception:
        return raw
    if not parsed.scheme or not parsed.netloc:
        return raw
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def _processed_sets(urls):
    """把历史 processed_urls 拆成两套比对键。

    `exact` —— 按新规则清洗后的键，精确比对。
    `lossy` —— 旧规则（剥光查询串）留下的键。这类键的信息已经丢了：
               存下来的时候 `?id=1001` 和 `?id=1002` 就已经撞成同一个字符串。
               为了不让升级后把已推过的新闻重推一遍，只能按旧形态兜底命中。

    只把「清洗后完全没有查询串」的历史键放进 lossy：新规则写下的键带着身份参数，
    拿它去做宽松比对会把 `?id=1002` 误判成 `?id=1001`——那正是任务 13 要修的 bug。
    """
    exact, lossy = set(), set()
    for u in urls or []:
        if not u:
            continue
        cleaned = clean_url(u)
        exact.add(cleaned)
        if "?" not in cleaned:
            lossy.add(_legacy_clean_url(u))
    return exact, lossy


def _matches_processed(url, sets):
    """这条链接是否已经处理过（同时兼容新旧两套键）。"""
    if not url:
        return False
    exact, lossy = sets
    if clean_url(url) in exact:
        return True
    return _legacy_clean_url(url) in lossy


def _normalize_link(link):
    """去除协议、查询参数、锚点与末尾斜杠，得到可比对的链接指纹。"""
    key = clean_url(link).lower()
    for prefix in ("https://", "http://", "www."):
        if key.startswith(prefix):
            key = key[len(prefix):]
    return key.rstrip("/")


def _normalize_title(title):
    """去除 Google News 附加的 ' - 媒体名' 后缀与所有标点，只保留字母/数字/汉字。"""
    text = (title or "").strip()
    if " - " in text:
        text = text.rsplit(" - ", 1)[0]
    return "".join(ch for ch in text.lower() if ch.isalnum())


def _dedupe_items(items):
    """跨源去重：链接指纹或标题指纹命中即视为重复，保留先出现的条目。"""
    seen_links, seen_titles, unique = set(), set(), []
    for it in items:
        link_key = _normalize_link(it.get("link"))
        title_key = _normalize_title(it.get("title"))
        if link_key and link_key in seen_links:
            continue
        # 过短标题（如 "Transfer news"）参与标题去重容易误杀，仅在足够长时启用
        if len(title_key) >= 12 and title_key in seen_titles:
            continue
        if link_key:
            seen_links.add(link_key)
        if len(title_key) >= 12:
            seen_titles.add(title_key)
        unique.append(it)
    return unique


def _filter_by_freshness(items, hours, require_published=True):
    """只保留发布至今不超过 hours 小时的条目。

    返回 (保留的条目, 因时间不可信被排除的条数)。

    V1.2 任务 12：默认**不再**「无日期就保留」。
    旧逻辑 `age is None → 保留`，于是「源没给 pubDate」成了绕过时效的后门：
    一篇三天前的稿子只要没有时间戳，就能被当成突发新闻推出去。
    突发新闻的前提是「刚发生」——无法证明时间，就不能算突发。
    """
    kept, unknown = [], 0
    for it in items:
        age = _age_hours(it.get("published"))
        if age is None:
            if require_published:
                unknown += 1
                continue
            kept.append(it)
            continue
        if age < -(FUTURE_SKEW_TOLERANCE_MIN / 60.0):
            unknown += 1        # 发布时间在未来：时钟不可信，等同「时间未知」
            continue
        if age <= hours:
            kept.append(it)
    return kept, unknown


def _fetch_one_source(url, headers):
    """抓取单个源（主源失败则自动走降级），返回 (url, items, status)。

    抽成独立函数是为了丢进线程池并发执行——原先 19 个源串行等网络，
    单次要 40~50 秒，而 GitHub Actions 是按分钟计费的。
    """
    status = None
    try:
        resp = requests.get(url, headers=headers, timeout=(5, 10))
        if resp.status_code == 200:
            parsed = _parse_feed_items(resp.content, url)
            # 关键：有些站点（如 thefa.com/rss）返回 200 + 可被 XML 解析的 HTML，
            # findall 得到 0 条却不抛异常。若不判空会直接跳过降级，导致该源永久空转。
            if parsed:
                return url, parsed, resp.status_code
            status = "200_EMPTY(0 items)"
        else:
            status = resp.status_code
    except Exception as e:
        status = str(e)

    # 主源不可用（403/404/超时/解析失败/空内容）时，尝试降级源
    fallback_url = FEED_FALLBACKS.get(url)
    if not fallback_url:
        return url, [], status
    try:
        fb_resp = requests.get(fallback_url, headers=headers, timeout=(5, 10))
        if fb_resp.status_code == 200:
            fb_items = _parse_feed_items(fb_resp.content, url, feed_origin=fallback_url)
            if fb_items:
                return url, fb_items, f"FALLBACK_OK({fb_resp.status_code}, {len(fb_items)} items)"
            return url, [], "FALLBACK_EMPTY(0 items)"
        return url, [], f"FALLBACK_FAIL({fb_resp.status_code})"
    except Exception as fb_err:
        return url, [], f"FALLBACK_ERR: {fb_err}"


def _source_health(source_status):
    """把各源的抓取状态归类，用于判断「是不是所有源都挂了」（V1.2 任务 17）。

    ok       真的拿到了内容（200 或降级成功）
    degraded 响应正常但零条目（源还活着，只是没新闻）
    failed   403/404/超时/解析失败/MISSING
    """
    ok = degraded = failed = 0
    for status in (source_status or {}).values():
        if isinstance(status, int):
            ok += 1 if status == 200 else 0
            failed += 0 if status == 200 else 1
            continue
        text = str(status)
        if text == "200" or text.startswith("FALLBACK_OK"):
            ok += 1
        elif "EMPTY" in text:
            degraded += 1
        else:
            failed += 1
    return {"ok": ok, "degraded": degraded, "failed": failed,
            "total": ok + degraded + failed}


def fetch_rss_sources():
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    rss_items = []
    source_status = {}

    workers = max(1, min(FETCH_CONCURRENCY, len(RSS_FEEDS)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_fetch_one_source, u, headers) for u in RSS_FEEDS]
        for fut in concurrent.futures.as_completed(futures):
            try:
                url, items, status = fut.result()
            except Exception as e:
                print(f"[ERROR] 源抓取任务异常: {type(e).__name__}: {e}")
                continue
            source_status[url] = status
            rss_items.extend(items)

    # 并发完成后按 RSS_FEEDS 原顺序重排，保证 telemetry 里的源顺序稳定可读
    source_status = {u: source_status.get(u, "MISSING") for u in RSS_FEEDS}

    raw_count = len(rss_items)
    rss_items = _dedupe_items(rss_items)
    if raw_count != len(rss_items):
        print(f"[INFO] 跨源去重：{raw_count} 条 → {len(rss_items)} 条（剔除 {raw_count - len(rss_items)} 条重复）")

    # ---- 时效硬过滤（V1.2 任务 12）----
    # 8h → 不够就放宽到 12h → 到此为止。绝不退回全量，绝不因「无发布时间」放行。
    after_dedupe = len(rss_items)
    require_published = not FRESHNESS_ALLOW_UNKNOWN
    fresh, untrusted = _filter_by_freshness(rss_items, FRESHNESS_HOURS,
                                            require_published=require_published)
    used_hours = FRESHNESS_HOURS
    if len(fresh) < MIN_FRESH_CANDIDATES:
        widened, untrusted = _filter_by_freshness(rss_items, FRESHNESS_HARD_CAP_HOURS,
                                                  require_published=require_published)
        print(f"[WARN] {FRESHNESS_HOURS}h 内仅 {len(fresh)} 条（低于 {MIN_FRESH_CANDIDATES}），"
              f"放宽到硬上限 {FRESHNESS_HARD_CAP_HOURS}h → {len(widened)} 条")
        fresh = widened
        used_hours = FRESHNESS_HARD_CAP_HOURS
    stale = max(0, after_dedupe - len(fresh) - untrusted)
    print(f"[INFO] 时效硬过滤：{after_dedupe} 条 → {len(fresh)} 条"
          f"（窗口 {used_hours}h / 硬上限 {FRESHNESS_HARD_CAP_HOURS}h；"
          f"超时排除 {stale} 条，发布时间未知或不可信排除 {untrusted} 条）")
    if not fresh:
        # 宁可这一轮什么都不发，也不拿旧闻凑数。Actions 上必须看得见原因。
        print(f"::warning title=No Fresh Candidates::{FRESHNESS_HARD_CAP_HOURS}h 内没有任何"
              f"带明确发布时间的新闻（抓取 {after_dedupe} 条全部超时或时间未知），本轮不喂模型")

    # 按发布时间倒序：最新的排最前，模型对靠前内容的注意力明显更强
    epoch = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    fresh.sort(key=lambda x: x.get("published") or epoch, reverse=True)
    return fresh, source_status

# ==================== 24 小时新闻池（早报的原料仓） ====================
# 原实现的早报根本没有「原料池」这个概念：它复用突发扫描的单条管线，
# 让 AI 从「刚刚抓到的 RSS」里再挑一条最炸的，然后套个「每日早报」的壳发出去。
# 结果有两个致命问题：
#   ① 挑中的那条极可能白天已经推过 → 命中 processed_urls → 静默拦截 → 绿勾无消息；
#   ② 根本不是「汇总」，用户要的是一篇昨日复盘的小红书笔记。
# 现在改为：每次扫描把抓到的候选写入 state.json 的 news_pool（滚动保留 72 小时），
# 早报从池子里取过去 24 小时的原料做汇总，不再受单次抓取的切片限制。

def _item_ts(item):
    """把条目里的时间字段统一成 UTC datetime；解析不出来返回 None。"""
    for field in ("published", "first_seen"):
        raw = item.get(field)
        if isinstance(raw, datetime.datetime):
            return raw.astimezone(datetime.timezone.utc) if raw.tzinfo else raw.replace(tzinfo=datetime.timezone.utc)
        if isinstance(raw, str) and raw:
            parsed = _parse_pub_date(raw)
            if parsed:
                return parsed
    return None


def _item_age_hours(item):
    ts = _item_ts(item)
    if ts is None:
        return None
    return (_now_utc() - ts).total_seconds() / 3600.0


def _merge_pool(state, items):
    """把新抓到的条目并入新闻池，返回新增条数。

    只做「加入 + 去重 + 裁剪」，不做时效硬过滤——池子本身要保留 72 小时，
    8 小时/24 小时的时效判断留到读取时（_pool_recent）再做，这样早报才有回旋余地。
    """
    pool = state.get("news_pool") or []
    seen_links = {_normalize_link(p.get("link")) for p in pool if p.get("link")}
    seen_titles = {_normalize_title(p.get("title")) for p in pool if len(_normalize_title(p.get("title"))) >= 12}
    added = 0
    for it in items:
        link_key = _normalize_link(it.get("link"))
        title_key = _normalize_title(it.get("title"))
        if link_key and link_key in seen_links:
            continue
        if len(title_key) >= 12 and title_key in seen_titles:
            continue
        ts = _item_ts(it)
        pool.append({
            "title": (it.get("title") or "")[:140],
            "link": it.get("link") or "",
            # 池子里只留 120 字描述，150 条 × 120 字 ≈ 36KB，state.json 不至于失控
            "desc": (it.get("desc") or "")[:120],
            "source": it.get("source") or "",
            # 这条稿子来自哪个 feed。V1.2 任务 1 之后，只有白名单内的 feed
            # 才允许用 <source url> 改写文章归属，所以必须把来源链路一起存下来。
            # 降级抓取时 feed_origin 是真实地址（如 news.google.com），
            # source 仍保留逻辑来源（如 thefa.com/rss）用于 telemetry。
            "feed_origin": it.get("feed_origin") or it.get("source") or "",
            "publisher": it.get("publisher") or "",
            "published": ts.isoformat() if ts else "",
            "first_seen": _now_iso(),
        })
        if link_key:
            seen_links.add(link_key)
        if len(title_key) >= 12:
            seen_titles.add(title_key)
        added += 1

    # 裁剪：先按保留时长砍掉过期项，再按时间倒序截断到上限
    cutoff = _now_utc() - datetime.timedelta(hours=POOL_RETENTION_HOURS)
    kept = []
    for p in pool:
        ts = _item_ts(p)
        if ts is None:
            continue          # 时间未知的条目对早报没用，直接丢弃
        if ts >= cutoff:
            kept.append(p)
    epoch = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    kept.sort(key=lambda p: _item_ts(p) or epoch, reverse=True)
    state["news_pool"] = kept[:POOL_MAX_ITEMS]
    return added


def _pool_recent(state, hours):
    """从新闻池取过去 hours 小时内、按时间倒序的条目（带 _age 字段）。"""
    pool = state.get("news_pool") or []
    out = []
    for p in pool:
        age = _item_age_hours(p)
        if age is None or age > hours:
            continue
        item = dict(p)
        item["_age"] = age
        out.append(item)
    epoch = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    out.sort(key=lambda p: _item_ts(p) or epoch, reverse=True)
    return out


# ==================== 早报素材资格（V1.2 任务 7） ====================
def _published_ts(item):
    """只读**发布时间**，不退化到 first_seen。

    _item_ts 会在 published 缺失时退到 first_seen（我们第一次抓到它的时间），
    那是「我们什么时候看到」而不是「新闻什么时候发生」。早报的事实时间必须
    用发布时间：拿 first_seen 顶替，等于把三天前的旧闻当成今天的新闻。
    """
    raw = item.get("published")
    if isinstance(raw, datetime.datetime):
        return raw.astimezone(datetime.timezone.utc) if raw.tzinfo else raw.replace(tzinfo=datetime.timezone.utc)
    if isinstance(raw, str) and raw.strip():
        return _parse_pub_date(raw)
    return None


def _morning_eligible(item):
    """这条素材能否进入早报的**事实新闻**列表。返回 (是否合格, 原因)。"""
    ts = _published_ts(item)
    if ts is None:
        return False, "发布时间未知"
    age = (_now_utc() - ts).total_seconds() / 3600.0
    if age < -(FUTURE_SKEW_TOLERANCE_MIN / 60.0):
        return False, f"发布时间在未来（{age:.1f}h）"
    if age > SUMMARY_WINDOW_HOURS:
        return False, f"超过 {SUMMARY_WINDOW_HOURS} 小时（{age:.1f}h）"
    return True, ""


def _pool_for_morning(state):
    """早报事实新闻的候选池：**只**取过去 24 小时、且发布时间可信的条目。

    与 _pool_recent 的区别是这里会显式拒绝「未来时间」——
    _pool_recent 用 `age > hours` 过滤，负年龄（未来时间）会直接漏过去。
    """
    pool = state.get("news_pool") or []
    out, dropped = [], {"future": 0, "unknown": 0, "stale": 0}
    for p in pool:
        ok, why = _morning_eligible(p)
        if not ok:
            if "未来" in why:
                dropped["future"] += 1
            elif "未知" in why:
                dropped["unknown"] += 1
            else:
                dropped["stale"] += 1
            continue
        item = dict(p)
        item["_age"] = (_now_utc() - _published_ts(p)).total_seconds() / 3600.0
        out.append(item)
    epoch = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    out.sort(key=lambda p: _published_ts(p) or epoch, reverse=True)
    if any(dropped.values()):
        print(f"[INFO] 早报窗口过滤：未来时间 {dropped['future']} 条 / 时间未知 "
              f"{dropped['unknown']} 条 / 超过 {SUMMARY_WINDOW_HOURS}h {dropped['stale']} 条 → "
              f"保留 {len(out)} 条")
    return out


# 早报兜底专用：直接查 Google News，不依赖 RSS 是否还留着昨天的条目。
# 池子空的时候这是最后一道防线，宁可多抓一轮，也不要空手而归。
_SUMMARY_EMERGENCY_QUERIES = [
    'Premier League when:2d',
    'Premier League transfer OR injury OR referee when:2d',
    'Arsenal OR Liverpool OR "Man United" OR Chelsea OR "Man City" OR Tottenham when:2d',
]


def _emergency_google_news_fetch():
    """早报专用紧急抓取：直接打 Google News RSS，返回条目列表（可能为空）。"""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    items = []
    for q in _SUMMARY_EMERGENCY_QUERIES:
        url = (
            "https://news.google.com/rss/search?q="
            + quote(q)
            + "&hl=en-US&gl=US&ceid=US:en"
        )
        try:
            resp = requests.get(url, headers=headers, timeout=(5, 12))
            if resp.status_code != 200:
                print(f"[WARN] 早报兜底抓取失败 HTTP {resp.status_code}: {q}")
                continue
            got = _parse_feed_items(resp.content, url, limit=20)
            print(f"[INFO] 早报兜底抓取命中 {len(got)} 条：{q}")
            items.extend(got)
        except Exception as e:
            print(f"[WARN] 早报兜底抓取异常 {type(e).__name__}: {e} | {q}")
    return _dedupe_items(items)


# ==================== 带 30 秒硬超时保护的评估主逻辑 ====================
def evaluate_with_gemini(raw_text, is_morning=False, state=None):
    mode_desc = "每日权威早报汇总" if is_morning else "重大突发新闻速递"
    
    prompt = f"""
    你是一位严谨的体育新闻事实核验员，同时也是“越位情报局”的爆款内容导演与漫画编剧。
    你必须在两套完全不同的轨道间切换：常规新闻要做出爆款网感，一旦触及严肃红线，立刻切换为官方通稿风格。

    【最新事实防错映射表（必须严格遵守）】
    - 曼城: Pep Guardiola（瓜迪奥拉）。
    - 切尔西: Enzo Maresca（马雷斯卡），所有权由 Clearlake Capital（清湖资本/埃格巴利 Eghbali）主导。
    - 曼联: Rúben Amorim（阿莫林）。
    - 利物浦: Arne Slot（斯洛特）。
    - 阿森纳: Mikel Arteta（阿尔特塔）。
    - 托特纳姆热刺: Ange Postecoglou（波斯特科格鲁）。

    请根据以下 RSS 资讯核对最新事实：
    {raw_text}

    当前任务模式：{mode_desc}

    【⏱ 时效铁律（最高优先级，凌驾于一切流量价值之上）】
    - 每条素材后面都标注了 [X小时前]，这是该条新闻的**发布时间**，不是抓取时间。
    - 只允许把「12 小时内发生或更新」的单一事件认定为 valid_news。
    - 若最炸的事件已超过 12 小时，必须放弃它，转而选择 12 小时内更新的次新事件。
    - 若全部素材都超过 12 小时，has_valid_news 必须为 false —— 宁可这一轮不发，也绝不发旧闻。
    - 同一事件有多条素材时，以其中最新那条的时间为准。
    - 必须在 event_time 字段原样抄写所选事件对应素材的 [X小时前] 标注。

    【🐦‍⬛ 严肃新闻红线规范（若触发，绝对优先执行，凌驾于一切流量考量之上）】
    触发条件（满足任意一条即触发红线）：
    1. 生命安全与健康：球员、教练、球迷或工作人员的死亡、重伤、心脏骤停、重大疾病。
    2. 严重违法犯罪与调查：假球赌球、性侵、暴力伤害、财务欺诈等被警方或官方立案调查。
    3. 灾难事故与暴力冲突：球场事故、踩踏、球迷骚乱、群体冲突、交通事故等。
    4. 传奇逝世与官方悼念：名宿或传奇人物逝世、俱乐部官方哀悼、纪念仪式。
    一旦触发，必须严格执行：
    - var_rating 固定为 "🐦‍⬛【官方严谨·黑标警示】"，不得降级为其他任何等级。
    - draft_a / draft_b / draft_c 严禁出现任何诙谐、调侃、玩梗、嘲讽、煽动对立的词汇与句式。
    - 三稿必须整体切换为官方、肃穆、客观、克制的通稿风格，只陈述已核实事实与官方表态，不做情绪引导、不设站队提问。
    - cover_blueprint 与 image_prompt 必须保持纪实、庄重、尊重当事人，不得夸张搞笑，不得使用恶搞漫画画风。

    【🔥 常规新闻：A/B/C 三阶爆款网感标准（未触发红线时执行）】
    1. draft_a（争议拆解型 · 情绪杠杆与爆款口播风）
       - 结构：短句轰炸 + 情绪卡点 + 强互动引导。
       - 开头必须是黄金 3 秒悬念或痛点问句，严禁“据报道”“据悉”等死板开场。
       - 结尾必须包含强引导性的评论区站队提问（例如“你站哪一边？”）。
    2. draft_b（证据核验型 · 金哨辟谣与信源链条风）
       - 结构：VAR 终审结论 + 证据链条梳理 + 传闻澄清。
       - 必须点明信源层级（官方公告 / BBC / 天空体育 / 名记），树立专业严谨人设。
    3. draft_c（机制解释型 · 底层逻辑与硬核增量风）
       - 结构：表象破局 + 机制拆解（财务、合同、FFP/PSR、更衣室规则）+ 未来预判。
       - 必须提供可迁移的知识增量，而不是复述新闻本身。

    【爆款流量感知指引（以下类型事件具备极高流量价值，必须优先提炼并认定为 valid_news）】
    1. 裁判与判罚争议：红黄牌申诉驳回/生效（如道曼红牌申诉）、焦点战主裁判/VAR 裁判指定、争议判罚报告、英足总/英超官方的纪律处分公告。
    2. 豪门财务与经营危机：巨额亏损（如曼联亏损数亿镑）、FFP/PSR 财务违规风险、扣分警告。
    3. 重大转会与更衣室突发：知名记者（Ornstein / Romano / 邮报）以及 Big 6 跟队记者的独家跟进与爆料。
    4. 豪门官方发布：Big 6 官网/官推的伤情通报、首发名单、主帅表态、合同续约与转会官宣。

    【VAR 情报评级标准（必须严格套用，并在 var_rating 字段原样填写等级名）】
    - 🐦‍⬛【官方严谨·黑标警示】：触发上述严肃新闻红线时使用，优先级最高。
    - ✨【金哨认证·绝对真理】(Tier 1)：官方公告、BBC、天空体育、英超官方。
    - ⚡【闪电突袭·跟队独家】(Tier 2)：知名跟队记者或一线深度突发分析。
    - 💛【吃瓜狂欢·黄牌警告】(Tier 3)：太阳报、每日邮报等爆料、财务亏损、裁判争议或话题炒作。

    评级执行细则：
    - 红线判定优先于一切流量考量；触发红线时，即便事件同时具备极高流量价值，也必须使用黑标并切换严肃文风。
    - 评级必须给出理由支撑，不得为了凑 Tier 1 而无视证据缺口；同一事件多个源说法冲突时，取其中最低等级。
    - 若现有资讯不足以判断真假，则 has_valid_news 必须为 false，且 var_rating 填 "💛【吃瓜狂欢·黄牌警告】(Tier 3)"。

    【证据字段说明（会被程序强制复核，虚报无效）】
    - evidence_sources_count 与最终证据等级由程序按真实 grounding 记录计算，你填的数值会被覆盖。
    - evidence_url 必须是你检索结果里真实出现的原文链接；编造或引用旧稿会被判为 UNVERIFIED 并直接拦截。
    - source_claims 只在同一事实点出现相反说法时填写；判断不了就留空数组，不要编造冲突。

    请严格返回合法的纯 JSON 对象（不含markdown反引号），结构如下：
    {{
      "has_valid_news": true/false,
      "var_rating": "🐦‍⬛【官方严谨·黑标警示】 / ✨【金哨认证·绝对真理】(Tier 1) / ⚡【闪电突袭·跟队独家】(Tier 2) / 💛【吃瓜狂欢·黄牌警告】(Tier 3)（四选一，原样填写）",
      "var_reason": "一句话说明为什么给这个评级（20字以内）",
      "event_title": "核心事件标题",
      "event_time": "所选事件对应素材的时效标注，原样抄写，如 2小时前",
      "fact_summary": "已核实的事实摘要",
      "evidence_url": "真实的原始报道链接（必须是你从搜索结果里实际看到的原文链接）",
      "source_claims": [
        {{"claim_key": "同一事实点的简短标识", "stance": "supports 或 denies", "source": "来源域名"}}
      ],
      "evidence_conflict": false,
      "evidence_sources_count": 1,
      "draft_a": "争议拆解型文案（爆款口播风；触发红线时为严肃通稿）",
      "draft_b": "证据核验型文案（信源链条风；触发红线时为严肃通稿）",
      "draft_c": "机制解释型文案（硬核增量风；触发红线时为严肃通稿）",
      "cover_blueprint": "封面设计指引",
      "image_prompt": "AI绘图英文Prompt（常规用 3D caricature style；触发红线时为纪实庄重风格）",
      "banner_overlay_text": "图片黄字标题（8字以内）"
    }}
    """
    
    return _call_model("analyze_news", prompt, state)


PARSE_ERROR_TEXT = "MODEL_OUTPUT_INVALID"


def _call_model(task, prompt, state):
    """统一出口：所有模型调用都从这里进 AIRouter，主程序不再碰 HTTP / SDK。

    返回 (data, error_message)。失败时 error_message 带状态码，便于日志归类。
    """
    router = get_router(state or {})
    res = getattr(router, task)(prompt)
    if not res.ok:
        return None, f"{res.status}: {res.error_message}"

    data = res.data if isinstance(res.data, dict) else None
    if data is None:
        return None, f"{PARSE_ERROR_TEXT}: 模型返回不是 JSON 对象"

    if res.provider == "gemini":
        # Gemini 的 grounding 只认 SDK 返回的工具元数据（ai_provider.GeminiProvider
        # 会用 candidates[0].grounding_metadata 覆盖这个字段）。
        # 普通 JSON 文本里自带的 grounding_records 一律丢弃——那是模型写出来的，
        # 不是检索回来的，二者必须在入口处就切开，否则「伪造 grounding」无法防御。
        data["grounding_records"] = _sdk_grounding(res)
        data["verification_status"] = ("GEMINI_GROUNDED" if data["grounding_records"]
                                       else "GEMINI_NO_GROUNDING")
    else:
        # 非 Gemini provider 没有检索能力，模型给的 grounding 全是编的，直接清空。
        # 证据只能来自程序自己抓取并落盘的 source_id（见 _finalize_result）。
        data["grounding_records"] = []
        data["verified_grounding_urls"] = []
        data["verification_status"] = f"{str(res.provider).upper()}_NO_GROUNDING"
    data["ai_provider"] = res.provider
    data["ai_model"] = res.model

    # 模型自报的证据字段一律作废，由 evidence 模块重新计算。
    # 这里连「来源等级」一起清掉：模型无权给自己的信源打分。
    for field in (
        "evidence_sources", "evidence_sources_count", "evidence_status",
        "verified_grounding_urls", "source_class", "source_level",
        "source_ratings", "publisher_trusted",
    ):
        data.pop(field, None)
    return data, None


def _sdk_grounding(res):
    """取 provider 层由 SDK 元数据解析出的 grounding 记录（唯一可信来源）。

    GeminiProvider 会用 candidates[0].grounding_metadata 的 grounding_chunks
    覆盖 data["grounding_records"]，并给每条打上 sdk=True。
    这里只收带 sdk 标记的记录：模型在 JSON 文本里自己写的 grounding_records
    没有标记，即使 provider 声称是 gemini 也会被丢弃。
    """
    records = res.data.get("grounding_records") if isinstance(res.data, dict) else None
    if not records:
        return []
    return [
        {"url": r["url"], "title": r.get("title") or ""}
        for r in records
        if isinstance(r, dict) and r.get("url") and r.get("sdk") is True
    ]


# ==================== 早报：昨日情报汇总（小红书笔记） ====================
def _age_text(age):
    if age is None:
        return "时间未知"
    if age < 1:
        return f"{int(age * 60)}分钟前"
    return f"{age:.0f}小时前"


def _build_verified_blob(events):
    """喂给模型的素材清单：**只**包含通过证据核验的事件。

    V1.2 任务 5.5 要求模型只能从已验证事件里选。原实现把整个新闻池塞进去，
    模型完全可以在 body 里写一条未核验的新闻——清单外内容必须根本不给它看到。
    """
    lines = []
    for e in events:
        lines.append(
            f"- [{e['title']}]({e['url']}) [{_age_text(e['age_hours'])}] "
            f"证据={e['status']}：{e.get('desc') or ''}"
        )
    return "\n".join(lines)


_SOURCE_CLASS_LABEL = {
    "OFFICIAL": "官方",
    "TIER1": "一线媒体",
    "LOW_TRUST": "爆料源",
    "AGGREGATOR": "聚合器",
    "UNKNOWN": "来源未识别",
}


def _build_morning_events(pool):
    """给每条素材建立独立证据记录并逐条核验（V1.2 任务 5）。

    返回 (verified, excluded)：
      · verified —— 通过核验、可以进入早报事实列表的事件，已分配 event_id
      · excluded —— 未核验 / 冲突，一律不得写成已确认事实

    注意每条素材都要独立跑一次 evaluate：早报是「多条事件汇总」，
    不能像突发扫描那样只核验一条。
    """
    records = ev.build_records([
        {"url": it.get("link"), "title": it.get("title"),
         "publisher": it.get("publisher"), "desc": it.get("desc"),
         "feed_origin": it.get("feed_origin") or it.get("source")}
        for it in pool[:SUMMARY_MAX_MATERIALS]
    ])
    by_canon = {r["canonical"]: r for r in records}

    verified, excluded = [], []
    for it in pool[:SUMMARY_MAX_MATERIALS]:
        canon = ev.canonical_url(it.get("link"))
        rec = by_canon.get(canon)
        if rec is None:
            continue
        verdict = ev.evaluate(it.get("link"), records)
        entry = {
            "title": it.get("title") or "",
            "url": it.get("link") or "",
            "desc": it.get("desc") or "",
            "domain": rec["domain"],
            "source_class": rec["source_class"],
            "source_id": rec["source_id"],
            "age_hours": it.get("_age"),
            "published_at": it.get("published") or "",
            "status": verdict["status"],
            "reason": verdict["reason"],
            "confirmed": verdict["status"] in (ev.OFFICIAL_CONFIRMED,
                                               ev.MULTIPLE_INDEPENDENT_SOURCES),
        }
        (verified if ev.is_pushable(verdict["status"]) else excluded).append(entry)

    for i, e in enumerate(verified, 1):
        e["event_id"] = f"E{i:02d}"
    return verified, excluded


def _morning_event_blob(events):
    """喂给模型的「已验证事件清单」。模型只能从这里挑 event_id。"""
    lines = []
    for e in events:
        lines.append(
            f"[{e['event_id']}] 证据={e['status']} | {_age_text(e['age_hours'])} | "
            f"{e['domain']}（{_SOURCE_CLASS_LABEL.get(e['source_class'], e['source_class'])}） | "
            f"{e['title']} | {e['url']}"
        )
    return "\n".join(lines)


def _attach_summary_evidence(result, events):
    """把模型选的 event_id 换成程序侧的权威证据字段。返回 (ok, errors)。

    V1.2 任务 5.6：模型不能自行制造 source_id / URL / evidence_status。
    因此 evidence_status、source_urls、published_at、attribution 全部由这里覆盖，
    模型只负责「挑哪几条」和「怎么写」。
    """
    by_id = {e["event_id"]: e for e in events}
    errors = []
    items = result.get("top_events")
    if not isinstance(items, list):
        return False, ["top_events 不是列表"]

    for idx, item in enumerate(items, 1):
        if not isinstance(item, dict):
            errors.append(f"top_events 第 {idx} 项不是对象")
            continue
        eid = str(item.get("event_id") or "").strip()
        evt = by_id.get(eid)
        if evt is None:
            errors.append(f"top_events 第 {idx} 项 event_id={eid!r} 不在已验证事件集合内")
            continue
        item["evidence_status"] = evt["status"]
        item["source_urls"] = [evt["url"]]
        item["published_at"] = evt["published_at"] or _age_text(evt["age_hours"])
        item["attribution"] = (
            f"{evt['domain']}（{_SOURCE_CLASS_LABEL.get(evt['source_class'], evt['source_class'])}）"
        )
        item.setdefault("time", _age_text(evt["age_hours"]))
    return (not errors), errors


def build_summary_prompt(pool_blob, stats_line, event_blob=""):
    return f"""
    你是「越位情报局」的主编，同时是一位小红书英超垂类的爆款笔记作者。
    你的任务不是播报单条突发，而是把过去 24 小时的英超情报做一次「复盘式汇总」，
    写成一篇可以直接发布的小红书笔记。

    【事实防错映射表（必须严格遵守）】
    - 曼城: Pep Guardiola（瓜迪奥拉）。
    - 切尔西: Enzo Maresca（马雷斯卡），所有权由 Clearlake Capital（清湖资本/埃格巴利 Egbahli）主导。
    - 曼联: Rúben Amorim（阿莫林）。
    - 利物浦: Arne Slot（斯洛特）。
    - 阿森纳: Mikel Arteta（阿尔特塔）。
    - 托特纳姆热刺: Ange Postecoglou（波斯特科格鲁）。

    【本次素材统计】
    {stats_line}

    【✅ 已验证事件清单（你只能从这份清单里挑选 top_events，event_id 必须原样抄写）】
    {event_blob or "（本次没有通过证据核验的事件）"}

    清单里每条都标注了程序算出的证据等级：
    - OFFICIAL_CONFIRMED（官方确认）/ MULTIPLE_INDEPENDENT_SOURCES（多源独立印证）→ 可以写成已确认事实。
    - SINGLE_SOURCE（单一来源）→ **只能写成「有报道称 / 传闻待官宣」，严禁写成已确认事实**。
    不在清单里的事件一律不许写。清单外的任何新闻、比分、转会费、伤停周数都不许补写。

    【情报池（每条都带 [X小时前]，这是发布时间，不是抓取时间）】
    {pool_blob}

    【汇总笔记的硬性要求（准确性优先于流量）】
    1. 只写上面清单与情报池里真实出现过的内容。池子里没有的事件、比分、转会费、伤停周数，一律不许补写、不许"合理推测"。
       宁可少写一条，也不许编造任何细节。
    2. 按重要性排序，从「已验证事件清单」里选出 3~5 条作为「今日必知」，每条一行。
       每条必须给出该事件的 event_id（形如 E01），程序会用 event_id 回填来源与证据状态。
    3. 必须区分信源等级：官方公告 / BBC / 天空体育 = 已确认；知名跟队记者爆料 = 传闻待官宣；太阳报 / 每日邮报 = 吃瓜。
       措辞必须体现这个差别，不许把传闻写成事实。
    4. 只允许使用过去 24 小时的事件（清单已经按此过滤过，你不需要再判断时间）。
    5. 若清单不足 5 条，就按实际条数写，不要凑数、不要编造，用「信息量偏少的一天」自然过渡。

    【小红书笔记文风标准】
    - note_title：≤20 字，带钩子，不用书名号，可用「｜」分隔。禁止「据报道」「据悉」开头。
    - hook：正文第一句，黄金 3 秒，制造信息差或悬念。
    - body：600~900 字。分 3~5 个小节，每节用 emoji + 短标题开头。
      句子要短，口语化，允许「说人话就是」「重点来了」这类口播词。
      每节结尾给一句「这对球队意味着什么」的判断，但必须用「我的判断是」标明这是判断而非事实。
      全文 emoji 总量控制在 15 个以内，不要每句话都挂表情。
    - tags：6~8 个话题标签，全部以 # 开头，必须含 #英超 和 2~3 个球队名标签。
    - 结尾必须有一句提问式的互动引导，但不得煽动对立、不得攻击球员或裁判人格。

    【严肃红线规范（触发即凌驾于一切流量考量）】
    若素材涉及死亡、重伤、疾病、违法犯罪调查、灾难事故、传奇逝世：
    该条必须排在第一位，全文使用严肃、克制、客观的通稿措辞，不得玩梗、不得调侃、不得做情绪引导。

    请严格返回合法的纯 JSON 对象（不含 markdown 反引号），结构如下：
    {{
      "has_valid_news": true,
      "note_title": "小红书笔记标题（≤20字）",
      "hook": "正文第一句钩子",
      "body": "小红书笔记正文（600~900字，分节，含 emoji 小标题）",
      "tags": "#英超 #阿森纳 #转会 ...",
      "top_events": [
        {{"rank": 1, "event_id": "E01", "title": "事件标题", "one_line": "一句话说清"}}
      ],
      "event_count": 4,
      "fact_check_note": "信息截至 X月X日 XX:XX（北京时间）",
      "cover_blueprint": "封面设计指引",
      "image_prompt": "AI绘图英文Prompt（3D caricature style）",
      "banner_overlay_text": "图片黄字标题（8字以内）"
    }}
    """


def evaluate_summary(pool_blob, stats_line, event_blob="", state=None):
    prompt = build_summary_prompt(pool_blob, stats_line, event_blob)
    # 汇总型任务不需要 grounding：Google 检索对「复盘昨天」这类问句经常不回传引用链，
    # 硬卡会把本来能出的早报误杀。准确性由「已验证事件清单 + Schema 校验」保证。
    return _call_model("generate_summary", prompt, state)


def _build_summary_card(result, meta):
    """早报卡片：结构化速览 + 可直接复制的小红书笔记全文。"""
    def esc(v):
        return html.escape(str(v or ""))

    card = "🌅 <b>【越位情报局 · 昨日情报汇总】</b>\n"
    card += f"📅 <b>{esc(meta['date'])}</b> · 覆盖过去 {meta['window']} 小时 · 素材 {meta['pool_size']} 条\n"
    if meta.get("forced"):
        card += "🔧 <b>本次为手动强制补发（FORCE_SUMMARY）</b>\n"
    if meta.get("degraded"):
        card += f"⚠️ <b>素材偏少：</b> {esc(meta['degraded'])}\n"
    card += "━━━━━━━━━━━━━━━\n"
    card += f"📌 <b>今日头条：</b> {esc(result.get('note_title'))}\n"
    if result.get("fact_check_note"):
        card += f"🧾 <b>信息截至：</b> {esc(result.get('fact_check_note'))}\n"

    events = result.get("top_events") or []
    if events:
        card += "\n🔎 <b>[今日必知]</b>\n"
        for item in events[:5]:
            rank = item.get("rank") or ""
            card += f"{esc(rank)}. <b>{esc(item.get('title'))}</b>（{esc(item.get('time'))}）\n"
            if item.get("one_line"):
                card += f"   {esc(item.get('one_line'))}\n"
            # V1.2 任务 5.8：每条都要显示证据状态与来源，让读者自己判断可信度
            status = item.get("evidence_status") or ""
            status_h = esc(ev.status_label(status)) if status else ""
            attribution_h = esc(item.get("attribution"))
            line = "   "
            if status_h:
                line += f"🛡 {status_h}"
            if attribution_h:
                line += f" · 📰 {attribution_h}"
            if line.strip():
                card += line + "\n"
            urls = item.get("source_urls") or []
            if urls:
                card += f"   🔗 {esc(urls[0])}\n"
    card += "━━━━━━━━━━━━━━━\n"
    card += "📝 <b>[小红书笔记 · 可直接复制]</b>\n\n"
    card += f"<b>【标题】</b>{esc(result.get('note_title'))}\n\n"
    if result.get("hook"):
        card += f"{esc(result.get('hook'))}\n\n"
    card += f"{esc(result.get('body'))}\n\n"
    card += f"<b>【话题标签】</b>{esc(result.get('tags'))}\n"
    card += "━━━━━━━━━━━━━━━\n"
    card += f"🎨 <b>[封面指引]</b>\n{esc(result.get('cover_blueprint'))}\n\n"
    card += f"🎭 <b>[绘图 Prompt]</b>\n<code>{esc(result.get('image_prompt'))}</code>\n"
    card += f"🏷️ <b>[黄字标题]</b> {esc(result.get('banner_overlay_text'))}"
    return card


def _published_index(*item_lists):
    """把候选与新闻池里的链接 → 发布时间建成索引，供突发新闻时效闸门使用。

    只收「有明确发布时间」的条目：没有时间的条目在这里没有意义，
    它们本来就不该出现在候选池里（见 _filter_by_freshness）。
    """
    index = {}
    for items in item_lists:
        for it in items or []:
            key = _normalize_link(it.get("link"))
            if not key or key in index:
                continue
            ts = _published_ts(it)
            if ts is not None:
                index[key] = ts
    return index


def _breaking_freshness_gate(data, published_index):
    """突发新闻 12h 硬时效闸门（V1.2 任务 12）。

    输入端已经把候选卡在 12 小时内了，但模型仍可能把一条**旧闻**选出来当突发：
    它引用的 evidence_url 可能来自新闻池（保留 72h）或 grounding 记录。
    所以推送前必须再按「证据文章自己的发布时间」复核一次。

    返回 (是否通过, 原因)。
    """
    if not published_index:
        return True, ""
    key = _normalize_link(data.get("evidence_url"))
    ts = published_index.get(key)
    if ts is None:
        # 证据文章不在本轮抓取集合里（模型引用了集合外的页面）→ 无法判定，不拦
        return True, ""
    age = _age_hours(ts)
    if age is None:
        return True, ""
    if age < -(FUTURE_SKEW_TOLERANCE_MIN / 60.0):
        return False, f"证据文章发布时间在未来（{age:.1f}h），时间不可信"
    if age > FRESHNESS_HARD_CAP_HOURS:
        return False, (f"证据文章发布于 {age:.1f} 小时前，超过突发新闻 "
                       f"{FRESHNESS_HARD_CAP_HOURS}h 硬上限")
    return True, ""


def _finalize_result(data, extra_records=None, published_index=None):
    """把模型输出收敛成「程序说了算」的最终结果。

    证据来源有两个：
      1. 模型侧 grounding 记录（Gemini 才有）；
      2. 程序侧自己抓到的原始报道（RSS 候选 + 新闻池）——这是 DeepSeek 模式下
         唯一可用的证据源，也正是「证据核验独立于模型」的落点。

    顺序：证据计算 → 评级复核 → Schema 校验。
    返回 (ok, data, reason, code)；ok=False 时 code 用于日志归类。
    """
    raw_records = list(data.get("grounding_records") or [])
    if extra_records:
        raw_records.extend(extra_records)
    records = ev.build_records(raw_records)
    verdict = ev.evaluate(
        data.get("evidence_url"),
        records,
        source_claims=data.get("source_claims"),
        model_conflict_flag=bool(data.get("evidence_conflict")),
    )
    data["evidence_records"] = records
    # 证据文章的**原始标题**：事件级去重要拿它和新的英文候选标题比对（V1.2 任务 14）。
    # 模型改写出来的 event_title 是中文，跟英文报道标题做相似度必然是 0。
    _key = _normalize_link(data.get("evidence_url"))
    data["evidence_title"] = ""
    for r in records:
        if _key and _normalize_link(r.get("url")) == _key and r.get("title"):
            data["evidence_title"] = r["title"]
            break
    data["evidence_status"] = verdict["status"]
    data["evidence_sources"] = verdict["sources"]
    data["evidence_sources_count"] = verdict["count"]
    data["evidence_reason"] = verdict["reason"]
    print(f"[INFO] 证据核验：{verdict['status']} | 独立来源 {verdict['count']} 个 | {verdict['reason']}")
    spoofed = [r for r in records if r.get("identity_conflict")]
    if spoofed:
        # 冒充官方是最需要留痕的一类问题：feed 说自己是 A，文章却挂在 B
        print("::warning title=Source Identity Conflict::"
              f"{len(spoofed)} 条记录的 publisher 域名与文章真实域名不一致"
              f"（feed={spoofed[0].get('feed_origin') or '未知'}，"
              f"publisher={spoofed[0].get('publisher_domain')}，"
              f"url={spoofed[0].get('url_domain')}），已按文章真实域名归类且不得充当官方确认")
    if verdict.get("official_rejected"):
        print("::warning title=Official Source Rejected::"
              f"官方域名 {','.join(verdict['official_rejected'])} 报道的不是同一事件，已排除，不计入官方确认")

    rating_key = out_schema.normalize_rating(data.get("var_rating"))
    if rating_key:
        classes = [s["source_class"] for s in verdict["sources"]]
        final_key, downgraded, why = ev.enforce_rating(rating_key, verdict["status"], classes)
        if downgraded:
            data["var_rating"] = out_schema.RATING_LABELS[final_key]
            data["rating_downgraded"] = why
            print(f"[WARN] 评级复核降级：{why}")

    # ---- 闸门一：状态门 + 单源门（V1.1 任务 4）----
    # SINGLE_SOURCE 默认不推送；只有白名单内的可信单一来源才放行。
    allowed, why = ev.auto_push_allowed(verdict, TRUSTED_SINGLE_SOURCES)
    if not allowed:
        return False, data, why, "EVIDENCE_NOT_PUSHABLE"

    # ---- 闸门二：黑标证据门（V1.1 任务 5）----
    # 模型自己写 var_rating=黑标 不能绕过这里：死亡/重伤/重病/违法/重大事故
    # 必须有官方确认或多源独立印证，单一来源（哪怕可信）一律不发。
    # 只扫「事实部分」（事件标题 + 事实摘要），不扫 A/B/C 稿：
    # 评论稿里出现「警方」「调查」这类词往往是比喻或引申，扫进去会大面积误杀。
    topic_text = f"{data.get('event_title') or ''} {data.get('fact_summary') or ''}"
    black_ok, black_why = ev.black_label_gate(rating_key, verdict, topic_text)
    if not black_ok:
        print(f"::warning title=Black Label Blocked::{black_why}")
        return False, data, black_why, "BLACK_LABEL_EVIDENCE_INSUFFICIENT"

    # ---- 闸门三：突发新闻 12h 硬时效（V1.2 任务 12）----
    # 证据文章自己的发布时间超过 12 小时 → 不管模型说得多炸，都不许当突发推出去。
    fresh_ok, fresh_why = _breaking_freshness_gate(data, published_index)
    if not fresh_ok:
        print(f"::warning title=Stale Breaking News::{fresh_why}")
        return False, data, fresh_why, "STALE_BREAKING_NEWS"

    # 单一来源放行时，卡片上必须带免责标注与原始来源，避免读者误以为已交叉验证
    note = ev.single_source_note(verdict)
    if note:
        data["single_source_note"] = note
        print(f"[WARN] 单一来源推送：{note['source_name']} · {note['note']}")

    ok, errors, data = out_schema.validate_scan(data)
    if not ok:
        return False, data, "Schema 校验未通过：" + out_schema.describe_errors(errors), "SCHEMA_INVALID"

    return True, data, "", ""


# ==================== 事件级去重（V1.2 任务 14） ====================
# 历史问题：event_id 由 evidence_url 哈希而来，同一件新闻被 BBC / Sky / 卫报
# 各报一次，就是三个不同的 event_id、三条 state 记录，运气不好还会推三次。
# URL 去重只能挡住「同一篇稿子」，挡不住「同一件事的不同报道」。
#
# 这里复用证据引擎的 same_event()（主体 / 事件类型 / 人物 / 关键数字 / 标题相关度
# 五个维度），把「同一件事」的判定统一到一处，避免出现两套松紧不一的标准。
EVENT_DEDUPE_WINDOW_HOURS = 168   # 只和最近 7 天的事件比对，避免 O(n) 无限膨胀
BLOCKING_DELIVERY_STATUSES = (
    store.COMMITTED, store.DELIVERED, store.PENDING_DELIVERY, store.DELIVERY_UNCERTAIN,
)


def _event_probe(title, url=""):
    """把一条事件/候选包装成 same_event() 能吃的记录结构。"""
    return {"canonical": (url or "").strip(), "title": title or ""}


_DIGITS_RE = re.compile(r"\d+")


def _numbers_in(title):
    return set(_DIGITS_RE.findall(title or ""))


def _number_conflict(a, b):
    """两条标题里的数字完全对不上 → 不是同一件事。

    证据引擎的 same_event() 用的是去掉数字后的词集合，
    所以「Arsenal beat Chelsea 2-1」和「Arsenal beat Chelsea 3-0」会被算成完全相同的标题。
    对「官方确认」这种加严场景无所谓（两侧都会被要求对齐具体事实），
    但事件级去重是**会拦截推送**的动作，比分不同必须视为不同比赛。
    """
    na, nb = _numbers_in(a), _numbers_in(b)
    return bool(na) and bool(nb) and not (na & nb)


def _find_same_event(state, title, url="", statuses=None):
    """在 state["events"] 里找「同一件事」，返回事件字典或 None。

    `statuses` 为 None 时比对全部状态（用于复用 event_id）；
    传 BLOCKING_DELIVERY_STATUSES 时只比对已投递/在途的事件（用于拦截重复推送）。
    """
    if not title:
        return None
    events = state.get("events") or {}
    if not events:
        return None
    cutoff = (_now_utc() - datetime.timedelta(hours=EVENT_DEDUPE_WINDOW_HOURS)).isoformat()
    anchor = _event_probe(title, url)
    for event in events.values():
        if statuses is not None and event.get("delivery_status") not in statuses:
            continue
        if (event.get("updated_at") or "") < cutoff:
            continue
        other_title = event.get("article_title") or event.get("title")
        if _number_conflict(title, other_title):
            continue
        ok, _why = ev.same_event(anchor, _event_probe(other_title, event.get("evidence_url")))
        if ok:
            return event
    return None


def _prepare_ai_candidates(state, items):
    """AI 调用前的候选池治理（任务书 5.1~5.5）。

    顺序严格固定：URL 规范化 → 文章去重 → 已处理过滤 → 事件指纹去重 → 排序 → 截断。
    这些判断必须发生在模型调用**之前**，否则每轮都在为旧闻反复烧 token，
    也永远解释不清「为什么模型总是挑同一条」。

    V1.2 任务 10：事件指纹去重只认「真的处理过了」。
    旧实现无条件把 state["events"] 里所有指纹塞进 known_fp，于是推送失败的新闻
    也被当成已发过，从此永久沉默——Telegram 500 一次，这条新闻就再也发不出去。
    现在改为按投递状态判断（store.blocks_rediscovery）：
      · COMMITTED / DELIVERED / PENDING_DELIVERY / DELIVERY_UNCERTAIN → 屏蔽
      · RETRYABLE_FAILURE → 到了 next_retry_at 就放回来重新投递
      · DELIVERY_FAILED   → 有限次重来，超过上限才彻底放弃
    """
    processed = _processed_sets(state.get("processed_urls"))
    now = _now_utc()
    known_fp = set()
    retry_due_fp = set()
    blocked_retry = blocked_uncertain = 0
    for e in (state.get("events") or {}).values():
        fp = e.get("event_fingerprint")
        if not fp:
            continue
        status = e.get("delivery_status")
        if status == store.RETRYABLE_FAILURE:
            if store.retry_due(e, now):
                retry_due_fp.add(fp)
                continue
            blocked_retry += 1
        if store.blocks_rediscovery(e, now):
            known_fp.add(fp)
            if status == store.DELIVERY_UNCERTAIN:
                blocked_uncertain += 1

    candidates, seen_links = [], set()
    dropped_processed = dropped_dup = dropped_same_event = 0
    revived = 0
    for it in items:
        key = clean_url(it.get("link"))
        if _matches_processed(it.get("link"), processed):
            dropped_processed += 1
            continue
        if key and key in seen_links:
            dropped_dup += 1
            continue
        # 事件级去重：这篇稿子讲的事已经投递过了（哪怕来自另一家媒体）
        if _find_same_event(state, it.get("title"), it.get("link"),
                            statuses=BLOCKING_DELIVERY_STATUSES):
            dropped_same_event += 1
            continue
        fp = store.event_fingerprint(it.get("title"), it.get("link"))
        if fp in known_fp:
            dropped_dup += 1
            continue
        if fp in retry_due_fp:
            revived += 1
        if key:
            seen_links.add(key)
        it["_fingerprint"] = fp
        candidates.append(it)

    epoch = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    candidates.sort(key=lambda x: x.get("published") or epoch, reverse=True)

    available = len(candidates)
    limit = AI_CFG.max_articles_per_run
    candidates = candidates[:limit]
    return candidates, {
        "total": len(items),
        "dropped_processed": dropped_processed,
        "dropped_dup": dropped_dup,
        "dropped_same_event": dropped_same_event,
        "available": available,
        "sent": len(candidates),
        "truncated": max(0, available - limit),
        # 失败新闻的复活情况：这三个数字解释「为什么这轮又在说同一件事」
        "revived_retry": revived,
        "waiting_retry": blocked_retry,
        "blocked_uncertain": blocked_uncertain,
    }


def _build_candidate_blob(candidates):
    """拼素材文本，并按字符预算截断，避免一次把上下文顶爆。"""
    lines, used = [], 0
    for it in candidates:
        line = f"- [{it['title']}]({it['link']}) [{_age_label(it.get('published'))}]: {it['desc']}"
        if used + len(line) > AI_CFG.max_input_chars:
            print(f"[WARN] 素材达到字符预算 {AI_CFG.max_input_chars}，后续 {len(candidates) - len(lines)} 条本轮不送模型")
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines), used


# ==================== 主执行流 ====================
def _send_low_info_morning(state, today_str, start_time, meta, reason):
    """低情报量报告（V1.2 任务 7）。

    没有通过核验的 24h 新闻时，宁可发一条诚实的空报，也不许把未核验内容
    写成事实。这条路径不调用模型——没有素材可汇总，调用只会换来编造。
    """
    card = "🌅 <b>【越位情报局 · 昨日情报汇总】</b>\n"
    card += f"📅 <b>{html.escape(today_str)}</b> · 覆盖过去 {meta['window']} 小时\n"
    if meta.get("forced"):
        card += "🔧 <b>本次为手动强制补发（FORCE_SUMMARY）</b>\n"
    card += "━━━━━━━━━━━━━━━\n"
    card += f"😴 <b>低情报量的一天</b>\n{html.escape(reason)}。\n"
    card += "今天不发凑数内容——没有通过证据核验的新闻，就不写。\n"
    card += "━━━━━━━━━━━━━━━\n"
    card += "🔎 <b>说明</b>\n"
    card += "本号只推送经过证据核验的内容：官方确认、多源独立印证，或明确标注的单一来源。\n"
    card += "未核验与来源冲突的内容不会进入早报。"

    push_success = send_telegram(card)
    state_updated = False
    if push_success:
        state["last_morning_report_date"] = today_str
        state["last_summary_sent_at"] = _now_iso()
        try:
            save_state(state)
            state_updated = True
        except store.StateError as e:
            print(f"[ERROR] 低情报量报告已发送，但日期锁落盘失败：{e}")
            print("::error title=State Write Failed After Delivery::早报已送达但 state.json 未写入")
            return 1
        print(f"[SUCCESS] 低情报量报告已推送 | {today_str}")
        print(f"::notice title=Morning Report Sent::{today_str} 低情报量报告已推送（{reason}）")
    else:
        print("[ERROR] 低情报量报告推送失败，日期锁未写入，下次触发会自动重试。")
        print("::error title=Morning Report Push Failed::Telegram 推送失败，state 未更新")

    log_telemetry({
        "start_time": start_time,
        "end_time": _now_iso(),
        "run_mode": "morning",
        "status": "LOW_INFORMATION",
        "reason": reason,
        "pool_size": meta.get("pool_size", 0),
        "window_hours": meta.get("window"),
        "push_success": push_success,
        "state_updated": state_updated,
    })
    return 0 if push_success else 1


def run_morning_mode(state, start_time):
    """早报模式：过去 24 小时情报汇总 → 小红书笔记。

    与突发扫描完全解耦，不再共用「单条事件 + URL 去重」的管线。
    这是旧版最大的坑：早报让 AI 从刚抓到的 RSS 里挑一条最炸的，而那条极可能
    白天已经推过，于是命中 processed_urls 被静默拦截 —— Actions 绿勾、Telegram 空手。
    """
    today_str = _cn_date_str()
    last_sent = state.get("last_morning_report_date") or ""
    print(f"[INFO] 早报模式启动 | 北京时间 {today_str} | 上次发送 {last_sent or '无'} | FORCE_SUMMARY={FORCE_SUMMARY}")

    if last_sent == today_str and not FORCE_SUMMARY:
        _skip(
            "MORNING_ALREADY_SENT",
            f"{today_str}（北京时间）的早报已发送过，日期锁生效；如需补发请设 FORCE_SUMMARY=true",
        )
        return 0
    if last_sent == today_str and FORCE_SUMMARY:
        print("[INFO] FORCE_SUMMARY 已生效，绕过日期锁，本次为强制补发。")

    # ---- 第 1 步：从新闻池取 24 小时原料（硬窗口，不放宽）----
    pool = _pool_for_morning(state)
    src = "state_pool"
    used_hours = SUMMARY_WINDOW_HOURS
    print(f"[INFO] 早报原料（state 新闻池 / 过去 {used_hours}h）：{len(pool)} 条")

    # ---- 第 2 步：不足则实时抓一轮（RSS 矩阵全量，并发）----
    if len(pool) < SUMMARY_MIN_MATERIALS:
        print(f"[WARN] 原料 {len(pool)} 条 < 阈值 {SUMMARY_MIN_MATERIALS} 条，触发实时抓取兜底…")
        live_items, _status = fetch_rss_sources()
        added = _merge_pool(state, live_items)
        pool = _pool_for_morning(state)
        src = f"state_pool+live(+{added})"
        print(f"[INFO] 实时抓取并入 {added} 条，24h 原料现为 {len(pool)} 条")

    # ---- 第 3 步：池子见底，打 Google News 紧急兜底 ----
    # 注意：兜底只解决「抓不到」的问题，**不改变 24 小时窗口**。
    # 抓回来的旧闻会在 _pool_for_morning 里被重新过滤掉。
    if len(pool) < SUMMARY_MIN_MATERIALS:
        print("[WARN] 池子仍不足，启动 Google News 紧急兜底抓取…")
        emergency = _emergency_google_news_fetch()
        added = _merge_pool(state, emergency)
        pool = _pool_for_morning(state)
        src += f"+gnews(+{added})"
        print(f"[INFO] 紧急抓取并入 {added} 条，24h 原料现为 {len(pool)} 条")

    # 池子有任何变化都先落盘，保证下次早报不用从零开始
    try:
        save_state(state)
    except store.StateError as e:
        print(f"[WARN] 新闻池落盘失败（不影响本次出稿，但下次仍会从零抓）：{e}")
        print("::warning title=State Write Failed::新闻池未能写入 state.json")

    meta = {
        "date": today_str,
        "window": used_hours,
        "pool_size": len(pool),
        "forced": FORCE_SUMMARY,
        "degraded": None,
    }

    # ---- 第 4 步：逐条证据核验（V1.2 任务 5）----
    verified, excluded = _build_morning_events(pool)
    print(f"[INFO] 早报证据核验：{len(pool)} 条素材 → 通过 {len(verified)} 条、"
          f"未通过 {len(excluded)} 条")
    for e in excluded[:5]:
        print(f"[INFO]   排除 {e['domain']} | {e['status']} | {e['reason'][:60]}")

    if not verified:
        # 没有通过核验的事件时，不调用模型、不编内容，直接发一份「低情报量报告」。
        # 这是任务 7 的要求：宁可发一条诚实的空报，也不许把未核验内容写成事实。
        reason = ("24 小时内没有通过证据核验的新闻" if pool
                  else "24 小时内没有抓到任何新闻")
        print(f"[WARN] {reason}，发送低情报量报告")
        print(f"::warning title=Low Information Day::{reason}")
        return _send_low_info_morning(state, today_str, start_time, meta, reason)

    if len(verified) < SUMMARY_MIN_MATERIALS:
        meta["degraded"] = f"仅 {len(verified)} 条通过核验，已按「低信息日」处理，不凑数"
        print(f"[WARN] {meta['degraded']}")

    pool_blob = _build_verified_blob(verified)
    event_blob = _morning_event_blob(verified)
    oldest = max((it.get("_age") or 0) for it in pool)
    sources = len({it.get("source") for it in pool if it.get("source")})
    stats_line = (
        f"共 {len(pool)} 条素材（时间跨度 0~{oldest:.0f} 小时，来源 {sources} 个），"
        f"其中 {len(verified)} 条通过证据核验；兜底路径：{src}"
    )
    print(f"[INFO] 早报素材统计：{stats_line}")

    result, error_msg = evaluate_summary(pool_blob, stats_line, event_blob, state)
    try:
        save_state(state)      # 落盘 AI 调用统计
    except store.StateError as e:
        print(f"[WARN] AI 调用统计落盘失败：{e}")
    if not result:
        _skip("MODEL_FAILED", f"Gemini/DeepSeek 全部失败，无法生成汇总 | {error_msg}")
        print("::error title=Morning Report Aborted::模型调用全部失败")
        return 1

    if not result.get("has_valid_news", True):
        _skip(
            "MODEL_REJECTED",
            f"模型判定素材不足以成稿 | status={result.get('verification_status')} | {error_msg or '无错误信息'}",
        )
        print("::error title=Morning Report Aborted::模型判定素材不足，未生成汇总")
        return 1

    # ---- 第 5 步：把 event_id 换成程序侧证据，再跑 Schema（V1.2 任务 5.6 / 任务 6）----
    ev_ok, ev_errors = _attach_summary_evidence(result, verified)
    allowed_ids = {e["event_id"] for e in verified}
    schema_ok, schema_errors, result = out_schema.validate_summary(
        result, allowed_event_ids=allowed_ids
    )
    if not (ev_ok and schema_ok):
        all_errors = ev_errors + schema_errors
        detail = out_schema.describe_errors(all_errors)
        _skip("SUMMARY_SCHEMA_INVALID", f"早报未通过校验，拒绝推送 | {detail}")
        print(f"::error title=Morning Report Schema Invalid::早报未通过校验，已拒绝推送 | {detail}")
        return 1

    meta["events_by_id"] = {e["event_id"]: e for e in verified}
    card = _build_summary_card(result, meta)
    push_success = send_telegram(card)

    state_updated = False
    if push_success:
        state["last_morning_report_date"] = today_str
        state["last_summary_sent_at"] = _now_iso()
        try:
            save_state(state)
            state_updated = True      # 只有确认落盘成功才算更新
        except store.StateError as e:
            print(f"[ERROR] 早报已发送，但日期锁落盘失败：{e}")
            print("::error title=State Write Failed After Delivery::早报已送达但 state.json 未写入，明日可能重复发送")
            return 1
        print(f"[SUCCESS] 早报已推送 | {today_str} | 原料 {len(pool)} 条 | 窗口 {used_hours}h")
        print(f"::notice title=Morning Report Sent::{today_str} 早报已推送，素材 {len(pool)} 条")
    else:
        print("[ERROR] 早报推送失败，日期锁未写入，下次触发会自动重试。")
        print("::error title=Morning Report Push Failed::Telegram 推送失败，state 未更新")

    log_telemetry({
        "start_time": start_time,
        "end_time": _now_iso(),
        "run_mode": "morning",
        "pool_source": src,
        "pool_size": len(pool),
        "window_hours": used_hours,
        "forced": FORCE_SUMMARY,
        "model_error": error_msg,
        "push_success": push_success,
        "state_updated": state_updated,
    })
    return 0 if push_success else 1


def _delivery_failure_status(chunk_report):
    """把分段结果汇总成事件级状态（V1.2 任务 9）。

    优先级：结果未知 > 确定性失败 > 可重试失败。
    「未知」优先级最高是因为它最危险——把它当成失败去重投，用户会收到重复推送。
    """
    statuses = {c.get("delivery_status") for c in (chunk_report or [])}
    if store.CHUNK_UNCERTAIN in statuses:
        return store.DELIVERY_UNCERTAIN
    if store.CHUNK_FAILED in statuses:
        return store.DELIVERY_FAILED
    return store.RETRYABLE_FAILURE


def _deliver_card(state, event, card):
    """推送一张卡片并如实记账（V1.2 任务 9 / 11）。

    返回 (是否全部送达, [message_id...], 最后一条错误描述)，语义与 send_telegram_ex 一致。
    调用方负责在此之前把 PENDING_DELIVERY 落盘。
    """
    prior_chunks = event.get("chunks") or []
    stored_text = event.get("pending_text") or ""
    if prior_chunks and stored_text:
        # 上一次没发完。必须沿用当时的原文续传：卡片是模型重新生成的，
        # 重新写一遍会有细微差异，分段哈希对不上就只能整条重发，用户会收到重复内容。
        if stored_text != card:
            print("[INFO] 沿用上次未发完的原文续传（保证分段一致，避免重复发送）")
        card = stored_text
    else:
        # 发之前就把原文记下来：万一下一秒进程被杀，下次还能原样续传
        event["pending_text"] = card

    chunk_report = []
    push_success, message_ids, send_err = send_telegram_ex(card, prior_chunks, chunk_report)

    if chunk_report:
        store.record_chunks(event, chunk_report)
    else:
        # 发送层被替换（测试桩）或凭证缺失时没有分段明细：
        # 按整体结果补一条单段记录，保证 state 里始终能看出「发了没发」。
        store.record_chunks(event, [{
            "chunk_index": 1,
            "chunk_total": 1,
            "delivery_status": store.CHUNK_DELIVERED if push_success else store.CHUNK_RETRYABLE,
            "telegram_message_id": message_ids[0] if message_ids else None,
            "attempts": 1,
            "last_error": "" if push_success else send_err,
        }])

    if push_success:
        store.mark_delivered(event, message_ids[0] if message_ids else None)
        return True, message_ids, send_err

    summary = store.delivery_summary(event)
    status = _delivery_failure_status(chunk_report)
    if send_err == MISSING_CREDENTIALS:
        # 凭证没配是确定性故障：重试一万次也不会变，必须红色告警等人改 Secrets
        status = store.DELIVERY_FAILED
    reason = send_err or "Telegram 推送失败"
    if status == store.DELIVERY_UNCERTAIN:
        store.mark_uncertain(event, reason)
        print("[ERROR] 请求已发出但未确认送达 → DELIVERY_UNCERTAIN，本轮不重发。")
        print(f"::error title=Delivery Uncertain::{reason} | 已送达分段 {summary['delivered_indexes']}，"
              f"未确认分段 {summary['pending_indexes']}")
    elif status == store.DELIVERY_FAILED:
        store.mark_failed(event, reason)
        print("[ERROR] 确定性失败（重试无意义）→ DELIVERY_FAILED。")
        print(f"::error title=Delivery Failed::{reason} | 已送达分段 {summary['delivered_indexes']}")
    else:
        store.mark_retryable(event, reason)
        print(f"[WARN] 可重试失败 → RETRYABLE_FAILURE，下次重投时间 {event.get('next_retry_at')}")
        print(f"::warning title=Delivery Retryable::{reason} | 已送达分段 {summary['delivered_indexes']}，"
              f"重投时间 {event.get('next_retry_at')}")

    if summary["partial"]:
        # 部分成功绝不能冒充全部成功：明确报出哪些段已经在群里了
        print(f"::warning title=Partial Delivery::本条消息共 {summary['total']} 段，"
              f"已送达第 {summary['delivered_indexes']} 段"
              f"（message_id={summary['delivered_message_ids']}），"
              f"未送达第 {summary['failed_indexes'] + summary['pending_indexes'] + summary['not_attempted_indexes']} 段")
    return False, message_ids, send_err


def run_scan_mode(state, start_time):
    rss_items, source_status = fetch_rss_sources()
    discovered_count = len(rss_items)
    health = _source_health(source_status)

    # V1.2 任务 17：所有源都挂了是**真实故障**，不能装作「今天没新闻」。
    # 旧实现会一路走到 NO_NEW_ARTICLE，返回 0，Actions 上是一个漂亮的绿勾，
    # 而实际上站点、网络或解析器已经坏了——这种「静默停摆」可以持续好几天没人发现。
    if health["total"] > 0 and health["ok"] == 0:
        print(f"::error title=All Sources Failed::"
              f"{health['failed']}/{health['total']} 个源全部抓取失败，本轮没有任何素材")
        log_telemetry({
            "start_time": start_time, "end_time": _now_iso(), "run_mode": "scan",
            "status": "FAILED_ALL_SOURCES", "source_status": source_status,
            "source_health": health, "discovered_count": discovered_count,
        })
        return 1
    if health["failed"]:
        print(f"::warning title=Partial Source Failure::"
              f"{health['failed']}/{health['total']} 个源抓取失败（其余正常）")

    # 顺手把候选写进 24 小时新闻池，供次日早报做汇总原料。
    # 这一步即使本轮不推送也要做——旧版的问题正是「没推的新闻就彻底消失了」，
    # 早报自然无米下锅。
    pool_added = _merge_pool(state, rss_items)
    print(f"[INFO] 新闻池：本轮新增 {pool_added} 条，池内共 {len(state.get('news_pool') or [])} 条"
          f"（保留 {POOL_RETENTION_HOURS}h / 上限 {POOL_MAX_ITEMS} 条）")

    # 新闻池必须当场落盘：以前只在推送成功时才 save，结果「没推的那天池子全丢」，
    # 早报第二天照样无米下锅。state_updated 只在确认写成功后置 true。
    state_updated = False
    if pool_added > 0:
        try:
            save_state(state)
            state_updated = True
        except store.StateError as e:
            print(f"[WARN] 新闻池落盘失败（不影响本轮判定，但早报原料会缺）：{e}")
            print("::warning title=State Write Failed::新闻池未能写入 state.json")

    # ---- AI 调用前治理：去重 → 过滤已处理 → 事件指纹去重 → 排序 → 截断 ----
    candidates, prep = _prepare_ai_candidates(state, rss_items)
    print(f"[INFO] AI 候选：抓取 {prep['total']} 条 → 已处理 {prep['dropped_processed']} 条、"
          f"重复稿 {prep['dropped_dup']} 条、同事件已推 {prep['dropped_same_event']} 条 "
          f"→ 可评估 {prep['available']} 条 → 本轮送模型 {prep['sent']} 条"
          + (f"（超出上限截断 {prep['truncated']} 条）" if prep["truncated"] else ""))

    if not candidates:
        # 没有新文章就不调用 AI —— 这是省额度最关键的一条
        _skip("NO_NEW_ARTICLE", "本轮没有未处理过的新文章，未调用任何 AI 接口")
        log_telemetry({
            "start_time": start_time, "end_time": _now_iso(), "run_mode": "scan",
            "status": "SKIPPED_NO_NEW_ARTICLE", "discovered_count": discovered_count,
            "candidate_stats": prep, "ai_calls": 0,
        })
        return 0

    raw_blob, blob_chars = _build_candidate_blob(candidates)

    model_result, error_msg = evaluate_with_gemini(raw_blob, is_morning=False, state=state)

    # 调用统计必须落盘，否则「今天调了几次」永远查不清
    try:
        save_state(state)
    except store.StateError as e:
        print(f"[WARN] AI 调用统计落盘失败：{e}")
    print(f"[INFO] 本轮 AI 统计：{ai.stats_snapshot(state)}")

    push_success = False
    # V1.2 任务 17：退出码必须如实反映「这轮到底有没有出故障」。
    #   0 = 正常完成（含「确实没新闻」这种正常空转）
    #   1 = 真实故障（源全挂 / 模型全挂 / 状态写失败 / 投递失败）
    exit_code = 0

    if not model_result:
        # 模型失败：不推送、保留候选（新闻池已保存）、下一轮按退避重试
        _skip("MODEL_FAILED", f"AI provider 全部失败，本轮暂停分析 | {error_msg}")
        ai_status = str(error_msg or "").split(":", 1)[0].strip()
        if ai_status in SELF_HEALING_AI_STATUSES:
            # 配额耗尽 / 429 / 熔断冷却：都是**自愈型**状态，已经用 warning 注解报过一次，
            # 下一轮会自动重试。把它们也标成 Job 失败，只会让真正的故障被红叉淹没。
            print(f"::notice title=AI Temporarily Unavailable::"
                  f"{ai_status} 属于自愈型状态，本轮跳过，下一轮自动重试（非新故障）")
        else:
            print(f"::error title=AI Unavailable::AI provider 全部失败（{ai_status}），"
                  f"本轮没有任何产出，需要人工处理")
            exit_code = 1
    elif not model_result.get("has_valid_news"):
        _skip(
            "NO_VALID_NEWS",
            f"模型判定本轮无有效新闻 | status={model_result.get('verification_status')} "
            f"| reason={model_result.get('var_reason') or '未给出'}",
        )
        model_result = None      # 必须置空，否则会掉进下面的推送分支
    else:
        # 程序侧证据：本轮候选 + 过去 24h 新闻池里的原始报道。
        # 模型只能「指认」哪一篇是原始报道，能不能算已核验由这里算。
        program_records = [
            {"url": it.get("link"), "title": it.get("title"),
             "publisher": it.get("publisher"), "desc": it.get("desc"),
             "feed_origin": it.get("feed_origin") or it.get("source")}
            for it in candidates
        ]
        for it in _pool_recent(state, 24):
            program_records.append(
                {"url": it.get("link"), "title": it.get("title"),
                 "publisher": it.get("publisher"), "desc": it.get("desc"),
                 "feed_origin": it.get("feed_origin") or it.get("source")}
            )
        # 突发新闻 12h 硬时效：证据文章自己的发布时间说了算（V1.2 任务 12）
        published_index = _published_index(rss_items, state.get("news_pool") or [])
        ok, model_result, reason, code = _finalize_result(
            model_result, program_records, published_index=published_index)
        if not ok:
            _skip(code, reason)
            model_result = None

    if model_result:
        event_title = model_result.get("event_title", "未知事件")
        evidence_url = model_result.get("evidence_url", "")
        article_title = model_result.get("evidence_title") or event_title
        # 清洗后作为去重主键：剥掉 ?oc=5 / ?utm_source 等跟踪后缀，
        # 否则同一篇稿子换个参数就能绕过拦截，或已推过的反而认不出来。
        evidence_key = clean_url(evidence_url)

        processed_urls = state.get("processed_urls", [])
        # 历史 state 里存的是未清洗（或旧规则清洗）的原始 URL，这里统一展开后再比对
        processed_keys = _processed_sets(processed_urls)
        same_event = _find_same_event(state, article_title, evidence_url,
                                      statuses=BLOCKING_DELIVERY_STATUSES)
        if _matches_processed(evidence_url, processed_keys):
            _skip("URL_DEDUPED", f"依据 URL 去重拦截：[{evidence_key}] 已处理过")
        elif same_event is not None:
            # 同一件事换了一家媒体报道：URL 不同，但事件已经推过了（V1.2 任务 14）
            _skip("EVENT_DEDUPED",
                  f"事件级去重拦截：与已投递事件 {same_event.get('event_id')} "
                  f"（{same_event.get('article_title') or same_event.get('title')}）是同一件事")
        else:
            # 黑标（严肃红线）走独立卡片样式：肃穆标题 + 分隔线，且不展示 A 稿
            is_black_label = "黑标" in str(model_result.get("var_rating") or "")

            # 对大模型或 RSS 输出的不确定文本统一做 html.escape 安全过滤，
            # 防止 < > & 等字符触发 Telegram HTTP 400。
            # 注意：evidence_url 的原始值仍用于 state 去重，这里只做展示副本，避免污染持久化数据。
            var_rating_h = html.escape(str(model_result.get("var_rating") or "⚡【闪电突袭·跟队独家】"))
            event_title_h = html.escape(str(event_title or "未知事件"))
            fact_summary_h = html.escape(str(model_result.get("fact_summary") or ""))
            evidence_url_h = html.escape(str(evidence_url or ""))
            draft_a_h = html.escape(str(model_result.get("draft_a") or ""))
            draft_b_h = html.escape(str(model_result.get("draft_b") or ""))
            draft_c_h = html.escape(str(model_result.get("draft_c") or ""))
            cover_blueprint_h = html.escape(str(model_result.get("cover_blueprint") or ""))
            image_prompt_h = html.escape(str(model_result.get("image_prompt") or ""))
            banner_overlay_text_h = html.escape(str(model_result.get("banner_overlay_text") or ""))
            var_reason_h = html.escape(str(model_result.get("var_reason") or "").strip())
            event_time_h = html.escape(str(model_result.get("event_time") or "").strip())

            if is_black_label:
                # 严肃通报模板：肃穆标题 + 分隔线，只保留 B/C 稿，杜绝流量化表达
                card = "🐦‍⬛ <b>【严肃通报 · 官方严谨·黑标警示】</b>\n"
                card += "━━━━━━━━━━━━━━━\n"
                if var_reason_h:
                    card += f"📋 <b>评级依据：</b> {var_reason_h}\n"
                card += f"📌 <b>事件：</b> {event_title_h}\n"
                if event_time_h:
                    card += f"⏱ <b>时效：</b> {event_time_h}\n"
                card += f"🛡️ <b>事实摘要：</b> {fact_summary_h}\n"
                card += f"🔗 <b>原始证据 URL：</b> {evidence_url_h}\n\n"
                card += f"🛡️ <b>[B稿: 证据核验]</b>\n{draft_b_h}\n\n"
                card += f"🧠 <b>[C稿: 机制解释]</b>\n{draft_c_h}\n\n"
                card += f"🎨 <b>[封面指引]</b>\n{cover_blueprint_h}\n\n"
                card += f"🎭 <b>[绘图 Prompt]</b>\n<code>{image_prompt_h}</code>\n"
                card += f"🏷️ <b>[标题]</b> {banner_overlay_text_h}"
            else:
                card = "🚨 <b>【越位情报局·突发新闻核验审核】</b>\n\n"
                card += f"🏆 <b>VAR 评级：</b> {var_rating_h}\n"
                if var_reason_h:
                    card += f"📋 <b>评级依据：</b> {var_reason_h}\n"
                card += f"📌 <b>事件：</b> {event_title_h}\n"
                if event_time_h:
                    card += f"⏱ <b>时效：</b> {event_time_h}\n"
                card += f"🛡️ <b>事实摘要：</b> {fact_summary_h}\n"
                card += f"🔗 <b>原始证据 URL：</b> {evidence_url_h}\n\n"
                card += f"🔥 <b>[A稿: 争议拆解]</b>\n{draft_a_h}\n\n"
                card += f"🛡️ <b>[B稿: 证据核验]</b>\n{draft_b_h}\n\n"
                card += f"🧠 <b>[C稿: 机制解释]</b>\n{draft_c_h}\n\n"
                card += f"🎨 <b>[封面指引]</b>\n{cover_blueprint_h}\n\n"
                card += f"🎭 <b>[漫画 Prompt]</b>\n<code>{image_prompt_h}</code>\n"
                card += f"🏷️ <b>[黄字标题]</b> {banner_overlay_text_h}"

            # 单一来源放行时必须显式免责：读者不能误以为这条已经过交叉验证。
            # 同时给出原始来源名与 URL，让人能自己去点开原文判断。
            ss_note = model_result.get("single_source_note") or {}
            if ss_note:
                card += (
                    f"\n\n⚠️ <b>信源提示：</b>本条为单一来源"
                    f"（{html.escape(str(ss_note.get('source_name') or '未知'))}），"
                    f"{html.escape(str(ss_note.get('note') or '未经独立交叉验证'))}"
                )
                if ss_note.get("url"):
                    card += f"\n🔗 <b>原始来源：</b>{html.escape(str(ss_note['url']))}"

            # ---- 投递状态机 ----
            # 顺序刻意如此：先落盘 PENDING_DELIVERY（含 attempts+1）→ 再发 → 记 DELIVERED → 记 COMMITTED。
            # 任何一步崩掉，下次启动都能从状态反推出「发没发出去」，避免盲目重发。
            # 同一件事如果已经有一条未完结的记录（可重试失败 / 确定性失败），
            # 复用它的 event_id，别让同一件事在 state 里长出第二条记录。
            reusable = _find_same_event(state, article_title, evidence_url)
            event = store.record_event(
                state,
                evidence_key or evidence_url,
                event_title,
                model_result["evidence_status"],
                store.VERIFIED,
                event_id=(reusable or {}).get("event_id"),
                article_title=article_title,
            )
            store.begin_delivery(event)
            try:
                save_state(state)
            except store.StateError as e:
                print(f"[ERROR] 发送前状态落盘失败，已中止本次推送：{e}")
                print("::error title=State Write Failed::state.json 写入失败，为避免「发了但没记住」已中止发送")
                log_telemetry({
                    "start_time": start_time, "end_time": _now_iso(), "run_mode": "scan",
                    "status": "ABORTED_STATE_WRITE_FAILED", "error": str(e),
                })
                return 1

            push_success, message_ids, send_err = _deliver_card(state, event, card)

            if not push_success:
                # 投递失败同样是真实故障：新闻没送到，Job 就不能报成功
                exit_code = 1
                print(f"::error title=Delivery Failed::本轮消息未能送达（状态 {event.get('delivery_status')}）")
                try:
                    save_state(state)
                except store.StateError as e:
                    print(f"[ERROR] 失败状态也未能落盘：{e}")
                    print("::error title=State Write Failed::投递失败状态未能落盘，下一轮可能重复尝试")
            else:
                # _deliver_card 内部已把状态推到 DELIVERED 并写入 message_id，
                # 这里只负责「记下已处理 URL」和「推到 COMMITTED」。
                if evidence_key:
                    # 写入的也是清洗后的 URL，保证下次比对能命中
                    processed_urls.append(evidence_key)
                    state["processed_urls"] = processed_urls[-200:]
                try:
                    save_state(state)
                except store.StateError as e:
                    # 最坏情况：消息已经发出去，但状态没存下来 → 下次可能重复推送
                    print(f"[ERROR] 消息已发送，但状态落盘失败：{e}")
                    print("::error title=State Write Failed After Delivery::消息已送达但 state.json 未写入，下次运行可能重复推送，请人工核对")
                    log_telemetry({
                        "start_time": start_time, "end_time": _now_iso(), "run_mode": "scan",
                        "status": "DELIVERED_BUT_STATE_WRITE_FAILED",
                        "message_ids": message_ids, "error": str(e),
                    })
                    return 1
                # 先确保 DELIVERED 已落盘，再推 COMMITTED：
                # 万一这次写失败，下次启动的 recovery 会看到 DELIVERED 自动补提交，而不是重发。
                store.transition(event, store.COMMITTED)
                try:
                    save_state(state)
                except store.StateError as e:
                    print(f"[WARN] COMMITTED 落盘失败，下次启动将自动补提交（不会重发）：{e}")
                state_updated = True
                print(f"[SUCCESS] 消息发送成功！message_id={message_ids}")

    log_telemetry({
        "start_time": start_time,
        "end_time": _now_iso(),
        "run_mode": "scan",
        "source_status": source_status,
        "source_health": health,
        "discovered_count": discovered_count,
        "pool_added": pool_added,
        "pool_size": len(state.get("news_pool") or []),
        "candidate_stats": prep,
        "ai_stats": ai.stats_snapshot(state),
        "model_error": error_msg,
        "push_success": push_success,
        "state_updated": state_updated,
        "exit_code": exit_code,
    })
    return exit_code


def main():
    start_time = _now_iso()

    try:
        state = load_state()
    except store.StateError as e:
        print(f"[ERROR] state.json 无法解析，拒绝启动：{e}")
        print("::error title=State Load Failed::state.json 损坏，请检查仓库中的该文件（否则去重记录会全部失效）")
        return 1

    print(f"[INFO] 启动 | RUN_MODE={RUN_MODE} | FORCE_SUMMARY={FORCE_SUMMARY} "
          f"| 北京时间 {datetime.datetime.now(CN_TZ).strftime('%Y-%m-%d %H:%M')}")

    # 恢复上次遗留的中间态：典型场景是「Telegram 已发成功，但 GitHub 状态没提交成功」。
    # 这里只补提交，绝不重发，避免用户收到两条一样的推送。
    try:
        actions = store.recovery_actions(state)
        if actions:
            for a in actions:
                print(f"[RECOVER] {a['action']} | {a['event_id']} | {a['detail']}")
            done = store.apply_recovery(state)
            save_state(state)
            print(f"[INFO] 已完成状态恢复 {len(done)} 条：{done}")
            print(f"::notice title=State Recovery::恢复了 {len(done)} 条遗留投递状态，未重发消息")
        # 结果未知的事件单独成队：它们既不重发，也不能被当成「已处理」悄悄忘掉
        uncertain = store.uncertain_queue(state)
        if uncertain:
            for event in uncertain:
                summary = store.delivery_summary(event)
                print(f"::warning title=Delivery Uncertain::{event.get('event_id')} | "
                      f"{(event.get('title') or '')[:60]} | 已送达分段 {summary['delivered_indexes']} | "
                      f"未确认分段 {summary['pending_indexes'] + summary['failed_indexes']}")
            print(f"[WARN] {len(uncertain)} 条消息送达结果未知，已进入独立恢复队列，需人工核对后处理")
    except store.StateError as e:
        print(f"[WARN] 状态恢复失败，继续本次运行：{e}")

    if RUN_MODE == "morning":
        return run_morning_mode(state, start_time)
    return run_scan_mode(state, start_time)


if __name__ == "__main__":
    sys.exit(main())
