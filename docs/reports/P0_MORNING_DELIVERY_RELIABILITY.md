# P0 验收报告：早报分段交付可靠性

> 分支 `p0-morning-chunk-ledger` ｜ 基线 `85a4a23` ｜ 阶段：V2.2 第二阶段 P0
> 状态：**已完成，等待批准后合并进 main**（未进入 P1）

---

## 一、结论先行

| # | 用户要求 | 结论 | 证据 |
|---|---|---|---|
| 1 | 保留原有早报内容和消息格式 | ✅ 未改 | `_build_summary_card` / `_send_low_info_morning` 的卡片文本逐字未动，只换了发送出口 |
| 2 | 使用可靠的分段发送状态 | ✅ 已实现 | 新增 `state["morning_delivery"]` 账本，逐段落 `chunk_index` + `chunk_hash` + `telegram_message_id` |
| 3 | 全部分段成功后才提交完成状态 | ✅ 已实现 | `_lock_morning_sent()` 只在 `summary["all_delivered"]` 为真时调用 |
| 4 | 部分失败可恢复 | ✅ 已验证 | 5 个故障场景端到端演练，第 2 轮均能续传成功 |
| 5 | 已成功分段不得重发 | ✅ 已验证 | 续传日志实测输出「分块 1/3 上次已送达（message_id=9001），本次跳过不重发」 |
| 6 | 不得因失败错误锁定当天 | ✅ 已验证 | 失败时 `last_morning_report_date` 保持空值，4 类失败均覆盖 |
| 7 | 保留日期锁与跨运行持久化 | ✅ 未改 | `MORNING_ALREADY_SENT` 入口检查原样保留，账本落盘走 `save_state` |
| 8 | 测试中断 / 429 / 超时 / 部分成功 | ✅ 全覆盖 | 33 例新增测试，其中 5 类故障 + 3 类中断 |

**测试结果：663 例全绿（630 基线 + 33 新增），0 失败 0 错误。**

---

## 二、修的是什么缺陷

改动前，早报走的是**无账本路径**：

```python
push_success = send_telegram(card)   # bot.py:2287（旧）
```

`send_telegram()` 内部调 `send_telegram_ex(text)`，`chunk_records=None`。分段能力本身存在，但**分段结果从不落盘**，于是产生三个连锁问题：

1. **一张早报切成 3 段、第 2 段失败** → 系统只知道「整体失败」，不知道第 1 段已经在群里了。下一轮从第 1 段重来，用户收到重复内容。
2. **失败不写日期锁**（这点原本是对的）→ 每次触发都从第 1 段重来，永远发不完就永远重发。
3. **进程在「发完最后一段」和「写日期锁」之间被杀** → 同样整条重发。

对比之下，突发新闻走的 `_deliver_card()` → `send_telegram_ex(chunk_records, report)` **是有账本的**。早报是唯一一条漏掉的路径。

---

## 三、改动清单

### 3.1 `bot.py`（+192 / −24）

新增常量：

```python
MORNING_DELIVERY_KEY = "morning_delivery"
```

> 为什么不复用 `state["events"]`：events 是按 `evidence_url` 建键的**新闻去重表**，超过 200 条会被 `_prune_events` 裁剪。早报不是新闻事件，混进去既污染去重判定，又可能被裁剪掉导致「已送达段落」记忆丢失。

新增三个函数：

| 函数 | 职责 |
|---|---|
| `_morning_delivery_record(state, today_str)` | 取当日账本；**跨天作废重建**（昨天的段数/哈希与今天无关，硬套必然整条重发） |
| `_deliver_morning(state, card, today_str, force=False)` | 先落盘再发送 → `send_telegram_ex(card, prior_chunks, report)` → `store.record_chunks()` → 再落盘 |
| `_lock_morning_sent(state, today_str, what)` | 全部送达后才写日期锁；写盘失败返回 `False` 并报 `::error` |

