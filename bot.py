import os
import sys
import json
import html
import re
import datetime
import requests
import xml.etree.ElementTree as ET
import concurrent.futures
from google import genai
from google.genai import types

# ==================== 配置与初始化 ====================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
RUN_MODE = os.environ.get("RUN_MODE", "scan") # scan 或 morning

STATE_FILE = "state.json"
LOG_FILE = "run_telemetry.log"

# ==================== 极速与权威 RSS 监控矩阵 (含 X 顶级名记转换) ====================
RSS_FEEDS = [
    # 1. 顶级权威主流源 (Tier 1 金哨)
    "https://feeds.bbci.co.uk/sport/football/rss.xml",
    "https://www.skysports.com/rss/12040",
    "https://theathletic.com/feed/",

    # 2. X (Twitter) 顶级名记与独家源 (通过 RSS 转换)
    # David Ornstein (TA转会第一名记)
    "https://rsshub.app/twitter/user/David_Ornstein",
    # Fabrizio Romano (转会流量与 Here We Go)
    "https://rsshub.app/twitter/user/FabrizioRomano",
    # Matt Law (切尔西与英超重磅跟队)
    "https://rsshub.app/twitter/user/Matt_Law_DT",
    # Sam Lee (曼城跟队名记)
    "https://rsshub.app/twitter/user/SamLee"
]

# ==================== 名记监控降级映射 ====================
# 说明：rsshub.app 公共实例已明确声明"仅供测试、不应用于生产"，
# 会对云端 IP（含 GitHub Actions）返回 403，且 Twitter/X 路由被禁用（302 → google.com/404）。
# 因此当上述 X 源不可用时，自动降级到 Google News RSS 检索该名记的相关报道，
# 保证监控矩阵不空转、不静默丢源。
X_FEED_FALLBACKS = {
    "https://rsshub.app/twitter/user/David_Ornstein":
        "https://news.google.com/rss/search?q=%22David+Ornstein%22+when:2d&hl=en-US&gl=US&ceid=US:en",
    "https://rsshub.app/twitter/user/FabrizioRomano":
        "https://news.google.com/rss/search?q=%22Fabrizio+Romano%22+when:2d&hl=en-US&gl=US&ceid=US:en",
    "https://rsshub.app/twitter/user/Matt_Law_DT":
        "https://news.google.com/rss/search?q=%22Matt+Law%22+Chelsea+when:2d&hl=en-US&gl=US&ceid=US:en",
    "https://rsshub.app/twitter/user/SamLee":
        "https://news.google.com/rss/search?q=%22Sam+Lee%22+Manchester+City+when:2d&hl=en-US&gl=US&ceid=US:en",
}

client = None
if GEMINI_API_KEY:
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:
        print(f"[WARN] 初始化 Gemini Client 失败: {e}")

# ==================== 状态持久化管理 ====================
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[WARN] 读取状态文件失败: {e}")
    return {"processed_urls": [], "last_morning_report_date": ""}

def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[ERROR] 保存状态失败: {e}")

def log_telemetry(data):
    entry = f"[{datetime.datetime.utcnow().isoformat()}] {json.dumps(data, ensure_ascii=False)}\n"
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


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram 凭证缺失，跳过推送。")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = _split_html(text, 4000)
    success = True
    for chunk in chunks:
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",  # 切为 HTML 模式，彻底远离 Markdown 语法崩溃
            "disable_web_page_preview": False
        }
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code != 200:
                print(f"[ERROR] Telegram 发送失败 [Code {resp.status_code}]: {resp.text}")
                success = False
        except Exception as e:
            print(f"[ERROR] Telegram 请求异常: {e}")
            success = False
    return success

# ==================== RSS 抓取（带严格超时与安全解析） ====================
def _parse_feed_items(content, source_url, limit=3):
    """安全解析 RSS 内容，返回前 limit 条条目。节点缺失或文本为空时均使用兜底值。"""
    items = []
    root = ET.fromstring(content)
    for item in root.findall(".//item")[:limit]:
        title_elem = item.find("title")
        title = title_elem.text if title_elem is not None and title_elem.text else "无标题"
        link_elem = item.find("link")
        link = link_elem.text if link_elem is not None and link_elem.text else source_url
        desc_elem = item.find("description")
        desc = desc_elem.text if desc_elem is not None and desc_elem.text else ""
        items.append({"title": title, "link": link, "desc": desc, "source": source_url})
    return items


# ==================== 跨源去重（同一稿件被多源转述时只保留首次出现） ====================
def _normalize_link(link):
    """去除协议、查询参数、锚点与末尾斜杠，得到可比对的链接指纹。"""
    key = (link or "").strip().lower()
    key = key.split("#")[0].split("?")[0]
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


