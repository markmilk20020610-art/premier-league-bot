# AI 模型调用体系整改报告

执行日期：2026-10-31
范围：模型 Provider 层、熔断与预算、DeepSeek 主模型调用优化、调用前候选治理

---

## 一、修改文件清单

| 文件 | 类型 | 说明 |
|---|---|---|
| `ai_provider.py` | 新增 | Provider 层：统一接口、DeepSeek/Gemini/OpenAI 实现、熔断器、预算、调用统计 |
| `bot.py` | 改造 | 删除全部散落的模型调用代码，改为经 AIRouter；新增调用前候选治理 |
| `evidence.py` | 改造 | 支持程序侧证据（RSS 原始报道）；新增同事件相关性过滤 |
| `diagnose.py` | 改造 | 输出当日 AI 调用统计与熔断状态 |
| `.github/workflows/scanner.yml` | 改造 | 注入模型策略环境变量 |
| `scripts/smoke_run.py` | 新增 | 本地端到端冒烟（真实执行，仅把外部 HTTP 换成本地 mock） |
| `tests/test_ai_provider.py` | 新增 | Provider 层单测（16 例） |
| `tests/test_ai_integration.py` | 新增 | 主流程联调（9 例） |
| `tests/test_pipeline.py` | 改造 | 适配新的 Provider 层 |
| `tests/test_evidence.py` | 改造 | 适配程序侧证据与相关性过滤 |
| `tests/run_all.py` | 改造 | 纳入新测试模块 |

---

## 二、修改前后代码差异

### 2.1 bot.py 不再直接调用模型

```python
# 修改前：主程序自己持有 SDK client，自己轮询 3 个 Gemini 模型，自己调 DeepSeek
client = genai.Client(api_key=GEMINI_API_KEY)
def _run_model_pipeline(prompt, require_grounding=True):
    for model_name in ['gemini-2.5-flash', 'gemini-1.5-flash', 'gemini-1.5-flash-8b']:
        ...
    return evaluate_with_deepseek(prompt)      # 失败才降级

# 修改后：主程序只认 AIRouter
def _call_model(task, prompt, state):
    router = get_router(state or {})
    res = getattr(router, task)(prompt)
    if not res.ok:
        return None, f"{res.status}: {res.error_message}"
    ...
```

`bot.py` 已不含任何 `genai` / `api.deepseek.com` 字样的调用代码，只剩一句注释。

### 2.2 统一 Provider 接口

```python
class AIRouter:
    def analyze_news(self, prompt, **kw)      # 单条事件分析
    def generate_summary(self, prompt, **kw)  # 每日汇总
    def generate_content(self, prompt, **kw)  # 通用文案
    def health_check(self)                    # 各 provider 健康状态
```

### 2.3 熔断状态持久化

```python
breaker.trip(reason, cooldown_minutes, error_type)
# state["circuit_breakers"]["deepseek"] = {
#   "state": "open", "open_until": "...", "reason": "...", "error_type": "QUOTA_EXHAUSTED", "failures": 1
# }
```

熔断期内 `_call` 直接跳过该 provider，不产生任何网络请求。

### 2.4 调用前候选治理（省额度的关键）

```python
# 修改前：把全部 RSS 条目塞给模型，模型挑完才查 processed_urls
raw_blob = "\n".join(...)   # 全部条目
model_result = evaluate_with_gemini(raw_blob)
... 之后才判断 evidence_url 是否推过

# 修改后：先过滤，再决定要不要调用模型
candidates, prep = _prepare_ai_candidates(state, rss_items)
if not candidates:
    _skip("NO_NEW_ARTICLE", "本轮没有未处理过的新文章，未调用任何 AI 接口")
    return 0
```

治理顺序：URL 规范化 → 文章去重 → 已处理过滤 → 事件指纹去重 → 按时间排序 → 按条数/字符预算截断。

### 2.5 证据核验与模型解耦

```python
# 修改前：只认 Gemini 的 grounding，DeepSeek 结果永远 UNVERIFIED（等于整个 bot 推不出去）
records = ev.build_records(data.get("grounding_records") or [])

# 修改后：grounding + 程序自己抓到的原始报道一起作为证据源
raw_records = list(data.get("grounding_records") or [])
if extra_records:                      # 本轮候选 + 24h 新闻池
    raw_records.extend(extra_records)
records = ev.build_records(raw_records)
```

并新增「同事件相关性过滤」：只统计与所选报道标题词重叠度达标的记录，
避免新闻池里几百条无关稿件把任何一条新闻都算成「多源印证」。

---

## 三、AI Provider 调用流程