改造两处调用点：`run_morning_mode()` 与 `_send_low_info_morning()`。

**三条不变式**（写在代码注释里，改这个函数前必须先读）：

- 已确认送达的段永不重发（靠 `chunk_index` + `chunk_hash` 双重匹配）；
- 只有全部分段送达，才允许写 `last_morning_report_date`；
- 卡片原文必须跨运行沿用（`pending_text`），否则模型重写一遍导致哈希变化，断点续传直接失效。

### 3.2 `scripts/merge_state.py`（+53）

新增 `_merge_morning_delivery()` 并接入 `merge()`。

必要性：`merge_state.py` 的合并语义是 `out = dict(local)` 起手、**只显式合并已知键**——不认识的键会被远端覆盖丢失。不加这段，CI 里 state.json 冲突一次，账本就没了。

合并原则：**宁可少发一段，也不能重复发一段**。

- 日期不同 → 取更晚那天
- 同一天但原文不同 → 整份取「已送达段更多」的那份
- 同一天且原文一致 → 分段取并集，计数取大，时间取晚

### 3.3 `tests/test_morning_delivery.py`（新建，533 行，33 例）

| 测试类 | 例数 | 覆盖 |
|---|---|---|
| `TestAllDelivered` | 3 | 全成功、锁只在全送达后写、message_id 落盘 |
| `TestPartialFailure` | 4 | 部分失败不锁、状态为 PARTIALLY_DELIVERED、续传只补剩余段 |
| `TestFailureKinds` | 5 | 429 / 超时 / 403 确定性失败 / UNCERTAIN / 凭证缺失 |
| `TestInterruption` | 3 | 发送前杀、发完最后一段但锁未写时杀、中途杀 |
| `TestTextContinuity` | 3 | 卡片重新生成后仍沿用旧原文、跨天账本作废 |
| `TestLockWriteFailure` | 1 | 锁写盘失败返回 False |
| `TestMorningModeIntegration` | 3 | `run_morning_mode` 端到端：部分失败不锁、全成功锁、重跑不重复 |
| `TestMorningUsesLedgerPath` | 2 | 静态守卫：不得退回 `send_telegram(card)` |
| `TestForceSummary` | 4 | `FORCE_SUMMARY` 强制补发必须真的发出去 |
| `TestMergeState` | 5 | 合并语义 |

### 3.4 `tests/run_all.py`（+1）

把 `tests.test_morning_delivery` 登记进 `MODULES` 白名单。

> 这是硬编码清单，不登记就永远不会被 CI 跑到——首轮跑完整套件时正是 630 例，新模块被静默忽略。

---

## 四、端到端演练实测

3 段卡片，注入故障后跑两轮（第 2 轮为恢复轮）：

| 场景 | 第 1 轮结果 | 状态 | 日期锁 | 第 2 轮 |
|---|---|---|---|---|
| A 三段全成功 | ok=True | `DELIVERED` | 已写 | 一段不发，日志「上次已送达，跳过不重发」 |
| B 第 2 段 Telegram 429 | ok=False | `PARTIALLY_DELIVERED` | 未写 | 只补第 2、3 段 |
| C 第 2 段网络超时 | ok=False | `PARTIALLY_DELIVERED` | 未写 | 只补第 2、3 段 |
| D 第 1 段 403 确定性失败 | ok=False | `DELIVERY_FAILED` | 未写 | 三段全补 |
| E 第 2 段结果不确定 | ok=False | `PARTIALLY_DELIVERED` | 未写 | 只补第 2、3 段 |

场景 A 的关键日志：

```
[INFO] 分块 1/3 上次已送达（message_id=9001），本次跳过不重发
[INFO] 分块 2/3 上次已送达（message_id=9002），本次跳过不重发
[INFO] 分块 3/3 上次已送达（message_id=9003），本次跳过不重发
```

---

## 五、过程中发现并修掉的两个问题

