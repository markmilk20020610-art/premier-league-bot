import os
import sys
import json
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

RSS_FEEDS = [
    "https://feeds.bbci.co.uk/sport/football/rss.xml",
    "https://www.skysports.com/rss/12040",
    "https://www.premierleague.com/rss/news",
    "https://theathletic.com/feed/"
]

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

# ==================== Telegram 推送 ====================
def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram 凭证缺失，跳过推送。")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    success = True
    for chunk in chunks:
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "Markdown",
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
def fetch_rss_sources():
    rss_items = []
    source_status = {}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    for url in RSS_FEEDS:
        try:
            resp = requests.get(url, headers=headers, timeout=(5, 10))
            source_status[url] = resp.status_code
            if resp.status_code == 200:
                root = ET.fromstring(resp.content)
                for item in root.findall(".//item")[:3]:
                    title_elem = item.find("title")
                    title = title_elem.text if title_elem is not None and title_elem.text else "无标题"
                    link_elem = item.find("link")
                    link = link_elem.text if link_elem is not None and link_elem.text else url
                    desc_elem = item.find("description")
                    desc = desc_elem.text if desc_elem is not None and desc_elem.text else ""
                    rss_items.append({"title": title, "link": link, "desc": desc, "source": url})
        except Exception as e:
            source_status[url] = str(e)
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
    
    请严格返回合法的纯 JSON 对象（不含markdown反引号），结构如下：
    {{
      "has_valid_news": true/false,
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
            mode_prefix = "🌅 **【越位情报局·每日权威早报】**" if is_morning_report else "🚨 **【越位情报局·突发新闻核验审核】**"
            card = f"{mode_prefix}\n\n"
            card += f"📌 **事件：** {event_title}\n"
            card += f"🛡️ **事实摘要：** {model_result.get('fact_summary')}\n"
            card += f"🔗 **原始证据 URL：** {evidence_url}\n\n"
            card += f"🔥 **[A稿: 争议拆解]**\n{model_result.get('draft_a')}\n\n"
            card += f"🛡️ **[B稿: 证据核验]**\n{model_result.get('draft_b')}\n\n"
            card += f"🧠 **[C稿: 机制解释]**\n{model_result.get('draft_c')}\n\n"
            card += f"🎨 **[封面指引]**\n{model_result.get('cover_blueprint')}\n\n"
            card += f"🎭 **[漫画 Prompt]**\n`{model_result.get('image_prompt')}`\n"
            card += f"🏷️ **[黄字标题]** {model_result.get('banner_overlay_text')}"
            
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
