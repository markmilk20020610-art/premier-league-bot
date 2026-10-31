# V1.2 第三批执行报告（任务 8–11）

范围：Telegram 投递安全与恢复。
原则：增量修改；不删测试、不放宽断言；不引入付费服务；不输出真实密钥。

测试结果（真实执行，非声称）：
- `python tests/run_all.py` → **183 用例，失败 0，错误 0，耗时 28.0s**
- 本批新增 `tests/test_delivery_v12.py` → **28 用例**
- 基线 155 → 183（+28），原有 155 项全部保留并通过
- `python scripts/smoke_run.py` → 通过（`投递事件 [('COMMITTED', 9001)]`）
- `python scripts/smoke_run.py --quota` → 通过

---

## 任务 8｜修复 Token 日志泄露

### 问题定位
`_post_telegram_once()` 在异常分支把 requests 的异常原文直接塞进返回值：

```python
except Exception as e:
    return False, True, f"请求异常 {type(e).__name__}: {e}", None
```

这条字符串随后流向四个地方，全部可被仓库协作者看到：

1. `print(f"[ERROR] Telegram 分块 … 发送失败：{reason}")` → stdout / Actions 日志
2. `::error title=Telegram Push Failed::…| {last_reason}` → Actions 页面顶部注解
3. `log_telemetry(...)` → `run_telemetry.log`
4. `store.mark_failed(event, send_err)` → `state.json` 的 `last_error`

而 requests 的异常原文长这样：
`ConnectionError: failed request https://api.telegram.org/bot123456:AAH.../sendMessage`
—— 也就是完整 Bot Token。

### 修复
新增 `redact.py`，单一职责只做脱敏：

| 形态 | 处理 |
|---|---|
| `/bot<token>`（Bot API URL） | `/bot<redacted>` |
| 裸 Telegram token（`数字:长串`） | `<redacted>` |
| `sk-…` | `sk-<redacted>` |
| `Bearer xxx` | `Bearer <redacted>` |
| `?token= / &api_key= / &access_token=` | 值替换为 `<redacted>` |

接入点：
- `_post_telegram_once()` 的异常分支与响应体
- `log_telemetry()` 整体脱敏后才落盘（遥测里带 `last_error`）
- `state_store.mark_failed()` / `mark_uncertain()` / `record_chunks()` 对 `last_error` 二次脱敏（纵深防御，防止绕过 bot.py 直接写状态）

脱敏保留诊断信息，只抹掉凭证：

```
原始: ConnectionError: failed request https://api.telegram.org/botTEST_TOKEN/sendMessage
脱敏: ConnectionError: failed request https://api.telegram.org/bot<redacted>/sendMessage
```

### 验收测试（`TestTask8TokenRedaction`，6 项）
- 注入 `ConnectionError("failed request https://api.telegram.org/botTEST_TOKEN/sendMessage")`，
  断言 **stdout / stderr / 返回值** 均不含 `TEST_TOKEN` 与 `botTEST_TOKEN`
- 断言 `run_telemetry.log` 不含 Token，但遥测条目照常写入
- 断言 `state.json` 的 `last_error` 不含 Token
- 断言脱敏后仍保留「分块 1/3」「第 2 次失败」「ConnectionError」等定位信息
- 断言 `sk-` / `Bearer` / `?token=` 三种形态均被抹掉
- 端到端：`run_scan_mode` 走真实推送路径（requests 抛带 Token 的异常）后，
  扫描 stdout、`run_telemetry.log`、`state.json` 全文，均无 `TEST_TOKEN`

---

## 任务 9｜明确投递结果

### 问题定位
旧实现只有一个 `retryable: bool`，把所有网络异常混成两类，于是出现两种反向错误：

- **读超时**（请求已发出、只是没等到回执）被当成「可重试」→ 重发 → 用户收到两条一样的推送
- `apply_recovery()` 把卡在 `PENDING_DELIVERY` 的事件直接标成 `DELIVERY_FAILED`
  → 等于把「可能已送达」谎报成「确定没送达」

### 修复
新增两个状态（`state_store.py`）：

```
RETRYABLE_FAILURE    确定没送达（429 / 5xx / 连接失败），按退避重试
DELIVERY_UNCERTAIN   请求已发出但结果未知（读超时），不自动重发
```

状态机（`COMMITTED` 仍是唯一终态）：

