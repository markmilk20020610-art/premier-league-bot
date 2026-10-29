import os
import sys
import json
import datetime
import requests
import xml.etree.ElementTree as ET
from google import genai
from google.genai import types

# ==================== 配置与初始化 ====================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
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
    client = genai.Client(api_key=GEMINI_API_KEY)

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

# ==================== RSS 抓取 ====================
def fetch_rss_sources():
    rss_items = []
    source_status = {}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    for url in RSS_FEEDS:
        try:
            resp = requests.get(url, headers=headers, timeout=8)
            source_status[url] = resp.status_code
            if resp.status_code == 200:
                root = ET.fromstring(resp.content)
                for item in root.findall(".//item")[:3]:
                    title = item.find("title").text if item.find("title") is not None else "无标题"
                    link = item.find("link").text if item.find("link") is not None else url
                    desc = item.find("description").text if item.find("description") is not None else ""
                    rss_items.append({"title": title, "link": link, "desc": desc, "source": url})
        except Exception as e:
            source_status[url] = str(e)
    return rss_items, source_status

# ==================== Gemini 评估与多模型降级重试逻辑（含最新事实防错与诙谐漫画编剧） ====================
def evaluate_with_gemini(raw_text, is_morning=False):
    if not client:
        return None, "Google GenAI Client 未初始化"
    
    mode_desc = "每日权威早报汇总" if is_morning else "重大突发新闻速递"
    
    prompt = f"""
    你是一位严谨的体育新闻事实核验员，同时也是“越位情报局”的爆款内容导演与诙谐漫画编剧。
    
    【最新事实防错映射表（必须严格遵守，绝对禁止使用旧信息）】
    - 曼城 (Manchester City)：主教练为 Pep Guardiola（瓜迪奥拉）。
    - 切尔西 (Chelsea)：主教练为 Enzo Maresca（马雷斯卡），所有权由 Clearlake Capital（清湖资本/埃格巴利 Eghbali）主导，伯利 (Todd Boehly) 已淡出日常管理。
    - 曼联 (Manchester United)：主教练为 Rúben Amorim（阿莫林）。
    - 利物浦 (Liverpool)：主教练为 Arne Slot（斯洛特）。
    - 阿森纳 (Arsenal)：主教练为 Mikel Arteta（阿尔特塔）。
    - 托特纳姆热刺 (Tottenham Hotspur)：主教练为 Ange Postecoglou（波斯特科格鲁）。

    请根据以下抓取到的 RSS 资讯，并使用 Google Search Grounding 联网核对过去几小时内欧洲足坛的最新事实：
    {raw_text}
    
    当前任务模式：{mode_desc}
    
    请严格返回合法的 JSON 对象（不得包含 markdown 代码块反引号，仅返回纯 JSON 字符串），结构如下：
    {{
      "has_valid_news": true/false,
      "event_title": "核心事件标题",
      "fact_summary": "已核实的事实摘要",
      "evidence_url": "真实的原始报道链接（必须来自搜索Grounding支持的有效URL）",
      "evidence_sources_count": 1,
      "draft_a": "争议拆解型文案草稿",
      "draft_b": "证据核验型文案草稿",
      "draft_c": "机制解释型文案草稿",
      "cover_blueprint": "P1封面设计指引",
      "image_prompt": "专门给AI绘图模型用的英文Prompt（3D caricature style, 诙谐夸张漫画隐喻，包含人物标志性特征）",
      "banner_overlay_text": "叠加在图片上的黄色高对比度标题（8字以内）"
    }}
    
    约束：如果未发现具有高信源支持的重大新闻，或者搜索证据不足，has_valid_news 必须设为 false。
    """
    
    candidate_models = ['gemini-2.5-flash', 'gemini-1.5-flash', 'gemini-1.5-flash-8b']
    last_error = None
    
    for model_name in candidate_models:
        try:
            print(f"[INFO] 正在尝试使用模型: {model_name} ...")
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
                print("[WARN] 拦截幻觉：模型声明有新闻但未提供有效的 Google Search Grounding 支撑 URL。")
                data["has_valid_news"] = False
                data["verification_status"] = "REJECTED_NO_GROUNDING"
            else:
                data["verified_grounding_urls"] = grounding_urls
                data["verification_status"] = "VERIFIED_WITH_GROUNDING"
            
            print(f"[SUCCESS] 模型 {model_name} 成功执行并完成生成！")
            return data, None
        except Exception as e:
            last_error = str(e)
            print(f"[WARN] 模型 {model_name} 执行失败: {e}。自动切换至队列中的下一个模型...")
            continue
            
    return None, f"所有候选 Gemini 模型调用均失败，最终错误: {last_error}"

# ==================== 主执行流 ====================
def main():
    start_time = datetime.datetime.utcnow().isoformat()
    state = load_state()
    
    today_str = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    is_morning_report = (RUN_MODE == "morning")
    
    if is_morning_report and state.get("last_morning_report_date") == today_str:
        print("[INFO] 今日早报已发送过，跳过本次早报任务。")
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
            print(f"[INFO] 依据 URL/GUID 去重拦截：URL [{evidence_url}] 已经处理过，跳过推送。")
        elif is_morning_report and state.get("last_morning_report_date") == today_str:
            print("[INFO] 早报防重拦截：今日已发送。")
        else:
            mode_prefix = "🌅 **【越位情报局·每日权威早报】**" if is_morning_report else "🚨 **【越位情报局·突发新闻核验审核】**"
            card = f"{mode_prefix}\n\n"
            card += f"📌 **事件：** {event_title}\n"
            card += f"🛡️ **事实摘要：** {model_result.get('fact_summary')}\n"
            card += f"🔗 **原始证据 URL：** {evidence_url}\n"
            card += f"🔍 **Grounding 独立引用源数：** {len(model_result.get('verified_grounding_urls', []))}\n\n"
            card += f"🔥 **[A稿: 争议拆解型]**\n{model_result.get('draft_a')}\n\n"
            card += f"🛡️ **[B稿: 证据核验型]**\n{model_result.get('draft_b')}\n\n"
            card += f"🧠 **[C稿: 机制解释型]**\n{model_result.get('draft_c')}\n\n"
            card += f"🎨 **[封面指引]**\n{model_result.get('cover_blueprint')}\n\n"
            card += f"🎭 **[漫画梗图 Prompt]**\n`{model_result.get('image_prompt')}`\n"
            card += f"🏷️ **[图片黄字标题]** {model_result.get('banner_overlay_text')}"
            
            push_success = send_telegram(card)
            if push_success:
                if evidence_url:
                    processed_urls.append(evidence_url)
                    state["processed_urls"] = processed_urls[-200:]
                if is_morning_report:
                    state["last_morning_report_date"] = today_str
                state_updated = True
                save_state(state)
                print("[SUCCESS] 消息发送成功，状态已成功更新并保存。")
            else:
                print("[ERROR] Telegram 发送失败，状态未提交，允许后续重试。")
    
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