def fetch_rss_sources():
    rss_items = []
    source_status = {}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    for url in RSS_FEEDS:
        try:
            resp = requests.get(url, headers=headers, timeout=(5, 10))
            source_status[url] = resp.status_code
            if resp.status_code == 200:
                rss_items.extend(_parse_feed_items(resp.content, url))
                continue
        except Exception as e:
            source_status[url] = str(e)

        # 主源不可用（403/302/超时/解析失败）时，尝试降级源
        fallback_url = X_FEED_FALLBACKS.get(url)
        if not fallback_url:
            continue
        try:
            fb_resp = requests.get(fallback_url, headers=headers, timeout=(5, 10))
            if fb_resp.status_code == 200:
                fb_items = _parse_feed_items(fb_resp.content, url)
                rss_items.extend(fb_items)
                source_status[url] = f"FALLBACK_OK({fb_resp.status_code}, {len(fb_items)} items)"
            else:
                source_status[url] = f"FALLBACK_FAIL({fb_resp.status_code})"
        except Exception as fb_err:
            source_status[url] = f"FALLBACK_ERR: {fb_err}"

    raw_count = len(rss_items)
    rss_items = _dedupe_items(rss_items)
    if raw_count != len(rss_items):
        print(f"[INFO] 跨源去重：{raw_count} 条 → {len(rss_items)} 条（剔除 {raw_count - len(rss_items)} 条重复）")
    return rss_items, source_status

# ==================== DeepSeek 兜底调用 ====================
def evaluate_with_deepseek(prompt):
    if not DEEPSEEK_API_KEY:
        return None, "未配置 DEEPSEEK_API_KEY"
    
    print("[INFO] 正在尝试使用 DeepSeek 兜底模型 (deepseek-chat)...")
    url = "https://api.deepseek.com/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}"
    }
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "你必须严格只返回 JSON 格式字符串，不得带有任何 markdown 反引号标记。"},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.2
    }
    
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=25)
        if resp.status_code == 200:
            result_json = resp.json()
            text = result_json["choices"][0]["message"]["content"].strip()
            if text.startswith("```json"):
                text = text[7:]
            if text.endswith("```"):
                text = text[:-3]
            data = json.loads(text.strip())
            data["verification_status"] = "VERIFIED_VIA_DEEPSEEK"
            print("[SUCCESS] DeepSeek 模型成功响应并完成输出！")
            return data, None
        else:
            return None, f"DeepSeek 响应异常: {resp.status_code}"
    except Exception as e:
        return None, f"DeepSeek 请求异常: {e}"

# ==================== 核心：单次 Gemini 调用（受控于超时线程） ====================
def _call_single_gemini(model_name, prompt):
    response = client.models.generate_content(
        model=model_name,
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            response_mime_type="application/json",
            temperature=0.2
        )
    )
    
    grounding_urls = []
    if response.candidates and response.candidates[0].grounding_metadata:
        gm = response.candidates[0].grounding_metadata
        if hasattr(gm, 'grounding_chunks') and gm.grounding_chunks:
            for chunk in gm.grounding_chunks:
                if hasattr(chunk, 'web') and chunk.web and chunk.web.uri:
                    grounding_urls.append(chunk.web.uri)
    
    text = response.text.strip()
    if text.startswith("```json"):
        text = text[7:]
    if text.endswith("```"):
        text = text[:-3]
    
    data = json.loads(text.strip())
    if data.get("has_valid_news") and not grounding_urls:
        data["has_valid_news"] = False
        data["verification_status"] = "REJECTED_NO_GROUNDING"
    else:
        data["verified_grounding_urls"] = grounding_urls
        data["verification_status"] = "VERIFIED_WITH_GROUNDING"
    return data

