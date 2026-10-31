#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
越位情报局 · 静默诊断工具（零依赖，仅用标准库）

用途：当「长时间没有收到推送」时，一次性定位问题出在哪一层：
  1. 网络层：RSS 源是否还活着、返回多少条
  2. 去重层：抓到的条目是否全部已在 state.json 的 processed_urls 里
  3. 候选层：现在这一轮还剩多少条「AI 可以拿来评估」的新鲜素材

用法：
  python diagnose.py                    # 读当前目录 state.json
  python diagnose.py path/to/state.json # 指定 state 文件
  python diagnose.py --no-state         # 完全忽略 state，只看抓取层

注意：本脚本只读、不写 state.json，也不会调用 Gemini / Telegram，
      因此不会产生任何费用，也不会影响线上的去重记录。
"""

import io
import os
import sys
import json
import types
import datetime
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))

# ============================================================
# 1. 依赖桩：本地没装 requests / google-genai 也能直接跑
# ============================================================
class _Resp:
    def __init__(self, code, body):
        self.status_code = code
        self.content = body
        self.text = body.decode("utf-8", "ignore") if isinstance(body, bytes) else str(body)


def _stub_requests():
    if "requests" in sys.modules:
        return
    # Actions 上已安装真实 requests，优先用真库；本地缺包才降级到 urllib 桩
    try:
        import requests  # noqa: F401
        return
    except ImportError:
        pass
    mod = types.ModuleType("requests")

    def get(url, headers=None, timeout=None, **kwargs):
        req = urllib.request.Request(url, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return _Resp(r.status, r.read())
        except urllib.error.HTTPError as e:
            return _Resp(e.code, e.read() or b"")
        except Exception:
            raise

    def post(url, json=None, timeout=None, **kwargs):
        raise RuntimeError("diagnose.py 不发送任何 POST 请求")

    mod.get = get
    mod.post = post
    sys.modules["requests"] = mod


def _stub_genai():
    if "google" in sys.modules:
        return
    try:
        from google import genai  # noqa: F401
        return
    except ImportError:
        pass
    g = types.ModuleType("google")
    gai = types.ModuleType("google.genai")
    gt = types.ModuleType("google.genai.types")
    gt.GenerateContentConfig = lambda **kw: None
    gt.Tool = lambda **kw: None
    gt.GoogleSearch = lambda **kw: None
    gai.types = gt
    g.genai = gai
    sys.modules.update({"google": g, "google.genai": gai, "google.genai.types": gt})


_stub_requests()
_stub_genai()

sys.path.insert(0, HERE)
try:
    import bot
except Exception as e:
    print(f"[FATAL] 无法导入 bot.py：{type(e).__name__}: {e}")
    sys.exit(1)


# ============================================================
# 2. 参数解析
# ============================================================
args = [a for a in sys.argv[1:]]
ignore_state = "--no-state" in args
args = [a for a in args if not a.startswith("--")]
state_path = args[0] if args else os.path.join(HERE, "state.json")

# utcnow() 在 Python 3.12 起弃用；去掉 tzinfo 后仍是裸 UTC，计算逻辑不变。
now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
now_bj = now_utc + datetime.timedelta(hours=8)

print("=" * 68)
print("越位情报局 · 静默诊断")
print("=" * 68)
print(f"当前时间  UTC {now_utc:%Y-%m-%d %H:%M:%S}  |  北京 {now_bj:%Y-%m-%d %H:%M:%S}")
print(f"运行模式  RUN_MODE={bot.RUN_MODE}")
print(f"监控源数  {len(bot.RSS_FEEDS)} 个")
print()


# ============================================================
# 3. 读取 state.json（只读）
# ============================================================
processed = []
last_morning = ""
if ignore_state:
    print("【第 1 步】state.json —— 已按 --no-state 跳过")
else:
    print("【第 1 步】state.json 去重记录")
    if not os.path.exists(state_path):
        print(f"  ✗ 未找到 {state_path}")
        print("    → 本地没有该文件是正常的：它由 GitHub Actions 生成并提交回仓库。")
        print("    → 请到 GitHub 仓库根目录打开 state.json，或 git pull 后再跑本脚本。")
    else:
        try:
            with io.open(state_path, "r", encoding="utf-8") as f:
                st = json.load(f)
            processed = st.get("processed_urls", []) or []
            last_morning = st.get("last_morning_report_date", "") or ""
            size = os.path.getsize(state_path)
            mtime = datetime.datetime.fromtimestamp(os.path.getmtime(state_path))
            print(f"  ✓ 读取成功：{size} 字节，本地修改时间 {mtime:%Y-%m-%d %H:%M:%S}")
            print(f"  · 已处理 URL 共 {len(processed)} 条（上限 200，超出后滚动淘汰最旧的）")
            print(f"  · 早报发送锁 last_morning_report_date = {last_morning or '(空)'}")
            pool = st.get("news_pool", []) or []
            print(f"  · 新闻池 news_pool 共 {len(pool)} 条（早报原料仓，保留 72h / 上限 150 条）")
            if pool:
                def _pool_ts(p):
                    raw = p.get("published") or p.get("first_seen") or ""
                    try:
                        t = datetime.datetime.fromisoformat(raw)
                    except Exception:
                        return None
                    return t if t.tzinfo else t.replace(tzinfo=datetime.timezone.utc)

                now = datetime.datetime.now(datetime.timezone.utc)
                for win in (24, 48, 72):
                    n = sum(
                        1 for p in pool
                        if _pool_ts(p) is not None
                        and (now - _pool_ts(p)).total_seconds() / 3600 <= win
                    )
                    print(f"      · 过去 {win}h 内可用：{n} 条")
            else:
                print("      ⚠ 池子是空的 → 早报会走「实时抓取 + Google News 兜底」，若仍为空则打印 [SKIP] NO_MATERIAL")

            stats = st.get("ai_stats") or {}
            if stats:
                print(f"  · AI 统计（{stats.get('date') or '无日期'}）：调用 {stats.get('calls', 0)} 次 / "
                      f"错误 {stats.get('errors', 0)} 次 / tokens "
                      f"{stats.get('input_tokens', 0)}→{stats.get('output_tokens', 0)}")
                for name, bucket in (stats.get("by_provider") or {}).items():
                    print(f"      · {name}: 调用 {bucket.get('calls', 0)} 次，错误 {bucket.get('errors', 0)} 次")
            else:
                print("  · AI 统计：尚无记录（本次运行后会写入 ai_stats）")

            breakers = st.get("circuit_breakers") or {}
            active = {k: v for k, v in breakers.items() if v.get("open_until")}
            if active:
                for name, b in active.items():
                    print(f"  · 熔断 {name}：至 {b['open_until']}（{b.get('error_type')}）")
            else:
                print("  · 熔断：无")
            if processed:
                print("  · 最近 8 条已处理记录：")
                for u in processed[-8:]:
                    print(f"      {u[:96]}")
            if len(processed) >= 200:
                print("  ⚠ 去重列表已达 200 条上限，正在滚动淘汰最旧记录")
        except Exception as e:
            print(f"  ✗ 解析失败：{type(e).__name__}: {e}")
print()


# ============================================================
# 4. 实时抓取（真实网络）
# ============================================================
print("【第 2 步】实时抓取 RSS（真实请求，约 10~30 秒）")
print("-" * 68)
items, status = bot.fetch_rss_sources()

ok_cnt = 0
for url, st in status.items():
    st_str = str(st)
    if st_str == "200":
        mark, ok_cnt = "✓", ok_cnt + 1
    elif st_str.startswith("FALLBACK_OK"):
        mark, ok_cnt = "↻", ok_cnt + 1
    else:
        mark = "✗"
    short = url.replace("https://", "").replace("http://", "")
    print(f"  {mark} {st_str[:32]:<32} {short[:46]}")
print("-" * 68)
print(f"  有效源 {ok_cnt}/{len(bot.RSS_FEEDS)}   抓取去重后共 {len(items)} 条")
print()


# ============================================================
# 5. 逐条比对去重状态
# ============================================================
print("【第 3 步】逐条比对：这些素材现在还能不能被 AI 看到")
print("-" * 68)
processed_set = set(processed)
fresh, stale = [], []
for it in items:
    link = it.get("link", "")
    is_stale = bool(link) and link in processed_set
    (stale if is_stale else fresh).append(it)
    tag = "已推过" if is_stale else "★ 新素材"
    print(f"  [{tag}] {it['title'][:56]}")
    if not is_stale:
        print(f"            {link[:90]}")
print("-" * 68)
print(f"  ★ 新鲜候选 {len(fresh)} 条   /   已推送过 {len(stale)} 条")
print()


# ============================================================
# 6. 结论与建议
# ============================================================
print("=" * 68)
print("诊断结论")
print("=" * 68)

if len(items) == 0:
    print("❌ 抓取层故障：一条都没抓到。")
    print("   可能原因：出口 IP 被全部源封禁 / DNS 异常 / 代理拦截 HTTPS。")
    print("   → 请检查第 2 步里是否所有源都是 ✗。")
elif ok_cnt == 0:
    print("❌ 所有源均不可用（可能全部走了降级且降级也失败）。")
    print("   → GitHub Actions 上大概率同样失败，需更换源或检查网络。")
else:
    print(f"✅ 抓取层正常：{ok_cnt} 个源有效，共 {len(items)} 条素材。")

print()
if not ignore_state and os.path.exists(state_path):
    if len(fresh) == 0 and len(items) > 0:
        print("⚠ 抓到的素材 100% 都是「已推送过」的。")
        print("   这意味着：AI 每轮看到的都是同一批旧闻 → 全部被 URL 去重拦截 → 静默。")
        print("   这是【静默最常见的原因】，但通常不是故障，而是源头没更新。")
        print("   验证方法：看 Actions 日志里有没有 '依据 URL 去重拦截' 这行。")
        print("   根治方法：把 _parse_feed_items 的 limit=3 调大到 8~10，")
        print("             让每个源多吐出几条，去重后仍能留下新鲜候选。")
    elif len(fresh) > 0:
        print(f"✅ 有 {len(fresh)} 条新鲜候选，说明数据链路是通的。")
        print("   若此时线上仍无推送，问题就在【AI 判定层】或【推送层】，")
        print("   请到 Actions 日志检查 model_error 与 push_success 字段。")
else:
    print("（未读取 state.json，无法判断去重层，仅完成抓取层体检）")

print()
print("下一步：打开 GitHub → Actions → 最近一次 run → 展开 Run bot.py")
print("        看最后一行 telemetry JSON 的这 4 个字段：")
print("          discovered_count  → 抓到的条数，为 0 说明抓取层挂了")
print("          model_error       → 非空说明 AI 调用失败（配额/网络/Key）")
print("          push_success      → false 且 model_error 为空，说明判定为无有效新闻")
print("          state_updated     → false 说明本轮什么都没写回，属正常静默")
print("=" * 68)