```
DISCOVERED        → VERIFIED | DELIVERY_FAILED
VERIFIED          → PENDING_DELIVERY | DELIVERY_FAILED
PENDING_DELIVERY  → DELIVERED | RETRYABLE_FAILURE | DELIVERY_UNCERTAIN | DELIVERY_FAILED
RETRYABLE_FAILURE → PENDING_DELIVERY | DELIVERY_UNCERTAIN | DELIVERY_FAILED
DELIVERY_UNCERTAIN→ DELIVERED | DELIVERY_FAILED      # 只等确定性结论，不自动重投
DELIVERY_FAILED   → PENDING_DELIVERY | DELIVERED
DELIVERED         → COMMITTED
COMMITTED         → ∅
```

失败分类（`bot._post_telegram_once`）：

| 情况 | 结局 | 事件状态 |
|---|---|---|
| HTTP 200 + `{"ok": true}` | `TG_OK` | `DELIVERED` |
| 400 / 401 / 403 / 200+`ok:false` | `TG_DETERMINISTIC` | `DELIVERY_FAILED` |
| 429 / 5xx | `TG_RETRYABLE` | `RETRYABLE_FAILURE` |
| `ConnectTimeout` | `TG_RETRYABLE` | `RETRYABLE_FAILURE` |
| `ReadTimeout` / 其他 `Timeout` | `TG_UNCERTAIN` | `DELIVERY_UNCERTAIN` |
| 凭证缺失 | — | `DELIVERY_FAILED`（重试一万次也不会变） |

退避：`RETRY_BACKOFF_MINUTES = (15, 60, 180)`，按 `attempts` 取档，写入 `next_retry_at`。
`apply_recovery()` 对卡住的 `PENDING_DELIVERY` 改为标 `DELIVERY_UNCERTAIN`。

### 一处既有测试的调整（如实说明）
`tests/test_state_store.py::test_recovery_uncertain_pending` 原断言 `DELIVERY_FAILED`，
与任务 9 的新规格直接冲突（该断言本身就在描述被修复的那个 bug）。已改为断言
`DELIVERY_UNCERTAIN`，并**加强**了断言：新增「不得被当作可重投」「必须出现在独立恢复队列」
两条。断言强度没有下降，只是指向了正确状态。

### 验收测试（`TestTask9DeliveryOutcome`，8 项）
500 → `RETRYABLE_FAILURE`（含 `next_retry_at`）｜403 → `DELIVERY_FAILED`（无 `next_retry_at`）｜
`ReadTimeout` → `DELIVERY_UNCERTAIN`｜`ConnectTimeout` → `RETRYABLE_FAILURE`｜
结果未知进独立恢复队列且 `recovery_actions` 给出 `UNCERTAIN_REPORT`｜
卡住的 `PENDING_DELIVERY` → `DELIVERY_UNCERTAIN`｜退避档位随 `attempts` 增长｜
凭证缺失判为确定性失败｜启动时把结果未知队列显式打出来。

---

## 任务 10｜修复失败新闻永久去重

### 问题定位
`_prepare_ai_candidates()` 无条件把所有事件指纹塞进 `known_fp`：

```python
known_fp = {e.get("event_fingerprint") for e in state["events"].values() if ...}
```

推送失败的事件也留下了指纹，于是「失败」被当成「已经发过」。
Telegram 500 一次，这条新闻就永久沉默，只能人工去 `state.json` 删记录才能救回来。

### 修复
改为按投递状态判断（`state_store.blocks_rediscovery()`）：

| 状态 | 是否阻止重新入池 | 理由 |
|---|---|---|
| `COMMITTED` / `DELIVERED` | 阻止 | 真的发过了 |
| `PENDING_DELIVERY` | 阻止 | 正在发，防并发重发 |
| `DELIVERY_UNCERTAIN` | 阻止 | 可能已送达，重发会重复 |
| `RETRYABLE_FAILURE` | 到 `next_retry_at` 后放行 | 失败的要重新排队 |
| `DELIVERY_FAILED` | `attempts < 3` 放行，之后放弃 | 凭证修好后仍值得发，但不能无限刷 |

`candidate_stats` 新增三个可观测字段：`revived_retry`（本轮复活几条失败新闻）、
`waiting_retry`（还在退避期）、`blocked_uncertain`（结果未知被拦下）。

`_prune_events()` 同步加固：`RETRYABLE_FAILURE` / `DELIVERY_UNCERTAIN` / `PENDING_DELIVERY`
的事件**不允许被裁剪**，否则等人工确认的那条会随第 201 条新闻一起静默消失。

### 验收测试（`TestTask10FailedNewsRequeue`，6 项 + 端到端）
覆盖任务书要求的五种情形：
1. Telegram 500 → `RETRYABLE_FAILURE`，下一轮候选池重新出现（`revived_retry=1`）
2. Telegram 403 → `DELIVERY_FAILED`，超过 3 次才彻底放弃
3. 超时 → `DELIVERY_UNCERTAIN`
4. 超时后实际已收到 → 下一轮被拦下（`blocked_uncertain=1`），不重发
5. 下一轮恢复 → 端到端：第一轮 500 记 `RETRYABLE_FAILURE`，把 `next_retry_at` 调到过去，
   第二轮真的把同一条新闻发出去并落到 `COMMITTED`（`message_id=555`）

