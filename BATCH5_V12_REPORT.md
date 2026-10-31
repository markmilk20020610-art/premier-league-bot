# V1.2 第五批执行报告（任务 14–17）

范围：事件级去重、真实 API 请求统计、429 限流、扫描 Job 退出码。
原则：增量修改；不删测试、不放宽断言；不引入付费服务；不输出真实密钥。

测试结果（真实执行，非声称）：
- `python tests/run_all.py` → **239 用例，失败 0，错误 0，耗时 32.7s**
- 本批新增 `tests/test_batch5_v12.py` → **33 用例**
- 基线 206 → 239（+33），原有 206 项全部保留并通过
- `python scripts/smoke_run.py` → 通过（`exit_code: 0`，`requests: 1`）
- `python scripts/smoke_run.py --quota` → 通过（退出码 0，自愈型状态）

---

## 任务 14｜事件级去重

### 问题定位
`event_id_for()` 用 `evidence_url` 哈希生成事件 ID。同一件新闻被 BBC / Sky / 卫报
各报一次，就是三个不同的 `event_id`、三条 state 记录，运气不好还会推三次。
`processed_urls` 只能挡住「同一篇稿子」，挡不住「同一件事的不同报道」。

### 修复
复用证据引擎的 `same_event()`（主体 / 事件类型 / 人物 / 关键数字 / 标题相关度五维），
把「同一件事」的判定统一到一处，不再维护第二套松紧不一的标准。

**三层接入：**

1. **候选池**（`_prepare_ai_candidates`）：与已投递/在途事件同事件的候选直接剔除，
   不送模型（省 token），计入 `dropped_same_event`。
2. **推送前**（`run_scan_mode`）：模型选中的证据文章再复核一次，
   命中则 `_skip("EVENT_DEDUPED")` 并注明是哪个 `event_id` 已推过。
3. **记录层**（`record_event`）：同一件事若已有未完结记录（可重试失败 / 确定性失败），
   复用其 `event_id`，不让它在 state 里长出第二条。

**两个必要的加固：**

- **中文标题比不了英文标题**：事件里存的 `title` 是模型改写的中文事件标题，
  而候选是英文报道标题，直接比对相似度必然是 0。因此新增 `article_title` 字段，
  由 `_finalize_result` 从命中的证据记录里取出英文原始标题（`evidence_title`），
  事件级去重一律用它。
- **比分不同 ≠ 同一场比赛**：证据引擎的 `_tokens` 会剥掉数字，
  「Arsenal beat Chelsea 2-1」和「Arsenal beat Chelsea 3-0」相似度是 1.0。
  对「官方确认」无妨（还有事实对齐兜底），但事件级去重是**会拦截推送**的动作，
  所以本地加了一道 `_number_conflict()`：两边都有数字且完全不相交 → 判为不同事件。
  这比改证据引擎更安全——那个函数的语义已经被前几批的测试锁死了。

`BLOCKING_DELIVERY_STATUSES = {COMMITTED, DELIVERED, PENDING_DELIVERY, DELIVERY_UNCERTAIN}`
——只有「真的发过 / 正在发 / 结果未知」才拦。`RETRYABLE_FAILURE` 不拦，
否则失败的新闻又被永久去重（那是第三批刚修好的问题）。

比对窗口 7 天（`EVENT_DEDUPE_WINDOW_HOURS`），避免 O(n) 无限膨胀。

### 验收测试（`TestTask14EventLevelDedupe` 9 项 + `TestTask14PipelineDedupe` 1 项）
同一件事换媒体能认出｜不同俱乐部不合并｜不同动作不合并｜**比分不同不合并**｜
比分相同仍合并｜候选池剔除计数正确｜可重试失败的事件不拦候选｜
`record_event` 复用 event_id 只留一条记录｜`evidence_title` 正确提取｜
端到端：已推过的事件换家媒体再抓到时不再推送

---