# ==================== 带 30 秒硬超时保护的评估主逻辑 ====================
def evaluate_with_gemini(raw_text, is_morning=False):
    mode_desc = "每日权威早报汇总" if is_morning else "重大突发新闻速递"
    
    prompt = f"""
    你是一位严谨的体育新闻事实核验员，同时也是“越位情报局”的爆款内容导演与诙谐漫画编剧。
    
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

    【VAR 裁判评级标准（必须严格套用，并在 var_rating 字段给出对应等级）】
    - Tier 1 金哨：官方/俱乐部官宣，或 Tier 1 名记（Ornstein、Romano 等）首发，且至少 2 个独立权威源交叉印证，并有可直接打开的原始报道链接。→ 证据链完整，可放心发布。
    - Tier 2 闪电：单一权威源（BBC / Sky / The Athletic）报道，事件本身成立，但关键细节（转会费、合同年限、体检安排、是否已签约）仍存在出入或未经二次确认。→ 事实成立，细节待跟进。
    - Tier 3 吃瓜：仅来自社交媒体传言、二手转述、聚合号搬运，无原始链接，或标题明显夸大、带节奏。→ 只能当话题聊，严禁作为事实陈述。
    - 若现有资讯不足以判断真假，则 has_valid_news 必须为 false，且 var_rating 填 "Tier 3 吃瓜"。
    - 评级必须给出理由支撑，不得为了凑 Tier 1 而无视证据缺口。

    请严格返回合法的纯 JSON 对象（不含markdown反引号），结构如下：
    {{
      "has_valid_news": true/false,
      "var_rating": "Tier 1 金哨 / Tier 2 闪电 / Tier 3 吃瓜（三选一，原样填写）",
      "var_reason": "一句话说明为什么给这个评级（20字以内）",
      "event_title": "核心事件标题",
      "fact_summary": "已核实的事实摘要",
      "evidence_url": "真实的原始报道链接",
      "evidence_sources_count": 1,
      "draft_a": "争议拆解型文案",
      "draft_b": "证据核验型文案",
      "draft_c": "机制解释型文案",
      "cover_blueprint": "封面设计指引",
      "image_prompt": "AI绘图英文Prompt（3D caricature style）",
      "banner_overlay_text": "图片黄字标题（8字以内）"
    }}
    """
    
    if client:
        candidate_models = ['gemini-2.5-flash', 'gemini-1.5-flash', 'gemini-1.5-flash-8b']
        for model_name in candidate_models:
            print(f"[INFO] 正在尝试使用模型: {model_name} (带 30秒 超时保护) ...")
            try:
                # 使用线程池实现硬超时控制，防止网络卡死
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(_call_single_gemini, model_name, prompt)
                    data = future.result(timeout=30) # 最多等 30 秒，超时直接抛出异常
                    print(f"[SUCCESS] 模型 {model_name} 成功执行并完成生成！")
                    return data, None
            except concurrent.futures.TimeoutError:
                print(f"[WARN] 模型 {model_name} 请求超时（超过30秒），主动切断并尝试下一个...")
                continue
            except Exception as e:
                print(f"[WARN] 模型 {model_name} 执行失败: {e}。切换下一个...")
                continue
                
    # 均失败或超时，降级到 DeepSeek
    print("[WARN] 所有 Gemini 尝试均失败或超时，触发自动降级，切换至 DeepSeek！")
    return evaluate_with_deepseek(prompt)

# ==================== 主执行流 ====================
def main():
    start_time = datetime.datetime.utcnow().isoformat()
    state = load_state()
    
    today_str = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    is_morning_report = (RUN_MODE == "morning")
    
    if is_morning_report and state.get("last_morning_report_date") == today_str:
        print("[INFO] 今日早报已发送过，跳过。")
        log_telemetry({"start_time": start_time, "status": "SKIPPED_MORNING_ALREADY_SENT"})
        return
        
    rss_items, source_status = fetch_rss_sources()
    raw_blob = "\n".join([f"- [{i['title']}]({i['link']}): {i['desc']}" for i in rss_items])
    
    discovered_count = len(rss_items)
    model_result, error_msg = evaluate_with_gemini(raw_blob, is_morning=is_morning_report)
    
    push_success = False
    state_updated = False
    
    if model_result and model_result.get("has_valid_news"):
        event_title = model_result.get("event_title", "未知事件")
        evidence_url = model_result.get("evidence_url", "")
        
        processed_urls = state.get("processed_urls", [])
        if evidence_url and evidence_url in processed_urls:
            print(f"[INFO] 依据 URL 去重拦截：[{evidence_url}] 已处理过。")
        else:
            mode_prefix = "🌅 <b>【越位情报局·每日权威早报】</b>" if is_morning_report else "🚨 <b>【越位情报局·突发新闻核验审核】</b>"

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

            card = f"{mode_prefix}\n\n"
            card += f"🏆 <b>VAR 评级：</b> {var_rating_h}\n"
            if var_reason_h:
                card += f"📋 <b>评级依据：</b> {var_reason_h}\n"
            card += f"📌 <b>事件：</b> {event_title_h}\n"
            card += f"🛡️ <b>事实摘要：</b> {fact_summary_h}\n"
            card += f"🔗 <b>原始证据 URL：</b> {evidence_url_h}\n\n"
            card += f"🔥 <b>[A稿: 争议拆解]</b>\n{draft_a_h}\n\n"
            card += f"🛡️ <b>[B稿: 证据核验]</b>\n{draft_b_h}\n\n"
            card += f"🧠 <b>[C稿: 机制解释]</b>\n{draft_c_h}\n\n"
            card += f"🎨 <b>[封面指引]</b>\n{cover_blueprint_h}\n\n"
            card += f"🎭 <b>[漫画 Prompt]</b>\n<code>{image_prompt_h}</code>\n"
            card += f"🏷️ <b>[黄字标题]</b> {banner_overlay_text_h}"
            
            push_success = send_telegram(card)
            if push_success:
                if evidence_url:
                    processed_urls.append(evidence_url)
                    state["processed_urls"] = processed_urls[-200:]
                if is_morning_report:
                    state["last_morning_report_date"] = today_str
                state_updated = True
                save_state(state)
                print("[SUCCESS] 消息发送成功！")
            else:
                print("[ERROR] Telegram 发送失败。")
    
    end_time = datetime.datetime.utcnow().isoformat()
    log_telemetry({
        "start_time": start_time,
        "end_time": end_time,
        "run_mode": RUN_MODE,
        "source_status": source_status,
        "discovered_count": discovered_count,
        "model_error": error_msg,
        "push_success": push_success,
        "state_updated": state_updated
    })

if __name__ == "__main__":
    main()