---

## 任务 11｜增加分段消息投递记录

### 问题定位
旧实现只有整体一个 `success` 布尔和 `message_ids` 列表：
- 第 1 段成功、第 2 段失败时，第 1 段的 `message_id` 直接丢失
- 下一轮重试必然整条重发，第 1 段内容重复进群
- 日志只写「推送失败」，看不出「已经发出去几段」

### 修复
每段一条独立记录，随事件持久化在 `state["events"][id]["chunks"]`：

```
chunk_index / chunk_total / chunk_hash / delivery_status /
telegram_message_id / attempts / last_error
```

`delivery_status ∈ {DELIVERED, RETRYABLE_FAILURE, DELIVERY_UNCERTAIN, DELIVERY_FAILED, NOT_ATTEMPTED}`

规则：
- 已 `DELIVERED` 且 `chunk_hash` 一致的段 → **跳过不重发**，其 `message_id` 仍计入返回值
- `record_chunks()` 不允许把已送达段覆盖成未送达
- 某段失败后不再发后续段，但剩余段必须留 `NOT_ATTEMPTED` 记录，否则汇总里看不出「还差几段」
- 部分成功 → `send_telegram_ex` 返回 `False`，并打
  `::warning title=Partial Delivery::`，明确列出已送达段号与 `message_id`

**跨轮续传的坑**：卡片是模型每轮重新生成的，文字会有细微差异，
分段哈希对不上就只能整条重发。因此事件在首次投递前就把原文存进 `pending_text`，
失败后保留，下一轮**沿用原文续传**；送达/确定失败/结果未知时清空。
进程在发送中途被杀也能凭 `pending_text` + `chunks` 原样续传。

### 验收测试（`TestTask11ChunkRecords`，7 项）
长消息确实被切成多段｜部分成功（第 1 段 OK、第 2 段 400）→ 返回 `False`、
`chunks[0]=DELIVERED(message_id=11)`、`chunks[1]=FAILED`、其余 `NOT_ATTEMPTED`、
`delivery_summary.partial=True`、事件落 `DELIVERY_FAILED`｜
已送达段不重发（只补发剩余段，`post` 调用次数 = 总段数 − 1）｜
分段布局变化时禁用续传｜`record_chunks` 不降级｜
全部送达才落 `DELIVERED` 并清空 `pending_text`｜
下一轮卡片内容变化时仍沿用原文续传，不产生重复段。

---

## 改动文件

| 文件 | 改动 |
|---|---|
| `redact.py` | **新增**。凭证脱敏（Bot Token / sk- / Bearer / 查询串） |
| `state_store.py` | 新增 `RETRYABLE_FAILURE` / `DELIVERY_UNCERTAIN`；`_TRANSITIONS` 扩展；`mark_retryable` / `mark_uncertain` / `retry_due` / `blocks_rediscovery`；分段记录 `normalize_chunks` / `record_chunks` / `delivery_summary` / `uncertain_queue`；`recovery_actions` 增加 `RETRY` / `WAIT` / `UNCERTAIN_REPORT`；`_prune_events` 保护未完结事件；`schema_version` 2→3 |
| `bot.py` | `_post_telegram_once` 结局分类 + 脱敏；`send_telegram_ex` 分段记账与续传；新增 `_deliver_card` / `_delivery_failure_status` / `_chunk_hash`；`_prepare_ai_candidates` 按状态去重；`log_telemetry` 脱敏；启动时报告结果未知队列 |
| `tests/_stubs.py` | requests 异常层次改为真实结构（`ConnectTimeout` / `ReadTimeout` / `ConnectionError`），否则分类逻辑测不出来 |
| `tests/test_pipeline.py`、`tests/test_ai_integration.py` | 桩函数签名加 `*args, **kwargs`（断言未改） |
| `tests/test_state_store.py` | 上述一处断言指向新状态（并加强） |
| `tests/test_delivery_v12.py` | **新增**，28 用例 |
| `tests/run_all.py` | 注册新测试模块 |

## 未做（留给后续批次）

- 投递失败时扫描 Job 仍返回 0（退出码虚报）→ 第五批任务 17
- 早报路径没有黑标闸门 → 待裁决
- 真实 GitHub Actions 运行验证：本批只在本地跑通 183 项测试与两个冒烟场景，
  尚未有线上运行记录，不作线上验证声明。