## 任务 15｜真实 API 请求统计

### 问题定位
`stats["calls"]` 每次 `_record()` 加一，而 `_record()` 是按「逻辑调用」记的。
DeepSeek 一次 `analyze_news` 在 429/5xx 下可能打 3 次 HTTP，统计里只记 1 次，
`health_check` 的请求更是完全不记。结果是「今天调了几次」和服务商用量面板永远对不上。

### 修复
- `CallResult` 新增 `http_requests`（这一次逻辑调用实际打了几次 HTTP）和 `retry_after`。
- `DeepSeekProvider` 在重试循环里累加真实请求数；`health_check` 也计入。
- `GeminiProvider` 用 `_generate()` 内层 + `generate_content()` 外层计数，
  避免在 7 个提前返回点逐个打补丁。
- 统计结构新增 `requests`：`stats["requests"]` 与 `by_provider[name]["requests"]`。
  旧 state 缺这个字段时 `setdefault` 自动补 0（有测试覆盖）。
- 日志行改为 `[AI] provider=... http=N tokens=...`，一眼能看出重试了几次。
- `stats_snapshot()` 暴露 `requests`。

`calls`（逻辑调用）与 `requests`（真实请求）并存，不互相替代：
预算判断仍用 `calls`，账单核对用 `requests`。

### 验收测试（`TestTask15RequestStats`，4 项）
500 重试后 `http_requests=2`、`calls=1`、`requests=2`｜单次成功 `requests=1`｜
旧 state 无 `requests` 字段自动升级｜provider 禁用时 `http_requests=0` 且零请求

---

## 任务 16｜429 限流处理

### 问题定位
429 被归进 `RETRYABLE`，一律用 2/4/8 秒指数退避。
服务端说「120 秒后再来」，程序 2 秒后就再敲一次门——
除了把限流窗口越拉越长，没有任何作用。而且每次重试都会消耗一次配额。

### 修复
1. **解析 `Retry-After`**（`parse_retry_after`）：支持秒数与 HTTP 日期两种格式，
   解析失败返回 None 交给指数退避兜底。
2. **按 `Retry-After` 等待**：`_retry_wait()` 优先用服务端给的秒数。
3. **等不起就 fail fast**：等待时间超过 `AI_MAX_RETRY_WAIT`（默认 60s）时**不再重试**，
   直接返回 `RATE_LIMITED` 交给下一轮。硬等 600 秒只会让 Actions 这一步超时。
4. **熔断冷却**：router 收到 `RATE_LIMITED` 时按 `Retry-After` 折算冷却分钟数
   （无则用 `AI_RATE_LIMIT_COOLDOWN_MIN`，默认 10 分钟），
   冷却期内不再发请求，并打 `::warning title=AI Rate Limited::`。

日志会明确写出依据：

```
[WARN] DeepSeek RATE_LIMITED，5s 后重试（1/2，Retry-After）
[WARN] DeepSeek RATE_LIMITED，服务端要求等待 600s 超过上限 60s，放弃重试（由下一轮处理）
::warning title=AI Rate Limited::deepseek 429 限流（Retry-After=180.0），熔断 3 分钟
```

### 验收测试（`TestTask16RateLimit`，8 项）
`Retry-After` 秒数解析｜HTTP 日期解析｜垃圾输入返回 None｜
**429 按 `Retry-After` 等待（断言 `sleep` 参数恰为 5.0）**｜
**600 秒要求时零等待、零重试、只打 1 次请求**｜
无 `Retry-After` 时回退指数退避｜429 触发熔断且记录 `error_type`｜
熔断期间不再发请求

---

## 任务 17｜扫描 Job 退出码

### 问题定位
`run_scan_mode()` 在三条路径上把失败报成成功：
- 所有源抓取失败 → 走到 `NO_NEW_ARTICLE` → `return 0`
- 投递失败 → `push_success=False` 但最终 `return 0`
- 模型全失败 → `_skip("MODEL_FAILED")` → `return 0`