```
                    ┌─────────────────────────────┐
                    │ 1. RSS 抓取（19 源并发）      │
                    │    失败自动降级 Google News   │
                    └──────────────┬──────────────┘
                                   ▼
                    ┌─────────────────────────────┐
                    │ 2. 候选治理（调用前）         │
                    │   规范化→去重→过滤已处理      │
                    │   →事件指纹→排序→截断        │
                    └──────────────┬──────────────┘
                                   ▼
              ┌────────────────────────────────────┐
              │ 3. 闸门：AIRouter                  │
              │   · 候选为空 → 不调用，直接退出      │
              │   · （无每日额度闸门，V1.3 已删）  │
              │   · 熔断中 → CIRCUIT_OPEN，零请求   │
              │   · Gemini 未启用 → 不进候选队列    │
              └───────────────┬────────────────────┘
                              ▼
                    ┌─────────────────────────────┐
                    │ 4. DeepSeek 批量调用          │
                    │   一轮一次请求                │
                    │   429/5xx/超时 → 指数退避重试  │
                    │   402/401 → 立即熔断          │
                    └──────────────┬──────────────┘
                                   ▼
                    ┌─────────────────────────────┐
                    │ 5. 闸门：证据核验（程序算）    │
                    │   独立来源 / 转载合并 / 冲突   │
                    │   UNVERIFIED、CONFLICTING 拦截│
                    └──────────────┬──────────────┘
                                   ▼
                    ┌─────────────────────────────┐
                    │ 6. Schema 校验 → Telegram    │
                    │   → DELIVERED → COMMITTED   │
                    └─────────────────────────────┘
```

---

## 四、测试结果

```
$ python tests/run_all.py
Ran 82 tests in 47.515s
OK
模块数 7 | 用例 82 | 失败 0 | 错误 0 | 耗时 47.5s
```

其中本批新增 25 个用例（Provider 16 + 联调 9）。

### 任务书要求的 7 项验证

| 编号 | 场景 | 断言 | 结果 |
|---|---|---|---|
| 测试 1 | Gemini 关闭 | Gemini 调用计数 0，全部请求只发往 DeepSeek endpoint | 通过 |
| 测试 2 | 没有新文章 | HTTP 请求数 0，`ai_stats.calls = 0`，日志 `[SKIP] NO_NEW_ARTICLE` | 通过 |
| 测试 3 | 10 篇新文章 | 合并为 **1 次**调用，10 条素材全部在同一 prompt 内 | 通过 |
| 测试 4 | DeepSeek 429 | 1 首发 + 2 重试后停止，退避 1s→2s，不无限重试 | 通过 |
| 测试 5 | 余额耗尽（402） | 不重试、不推送、候选保留在新闻池、熔断持久化 | 通过 |
| 测试 6 | Gemini 启用但配额耗尽 | 首次真实调用后熔断，第二轮零请求 | 通过 |
| 测试 7 | has_valid_news=true 但 UNVERIFIED | 不推送，不产生投递事件 | 通过 |

### 模拟 API 错误覆盖

| 错误 | 状态码 | 系统行为 | 用例 |
|---|---|---|---|
| 限流 | 429 | 指数退避，最多 2 次重试 | `test_retry_with_backoff_but_not_infinite` |
| 配额耗尽 | 402 / "Insufficient Balance" | 不重试，熔断 120 分钟 | `test_quota_exhausted_opens_breaker_and_stops` |
| 鉴权失败 | 401 | 不重试，熔断 1440 分钟 + `::error::` | `test_auth_failure_stops_immediately` |
| 服务端错误 | 503 | 每轮重试，连续 3 轮熔断 10 分钟 | `test_server_error_retries_then_opens` |
| 返回非法 JSON | 200 | `PARSE_ERROR`，不推送 | `test_provider_failure_returns_status` |
| 每日额度耗尽 | — | **V1.3 起不存在**：不再有每日 token / 次数上限，`BUDGET_EXCEEDED` 已从状态机移除 | `test_huge_ledger_still_calls_provider` |

---

## 五、真实运行记录

### 5.1 本地端到端冒烟（真实执行）

`python scripts/smoke_run.py` 会启动本地 mock 服务，真实执行 `bot.run_scan_mode`，
只有外部 HTTP 被指向 127.0.0.1。

```
冒烟模式：正常 | mock=http://127.0.0.1:56617
[INFO] 新闻池：本轮新增 3 条，池内共 3 条
[INFO] AI 候选：抓取 4 条 → 已处理 0 条、重复/同事件 0 条 → 可评估 4 条 → 本轮送模型 4 条
[AI] provider=deepseek model=deepseek-chat status=OK ok=True 45ms tokens=1200/500
[INFO] 证据核验：MULTIPLE_INDEPENDENT_SOURCES | 独立来源 2 个 | bbc.co.uk, skysports.com
[SUCCESS] 消息发送成功！message_id=[9001]
AI 调用次数：1     Telegram 收到：1 条
投递事件：('COMMITTED', 9001)
冒烟结果：通过
```

配额耗尽模式：