### 5.1 `FORCE_SUMMARY` 强制补发被账本挡住（行为回归）

`FORCE_SUMMARY=true` 的语义是「不管发过没发过，再来一份」。但账本上线后，强制补发会看到「3 段全 DELIVERED」→ 一段不发 → 返回成功 → 重写锁。结果是**记账显示已推送、群里什么都没有**，比原来的 bug 更难发现。

修法：`_deliver_morning(..., force=FORCE_SUMMARY)`，`force=True` 时清空当日账本整份重发。原则是——账本的意义是「别重发已在群里的段落」，人工指令的意义是「就要重发」，两者冲突时人的指令赢。

已加 4 例测试锁住，含一例走完整 `run_morning_mode`（日期锁已写 + `FORCE_SUMMARY=true` → 仍发出全部分段，且锁保持有效）。

### 5.2 `bot.py` 行尾被整份改写（差点污染 diff）

一次脚本改写把 `bot.py` 从 CRLF 写成了 LF，`git diff --numstat` 显示 2884/2740 全文件改动，review 完全没法看。已恢复为 CRLF，diff 回到 192 增 / 24 删。

仓库 `core.autocrlf=true`、无 `.gitattributes`，工作区必须保持 CRLF。

---

## 六、兼容性与影响面

### 6.1 旧 state.json 兼容性（实测）

用仓库现有 `state.json`（无 `morning_delivery` 键）验证：

| 检查项 | 结果 |
|---|---|
| `ensure_shape()` 是否强插新键 | 否，不引入 |
| `merge()` 两边都无该键 | 输出不含该键，且**原有 9 个键一个不丢** |
| 旧本地 + 新远端 | 账本正确保留远端那份 |
| 旧 state 上调用 `_morning_delivery_record` | 正常按需创建 |

设计选择：**不在 `ensure_shape` 里预置 `morning_delivery`**。老部署没发过早报就不该多一个空键；账本由 `_morning_delivery_record` 按需创建。

### 6.2 scan / focus 影响面

本次 diff 只触及 4 个 hunk：常量区、新增 3 个函数、`_send_low_info_morning`、`run_morning_mode`。

- `send_telegram()` / `send_telegram_ex()` / `_deliver_card()` **一字未改**
- 突发新闻路径（`_deliver_card` → `send_telegram_ex`）原样
- focus / scan 主流程原样
- 630 例基线测试全绿，即为回归证据

### 6.3 已知边界（沿用既有设计，P0 不改）

`DELIVERY_UNCERTAIN`（发出去了但读超时、不知道到底成没成）在 `store.delivery_summary()` 里归入 `pending`，会被重发。这意味着该段可能重复一次。

这是突发新闻路径的既有语义，不是 P0 引入的。取舍是「宁可重复一次，也不要丢一段」——早报丢段比重复更难被用户发现。如需改成「不确定就不重发」，属于独立议题，不在 P0 范围。

---

## 七、回滚方案

改动集中在 3 个文件，无数据库、无外部依赖、无 schema 迁移。

```bash
git revert <P0 merge commit>     # 或直接不合并本分支
```

回滚后 `state.json` 里残留的 `morning_delivery` 键是**惰性数据**：旧代码不认识它，`merge_state.py` 的 `dict(local)` 起手会原样保留，不会被读也不会报错。无需清理。

---

## 八、测试结果

```
$ python -m unittest tests.test_morning_delivery
Ran 33 tests in 0.812s
OK

$ python tests/run_all.py
Ran 663 tests in 49.285s
OK
============================================================
模块数 20 | 用例 663 | 失败 0 | 错误 0 | 耗时 49.3s
============================================================
```

---

## 九、等待批准

P0 已完成并通过全部验收项，**未进入生产集成**（分支未合并 main）。

下一步 P1（小红书内容包生产流程：`xhs_templates.py` / `xhs_state.py` / `xhs_pack.py` / `schema.validate_xhs_package()`）需批准后开始。
