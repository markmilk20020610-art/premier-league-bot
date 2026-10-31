"""本地端到端冒烟：真实执行 bot.run_scan_mode，只把外部 HTTP 换成本地 mock。

用途：在没有 API Key、不想消耗任何额度的前提下，拿到一份真实的运行记录，
证明「抓取 → 去重 → 批量调用 → 证据核验 → 推送」整条链路真的跑得通。

用法：
    python scripts/smoke_run.py            # 正常一轮
    python scripts/smoke_run.py --quota    # 模拟 DeepSeek 余额耗尽

注意：这不是 GitHub Actions 的真实运行记录，Actions 记录需上线后自行截图。
"""
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

MODE_QUOTA = "--quota" in sys.argv
CAPTURED = {"messages": [], "ai_calls": 0}

BBC = "https://www.bbc.co.uk/sport/football/articles/c0"

ANALYSIS = {
    "has_valid_news": True,
    "var_rating": "✨【金哨认证·绝对真理】(Tier 1)",
    "var_reason": "官方公告 + 一线媒体",
    "event_title": "英足总驳回阿森纳红牌申诉",
    "event_time": "2小时前",
    "fact_summary": "英足总官方公告确认驳回阿森纳关于道曼红牌的申诉，球员停赛三场。",
    "evidence_url": BBC,
    "draft_a": "这次申诉被驳回，阿森纳到底冤不冤？你站哪一边？",
    "draft_b": "英足总官方公告已确认，BBC 与天空体育同步报道。",
    "draft_c": "纪律条例规定申诉窗口 48 小时，逾期不予受理。",
    "cover_blueprint": "深色底 + 红色警示条 + 大字标题",
    "image_prompt": "3d caricature style referee holding a red card",
    "banner_overlay_text": "申诉被驳",
}


class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        payload = json.loads(raw or b"{}")

        if "/chat/completions" in self.path:
            CAPTURED["ai_calls"] += 1
            if MODE_QUOTA:
                self._json(402, {"error": {"message": "Insufficient Balance"}})
                return
            self._json(200, {
                "choices": [{"message": {"content": json.dumps(ANALYSIS, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 1200, "completion_tokens": 500},
            })
            return

        if "/sendMessage" in self.path:
            CAPTURED["messages"].append(payload.get("text", ""))
            self._json(200, {"ok": True, "result": {"message_id": 9001}})
            return

        self._json(404, {"ok": False})

    def do_GET(self):
        self._json(200, {"data": []})


def start_server():
    server = HTTPServer(("127.0.0.1", 0), MockHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def fixture_items():
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    fixtures = [
        # 原始报道（evidence_url 指向这一条）
        ("Arsenal lose appeal against Dowman red card",
         "https://www.bbc.co.uk/sport/football/articles/c0", "https://www.bbc.co.uk"),
        # 同一篇稿的转载，标题几乎一致 → 应被判为同一来源
        ("Arsenal lose appeal against Dowman red card",
         "https://www.dailymail.com/sport/article-1", "https://www.dailymail.com"),
        # 另一家独立报道同一事件 → 应算第 2 个独立来源
        ("FA rejects Arsenal appeal over Dowman red card",
         "https://www.skysports.com/football/news/1", "https://www.skysports.com"),
        # 无关新闻，不应参与该事件的交叉印证
        ("Chelsea injury update ahead of weekend fixture",
         "https://www.chelseafc.com/news/1", "https://www.chelseafc.com"),
    ]
    return [{
        "title": t,
        "link": link,
        "desc": "fixture description",
        "source": "https://feeds.bbci.co.uk/sport/football/rss.xml",
        "publisher": publisher,
        "published": now,
    } for t, link, publisher in fixtures]


def main():
    server, base = start_server()
    work = tempfile.mkdtemp()
    os.chdir(work)

    os.environ["TELEGRAM_BOT_TOKEN"] = "123456:TESTTOKEN"
    os.environ["TELEGRAM_CHAT_ID"] = "1"
    os.environ["TELEGRAM_API_BASE"] = base
    os.environ["DEEPSEEK_API_KEY"] = "test-key"
    os.environ["AI_PRIMARY_PROVIDER"] = "deepseek"
    os.environ["AI_GEMINI_ENABLED"] = "false"

    import bot
    import ai_provider as ai

    bot.TELEGRAM_API_BASE = base
    bot.TELEGRAM_BOT_TOKEN = "123456:TESTTOKEN"
    bot.TELEGRAM_CHAT_ID = "1"
    bot.STATE_FILE = os.path.join(work, "state.json")
    bot.fetch_rss_sources = lambda: (fixture_items(), {"fixture": "200"})

    cfg = ai.AIConfig()
    providers = ai.build_providers(cfg)
    providers["deepseek"].api_key = "test-key"
    providers["deepseek"].BASE_URL = f"{base}/chat/completions"
    bot.ROUTER = ai.AIRouter({}, config=cfg, providers=providers)

    print("=" * 66)
    print(f"冒烟模式：{'QUOTA_EXHAUSTED' if MODE_QUOTA else '正常'} | mock={base}")
    print("=" * 66)

    rc = bot.run_scan_mode(bot.load_state(), bot._now_iso())

    state = bot.load_state()
    print("=" * 66)
    print(f"退出码           : {rc}")
    print(f"AI 调用次数      : {CAPTURED['ai_calls']}（4 条素材去重后应为 1 次）")
    print(f"Telegram 收到    : {len(CAPTURED['messages'])} 条")
    print(f"新闻池           : {len(state.get('news_pool') or [])} 条")
    print(f"AI 统计          : {ai.stats_snapshot(state)}")
    print(f"熔断状态         : {state.get('circuit_breakers') or '无'}")
    events = state.get("events") or {}
    print(f"投递事件         : {[(e['delivery_status'], e.get('telegram_message_id')) for e in events.values()]}")
    print("=" * 66)

    server.shutdown()

    ok = True
    if MODE_QUOTA:
        ok = rc == 0 and not CAPTURED["messages"] and bool(state.get("circuit_breakers"))
    else:
        ok = rc == 0 and len(CAPTURED["messages"]) == 1 and CAPTURED["ai_calls"] == 1
    print("冒烟结果：" + ("通过" if ok else "未通过"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