Actions 上是一个漂亮的绿勾，而站点、网络、凭证或 Telegram 已经坏了——
这种「静默停摆」可以持续好几天没人发现。

### 修复

新增 `_source_health()`，把各源状态分成三类：

| 类别 | 判定 |
|---|---|
| ok | `200` / `FALLBACK_OK(...)` |
| degraded | `200_EMPTY(0 items)` / `FALLBACK_EMPTY(...)`（源活着，只是没新闻） |
| failed | 403/404/超时/解析失败/`MISSING` |

退出码规则：

| 情形 | 退出码 | 注解 |
|---|---|---|
| 所有源都 failed（`ok == 0` 且 `total > 0`） | **1** | `::error title=All Sources Failed::` |
| 部分源失败 | 0 | `::warning title=Partial Source Failure::` |
| 确实没新文章 / 无有效新闻 / 证据不足 / 时效拦截 | 0 | 正常空转 |
| 投递失败 | **1** | `::error title=Delivery Failed::` |
| 状态写失败（发送前/送达后） | 1 | 原有逻辑 |
| 模型失败（鉴权/参数/解析/无 provider） | **1** | `::error title=AI Unavailable::` |
| 模型失败但属自愈型（配额耗尽/429/熔断冷却） | 0 | `::notice title=AI Temporarily Unavailable::` |
| 全部成功 | 0 | — |

**为什么配额耗尽不判失败**：这是既有测试
`test_quota_exhausted_pauses_and_keeps_candidates` 已经写明的产品决策——
「配额耗尽属于可恢复状态，不应让 Job 红叉」。它已经用
`::warning title=AI Quota Exhausted::` + 熔断报过一次，每 30 分钟再红一次
只会把真正需要人干预的故障（鉴权失败、参数错误、源全挂）淹掉。
因此把自愈型状态单独列出（`SELF_HEALING_AI_STATUSES`），其余仍判 1。

`log_telemetry` 新增 `source_health` 与 `exit_code` 字段，事后可追溯。

工作流无需改动：`python bot.py` 直接继承退出码，
state 回写步骤已是 `if: always()`，失败时新闻池与投递状态照样保住。

### 验收测试（`TestTask17ExitCodes`，11 项）
源全挂 → 1｜部分源失败 → 0｜空 `source_status` 不误判 → 0｜没新文章 → 0｜
投递失败 → 1｜配额耗尽 → 0｜429 → 0｜鉴权失败 → 1｜解析失败 → 1｜
成功 → 0｜`_source_health` 七种状态分类正确

---

## 改动文件

| 文件 | 改动 |
|---|---|
| `ai_provider.py` | `CallResult` 新增 `http_requests` / `retry_after`；`parse_retry_after()`；DeepSeek 请求计数与 `Retry-After` 优先重试；Gemini 请求计数；统计新增 `requests`；429 触发熔断 |
| `state_store.py` | `record_event()` 支持指定 `event_id` 与 `article_title` |
| `bot.py` | `_event_probe` / `_number_conflict` / `_find_same_event` / `BLOCKING_DELIVERY_STATUSES`；候选池与推送前事件级去重；`_finalize_result` 提取 `evidence_title`；`_source_health`；退出码逻辑；`SELF_HEALING_AI_STATUSES` |
| `tests/test_batch5_v12.py` | **新增**，33 用例 |
| `tests/run_all.py` | 注册新测试模块 |

## 未做（留给后续批次）

- 第六批维护：`__pycache__` 清理、版本锁定、Action SHA 固定、
  禁止 `merge -X ours` 静默覆盖、移除固定主帅映射、心跳 Watchdog
- 早报路径没有黑标闸门 → 仍待裁决
- 真实 GitHub Actions 运行验证：本批只在本地跑通 239 项测试与两个冒烟场景，
  尚未有线上运行记录，不作线上验证声明。