```
[AI] provider=deepseek status=QUOTA_EXHAUSTED
::warning title=AI Quota Exhausted::deepseek 配额耗尽，熔断 120 分钟
[SKIP] MODEL_FAILED | AI 全部失败，本轮暂停分析
AI 调用次数：1     Telegram 收到：0 条
新闻池：3 条（候选保留）
熔断状态：{'deepseek': {'state': 'open', 'error_type': 'QUOTA_EXHAUSTED', ...}}
冒烟结果：通过
```

### 5.2 GitHub Actions 运行记录

**本批尚未产生真实 Actions 运行记录。** 本地环境无仓库推送权限、无真实 API Key，
无法代替线上运行。上线后请按下面清单核对，并把截图补进本文档：

1. Actions → Run workflow → mode=`scan` → 观察日志出现
   `[INFO] AI 候选：抓取 N 条 → … → 本轮送模型 M 条`；
2. 若 M=0，应看到 `[SKIP] NO_NEW_ARTICLE` 且**没有任何** `[AI] provider=` 行；
3. 正常一轮应只有 **1 行** `[AI] provider=deepseek ... status=OK`；
4. 日志中不应出现 `gemini` 字样（Gemini 已关闭）；
5. `state.json` 应出现 `ai_stats` 与（若失败）`circuit_breakers` 字段。

---

## 六、每日调用次数统计

统计写在 `state.json` 的 `ai_stats`，按北京时间自然日滚动，跨天自动清零：

```json
"ai_stats": {
  "date": "2026-10-31",
  "calls": 1,
  "errors": 0,
  "input_tokens": 1200,
  "output_tokens": 500,
  "by_provider": { "deepseek": { "calls": 1, "errors": 0 } }
}
```

每次运行结束会打印一行：

```
[INFO] 本轮 AI 统计：{'date': '2026-10-31', 'calls': 1, 'errors': 0, ...}
```

也可在 `diagnose` 模式下直接看：

```
· AI 统计（2026-10-31）：调用 1 次 / 错误 0 次 / tokens 1200→500
    · deepseek: 调用 1 次，错误 0 次
· 熔断：无
```

**V1.3 起没有任何每日额度上限。** 原来的 `AI_MAX_CALLS_PER_DAY=60`、
`AI_MAX_TOKENS_PER_DAY=150000` 已彻底删除：它们只是记账数字，却会在达到阈值时
一次 HTTP 都不发、直接返回 `BUDGET_EXCEEDED`，然后被 bot.py 判成 `MODEL_FAILED`
→ `exit 1`，生产上表现为「跑满一轮就锁死到次日零点」。
Token 与调用次数现在只用于监控（`AIRouter.usage_snapshot()`），不参与停止判断。

---

## 七、配置项

| 变量 | 默认 | 说明 |
|---|---|---|
| `AI_PRIMARY_PROVIDER` | `deepseek` | 主模型 |
| `AI_GEMINI_ENABLED` | `false` | 关闭时不构造 client、零请求 |
| `AI_OPENAI_ENABLED` | `false` | 未接入 |
| `AI_MAX_RETRIES` | `2` | 单次调用最大重试次数 |
| `AI_RETRY_BASE_DELAY` | `2` | 退避基数（秒），指数增长 |
| `AI_MAX_ARTICLES_PER_RUN` | `25` | 每轮送模型的文章上限 |
| `AI_MAX_INPUT_CHARS` | `20000` | 素材字符预算 |
| `AI_QUOTA_COOLDOWN_MIN` | `120` | DeepSeek 配额熔断时长 |
| `AI_GEMINI_QUOTA_COOLDOWN_MIN` | `360` | Gemini 配额熔断时长 |
| `AI_AUTH_COOLDOWN_MIN` | `1440` | 鉴权失败熔断时长 |

---

## 八、未解决问题

1. **没有真实 Actions 运行记录**，也没有真实 API Key 联调；DeepSeek 真实响应的
   `usage` 字段、错误文案措辞可能与 mock 有差异，需要上线后观察一轮。
2. **证据相关性用的是标题词重叠度**（Jaccard 与包含度取大），属于启发式：
   同一事件的不同角度报道可能被判成两个来源（偏松），改写标题的转载可能被判成单源（偏严）。
3. **DeepSeek 无法提供真实 grounding**，所谓「已核验」本质是「我们自己抓到了多篇独立报道」，
   不等于事实为真。程序只能保证「不是模型凭空编的」。
4. **熔断与预算状态依赖 state.json 回写**；若 git push 失败，熔断状态会丢，
   下一轮可能重复请求一次。已有 `State Push Failed` 告警，但无法完全避免。
5. **Gemini 路径未实测**（默认关闭），其 `grounding_chunks` 结构与异常分类只在单测中验证过。
6. `max_tokens` 只是请求参数，DeepSeek 仍可能输出超长文本；目前靠 Schema 的字段长度下限
   兜底，没有做输出后截断。
