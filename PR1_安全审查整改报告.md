# PR #1 安全审查整改报告

**任务**：越位情报局 PR #1（`feat/v1.0-review-privacy`）安全审查整改
**审查结论（整改前）**：暂不允许合并
**本次范围**：P0 / P1 / P2 三档本地修复 + 回归测试 + 审计
**报告时间**：2026-11-06

---

## 一、结论

三档缺陷全部修复，且**每一项都有「修复前必然失败、修复后必然通过」的双向证据**。

| 档位 | 缺陷 | 状态 | 关键证据 |
|---|---|---|---|
| P0 | 公开新闻池与审核状态的关联泄露 | 已修复 | 攻击脚本：修复前 `LEAK`，修复后 `SAFE` |
| P1 | claim 租约过期后的重复投递 | 已修复 | 修复前第二次调用**真的调用了 Telegram 发送**（`[1] != []`） |
| P2 | `approve_review` / `decline_review` fail-open | 已修复 | 修复前 15 项里 14 项破，修复后 15/15 |

**当前状态**：本地已提交，**未推送、未合并、未部署**。

---

## 二、操作边界遵守情况

| 约束 | 执行情况 |
|---|---|
| 只在 `D:\yuewei-intelligence-dev` 开发克隆中工作 | ✅ 全部改动均在该目录 |
| 不修改 `D:\premier-league-bot` 生产目录 | ✅ 未进入、未读写 |
| 不修改 recovery snapshots | ✅ 未触碰 |
| 不合并 PR、不部署 Cloudflare Worker | ✅ 未执行 `gh pr merge` / `wrangler deploy` |
| 不擅自 push | ✅ 未执行任何 `git push` |
| 不读取/输出/要求提供真实 Secret | ✅ 全程只用测试固定值（`TEST_TOKEN` / `svc_fake_key`） |

---

## 三、P0：公开新闻池与审核状态的关联泄露

### 3.1 缺陷与根因

`_merge_pool()` 在 AI 审核分类**之前**就把 RSS 标题、URL、描述写进公开
`state.json` 的 `news_pool`；而审核记录的公开键是：

```python
def event_id_for(evidence_url, title=""):
    key = (evidence_url or "").strip().lower()
    ...
    return _sha1(key)[:12]
```

输入是**已经公开**的新闻池 URL，函数是纯函数。于是任何拿到公开 state.json
的人都能自己算一遍：

```
新闻池里的 URL  →  sha1(clean_url)[:12]  →  在 state["events"] 里精确匹配到
                                            delivery_status = PENDING_REVIEW
```

泄露的不是内容，是「**哪一条**我们抓到了但压着没发」。这比 payload 泄露更
隐蔽：不需要看到任何一个字，只需要「这一条被挂起了」这个事实，就等于公开
宣布「我们手上有未核实的独家」。

`public_review_violations()` 当时只扫 `state["events"]`，对 `news_pool` /
`processed_urls` / `s_overflow` 完全失明，所以这个泄露一路畅通无阻。

### 3.2 为什么不采用「把审核事件的新闻池条目删掉」

这是最先想到的修法，评估后**否决**，理由写进了代码注释与测试：

1. 新闻池每轮都提交，URL 在**上一版 commit 里已经公开**，删掉只是让它
   「消失」——`git diff` 里凭空少一条，比哈希撞上还显眼，等于换个更响的方式
   宣布「这条被处理了」；
2. 删除会连带毁掉早报素材。

所以新闻池**一个条目都不删**。真正的修法是让公开 id 与 URL 之间不存在任何
可计算的映射。

### 3.3 修复方案

| # | 改动 | 位置 |
|---|---|---|
| 1 | 新增 `new_review_public_id()` → `"rv" + token_hex(8)`，纯随机 | `state_store.py` |
| 2 | `record_review_stub()` 的键改为随机值，**不再写 `event_id` 字段** | `state_store.py` |
| 3 | `REVIEW_STUB_ALLOWED` 移除 `"event_id"`（带它即越界） | `state_store.py` |
| 4 | `find_review_stub(state, review_ref)`：公开侧改按句柄定位 | `state_store.py` |
| 5 | `_enqueue_review()` 用随机公开键；同一 `review_ref` 只允许一条 stub | `bot.py` |
| 6 | `_find_review_rec()`：同时接受「公开键」与「句柄」 | `bot.py` |
| 7 | `_public_stub_of()` / `_mark_public_uncertain()` 优先按句柄找 | `bot.py` |
| 8 | `public_review_violations()` 扩展到 `news_pool` 等非 events 结构 | `state_store.py` |
| 9 | 新增关联检查 `_review_id_link_violations()` | `state_store.py` |
| 10 | `ensure_shape()` 迁移：历史派生键 → 随机键（`review_ref` 不动） | `state_store.py` |
| 11 | 审核分支日志/遥测不再打印 `event_id`（Actions 日志公开） | `bot.py` |
| 12 | 重复入队不再重发审核卡片（`_duplicate` 标记） | `bot.py` |

### 3.4 去重如何保住

去重**原本就不依赖新闻池**，现在也不依赖公开键：

- **事件级**：私有存储 `review` 表的 `ux_review_active` 唯一索引按
  `event_id`（派生值，只存在于私有 payload）去重，重复入队返回
  `duplicate: true` + **同一个** `review_ref`；
- **公开侧**：`_enqueue_review` 按 `review_ref` 命中已有 stub 就复用，
  不会再长第二条；
- `blocks_rediscovery()` 只看 `delivery_status`，`PENDING_REVIEW` 返回 `True`，
  与键无关。

### 3.5 修复前后公开 `state.json` 序列化对比

同一场景（太阳报「C 罗退出国家队」→ E4 + S → 入队），`save_state_atomic` 落盘后：

**修复前（HEAD `c8920c9`）**

```json
{
  "events": {
    "c509df10c201": {
      "attempts": 0,
      "delivery_status": "PENDING_REVIEW",
      "event_id": "c509df10c201",
      "review_ref": "5d23eeed898dab59",
      ...
    }
  }
}
```

攻击者计算：`sha1("https://www.thesun.co.uk/sport/football/ronaldo-portugal-retirement")[:12]`
= `c509df10c201` → **与公开键完全一致** → 精确命中。

**修复后（`625c9b6`）**

```json
{
  "events": {
    "rv239e5f6b63844281": {
      "attempts": 0,
      "delivery_status": "PENDING_REVIEW",
      "review_ref": "4002e8f261277805",
      ...
    }
  }
}
```

键为随机值，`event_id` 字段已消失。同样的计算得到 `c509df10c201`，
在公开 state 里**不存在**。

两次运行的 `news_pool` 完全相同（URL 都还在池里）：

```
POOL_LINKS = ["https://www.thesun.co.uk/sport/football/ronaldo-portugal-retirement"]
```

### 3.6 攻击脚本实测

同一个攻击脚本，分别在 HEAD 与修复后代码上运行：

| 代码版本 | 公开键 | 攻击者派生 id | 判定 |
|---|---|---|---|
| HEAD `c8920c9` | `c509df10c201` | `c509df10c201` | **LEAK（可精确匹配）** |
| 修复后 `625c9b6` | `rv239e5f6b63844281` | `c509df10c201` | **SAFE** |

---

## 四、P1：claim 租约过期后的重复投递风险

### 4.1 缺陷与根因

`SQL_CLAIM` 里有一句：

```sql
OR (status='claimed' AND claim_expires_at IS NOT NULL AND claim_expires_at < ?)
```

语义是「上一任超时了，换你上」。但投递不是可回滚的动作：

```
取到 claim → Telegram 发送成功 → 进程被杀 → 租约过期
                                        → 另一个 job 认领 → 重复公开推送
```

发送与 `finish` 回写不可能构成一个原子事务，所以「租约过期」只说明
**结果未知**，不说明**没发出去**。

### 4.2 修复方案

| # | 改动 | 位置 |
|---|---|---|
| 1 | `SQL_CLAIM` 去掉过期分支，只允许 `decided` / `failed` | `review_store.py` + `review_vault.js` |
| 2 | 新增 `SQL_REAP_EXPIRED`：过期 `claimed` → `needs_manual`（清 token、记 last_error） | 两侧同步 |
| 3 | `claim()` 先 reap 再 claim，同一事务内完成 | 两侧同步 |
| 4 | 新增异常 `ReviewNeedsManual`（与 `ReviewConflict` 语义分离） | `review_store.py` |
| 5 | 新增 `SQL_RELEASE` + `release(ref, operator_id, note)`：唯一放行通道 | 两侧同步 |
| 6 | `needs_manual` 再认领也返回 `needs_manual`（不退化成语义模糊的冲突） | 两侧同步 |
| 7 | 新增 DO 路由 `/review/<ref>/release` | `review_vault.js` |
| 8 | `execute_review_decision` 捕获 `ReviewNeedsManual` → `NEEDS_MANUAL` + 公开侧标 `DELIVERY_UNCERTAIN` | `bot.py` |
| 9 | `HttpReviewStore._http` 解析 409 响应体，保留 `needs_manual` 标记 | `review_store.py` |

**第 9 项是自查发现的隐藏缺陷**：`_http()` 在 HTTP 409 时**先抛
`ReviewConflict`**，`_call()` 的 `needs_manual` 映射根本没机会执行。
DO 用 409 表示冲突，而「租约过期」也是 409 —— 不改这里，P1 修复在生产上
等于没生效（本地全绿、线上照样重发）。已补 5 项 HTTP 路径测试锁住。

### 4.3 三态语义（确定性失败 vs 结果未知）

| 情形 | 状态 | 是否自动重发 |
|---|---|---|
| HTTP 400/401/403 等确定性失败 | `failed` | ✅ 可重新认领（重发不会重复） |
| 读超时 / 连接中断（结果未知） | `needs_manual` | ❌ 绝不自动重发 |
| 租约过期（上一任可能已发出） | `needs_manual` | ❌ 绝不自动重发 |
| 人工核对后显式放行 | `decided` | ✅ 可重新认领 |

### 4.4 复现证据

**Python 侧**（`tests/test_review_lease_expiry.py`，22 项）

用修复前的 `claim()` 实现跑同一组测试：

```
OLD_CODE run=22 failures=13 errors=1
```

其中最关键的一条：

```
FAIL: test_no_duplicate_send_on_second_attempt
AssertionError: Lists differ: [1] != []
```

→ **修复前，租约过期后的第二次调用真的又调了一次 Telegram 发送函数。**

**Worker Node 侧**（`worker.test.mjs` C-11a ~ C-11f，45 项断言）

用 HEAD 版 `review_vault.js` 跑最终版测试：

```
134/155 passed        （21 项失败）
FAIL C-11a 过期后不能重新认领            ← 期望=false 实际=true
FAIL C-11a 日志记录 LEASE_EXPIRED        ← ["ENQUEUED","DECIDED:approve","CLAIMED","CLAIMED"]
FAIL C-11f 重启后不能自动重新认领        ← 期望=false 实际=true
```

日志里出现**两条 `CLAIMED`** —— 正是「同一件事被发了两次」的直接痕迹。

修复后：

```
155/155 passed
```

### 4.5 要求逐条对照

| 要求 | 落实 |
|---|---|
| 明确区分确定性失败与发送结果未知 | `failed` vs `needs_manual`，异常类型 `ReviewConflict` vs `ReviewNeedsManual` |
| 不允许过期 `claimed` 直接自动重发 | `SQL_CLAIM` 删除该分支（Python + JS 逐句同步） |
| 过期认领进入 `needs_manual` | `SQL_REAP_EXPIRED`，与认领同一事务 |
| 必须由明确恢复流程确认后才能重投 | `release(ref, operator_id, note)`，缺 `operator_id` 直接拒 |
| 保留 `failed` 的合理重试 | `status IN ('decided','failed')` 原样保留 |

---

## 五、P2：`approve_review()` / `decline_review()` fail-closed 加固

### 5.1 缺陷

```python
if client is not None and review_ref:      # ← 两者都有才校验
    try:
        client.decide(review_ref, "approve", operator_id, note)
    except review_store.ReviewConflict as e:
        raise store.StateError("批准被拒：{}".format(e))

store.transition(rec, store.PENDING_DELIVERY)   # ← 无论如何都会执行
```

任意一个条件不成立，整段私有 `decide` 被静默跳过，公开状态照常前进。
更细一层：即使两者都在，也只捕获 `ReviewConflict`，HTTP 5xx / 连接中断 /
协议错误会直接穿透，调用方很容易在外面包一层 `try/except` 变成
「私有失败、公开成功」。

后果是**审核链条断在中间**：一条 E4 级未核实内容被放行，事后既查不到谁批的，
也查不到为什么批。

### 5.2 修复

抽出唯一实现点 `_decide_private_or_raise()`：

```python
if client is None:
    raise store.StateError("{}被拒：私有审核存储未配置…".format(label))
if not review_ref:
    raise store.StateError("{}被拒：审核记录缺少 review_ref…".format(label))
try:
    client.decide(review_ref, action, operator_id, note)
except review_store.ReviewConflict as e:
    raise store.StateError(...)
except review_store.ReviewStoreError as e:
    raise store.StateError(...)
except Exception as e:            # fail-closed 要求兜住一切
    raise store.StateError(...)
```

顺序固定：**先私有、后公开**。反过来的话，公开状态一旦前进，私有存储失败
就再也补不回来。

### 5.3 实际调用路径（已确认）

`approve_review` / `decline_review` 在 `bot.py` 生产代码中**没有任何调用点**：

- 生产审核动作走 `run_review_action_mode()` → `review_action()` →
  `execute_review_decision()`；
- 这两个函数目前只被 `scripts/verify_10_checks.py` 与测试调用。

**但仍是安全隐患**：任何人给它们接上调用点就会绕过 fail-closed。本次加固后
即使被接上也不会 fail-open。

### 5.4 测试

`tests/test_review_fail_closed.py`，15 项，覆盖：

- 存储缺失 → 拒绝（批准 / 拒绝各一条）
- `review_ref` 为空 → 拒绝
- 存储返回 `ReviewStoreError` → 拒绝
- 存储抛非预期异常（`RuntimeError` / `ValueError`）→ 拒绝
- 私有侧已被他人决过（`ReviewConflict`）→ 拒绝且不覆盖
- 拒绝时公开 state 序列化结果**逐字节相同**
- 拒绝后记录仍可被正常处理（不会卡死）
- 拒绝不牵连其它待审记录
- 正常路径不被堵死（批准后 `cred` 仍为 E4）

---

## 六、测试结果

### 6.1 Python（完整套件）

```
python tests/run_all.py
```

| 指标 | 数值 |
|---|---|
| 模块数 | 35 |
| 用例数 | **1048** |
| 失败 | **0** |
| 错误 | **0** |
| 跳过 | **0** |
| 耗时 | 80.8s |
| 退出码 | **0** |

> 对比整改前：981 用例 → 1048 用例（+67，全部为本次新增）。

### 6.2 Worker Node

```
node --version  →  v22.22.2
node worker.test.mjs
```

| 指标 | 数值 |
|---|---|
| 断言数 | **155** |
| 通过 | **155** |
| 失败 | **0** |
| 跳过 | **0** |
| 退出码 | **0** |

> 对比整改前：110 断言 → 155 断言（+45，C-11a ~ C-11f）。

**未使用任何 `skip` 冒充通过**：Python 跳过数 0，Node 侧无 skip 机制。

### 6.3 附加自检

```
python scripts/verify_10_checks.py  →  共 10 条，通过 10 条，失败 0 条
python scripts/scrub_public_state.py --check
  →  50 事件 / 0 审核记录 / 0 越界字段
```

真实数据文件在新守卫下：

```
state.json        violations = 0
focus_state.json  violations = 0
```

---

## 七、新增测试清单

| 文件 | 用例 | 针对 |
|---|---|---|
| `tests/test_news_pool_privacy.py` | 30 | P0（含 5 项端到端 `run_scan_mode`） |
| `tests/test_review_lease_expiry.py` | 22 | P1（含 5 项 HTTP 生产路径） |
| `tests/test_review_fail_closed.py` | 15 | P2 |
| `worker.test.mjs`（追加） | +45 断言 | P1（DO 侧，C-11a ~ C-11f） |

### P0 回归测试的关键断言

按要求构造「真实结构 RSS 候选 → E4+S 分类 → 入队 → 检查序列化 state」：

1. `test_derived_id_never_matches_a_review_record`
   自己算 `sha1(clean_url(url))[:12]`，断言它**不在**审核记录的键里。
2. `test_derived_id_absent_from_whole_public_state`
   派生 id 不得出现在公开 state.json 的**任何位置**。
3. `test_serialized_review_records_contain_no_news_text`
   逐条检查审核记录，标题/摘要/域名均不出现。
4. `test_reviewed_item_also_stays_in_pool` / `test_ordinary_item_stays_in_pool`
   新闻池条目一个不少，早报素材不受影响。
5. `test_second_enqueue_reuses_the_same_stub`
   同一事件第二轮复用同一句柄，公开侧不长第二条（去重保留）。
6. `test_guard_catches_planted_leak` / `test_save_is_refused_for_planted_leak`
   故意种一条修复前形态的记录，守卫必须拦下且**拒绝落盘**。
7. `TestFullScanCycleEndToEnd`（5 项）
   真跑 `run_scan_mode`，断言落盘文件与 **Actions 日志**都不含派生 id。

---

## 八、修改文件清单

| 文件 | 增/删 | 说明 |
|---|---|---|
| `bot.py` | +227 / −? | P0 公开键与查找、P2 fail-closed、P1 `NEEDS_MANUAL` 分支、日志脱敏 |
| `state_store.py` | +180 / −? | P0 随机公开 id、守卫扩展、迁移换键 |
| `review_store.py` | +140 / −? | P1 SQL 与异常、`release()`、HTTP 409 解析 |
| `deploy/cloudflare-worker/review_vault.js` | +88 / −? | P1 SQL 与路由（与 Python 逐句对应） |
| `deploy/cloudflare-worker/worker.test.mjs` | +252 | C-11a ~ C-11f |
| `tests/test_news_pool_privacy.py` | +529（新） | P0 |
| `tests/test_review_lease_expiry.py` | +373（新） | P1 |
| `tests/test_review_fail_closed.py` | +299（新） | P2 |
| `tests/test_review_privacy_v11.py` | +42 / −? | 适配随机公开键 + 路由契约加 `release` |
| `tests/test_review_queue.py` | 62 行 | 适配随机公开键 |
| `tests/test_review_safety.py` | 72 行 | 适配随机公开键 |
| `tests/run_all.py` | +7 | 注册 3 个新模块 + 输出跳过数 |
| `scripts/verify_10_checks.py` | +8 / −? | 显式注入私有存储（原先依赖 fail-open） |

**未触碰**：`state.json`、`focus_state.json`、`.github/workflows/*`、`wrangler.toml`、
`deploy/cloudflare-worker/worker.js`、`D:\premier-league-bot`、recovery snapshots。

---

## 九、Git 状态

| 项 | 值 |
|---|---|
| 分支 | `feat/v1.0-review-privacy` |
| 代码提交 | `625c9b6756c3d5aaeca9cd73b8720edd5f861481` |
| 提交信息 | `fix(review): PR #1 安全审查整改（P0 关联泄露 / P1 租约过期 / P2 fail-closed）` |
| 本报告 | 作为同一分支上的独立文档提交（SHA 见交付说明） |
| 相对 `origin/main` | ahead 15 |
| 工作区 | **干净**（`git status --porcelain` 输出 0 行） |
| 推送 | **未推送** |
| 合并 | **未合并** |
| 部署 | **未部署** |

### 密钥审计

对本次 diff 全量扫描 `bot<digits>:` / `ghp_` / `sk-` / `AKIA` / `PRIVATE KEY` /
`service_key = "..."`：**零命中**。所有凭据均为测试固定值
（`TEST_TOKEN`、`svc_fake_key`、`unit-test-hmac-secret`）。

### 普通新闻处理逻辑

`_merge_pool()`、`_prepare_ai_candidates()`、`_finalize_result()`、投递状态机、
早报聚合**均未修改**。`bot.py` 的改动全部落在审核分支内，以及
`run_scan_mode` 里审核分支的两处日志。

---

## 十、遗留风险与建议（非本次范围，供下一轮决策）

1. **时间戳侧信道**：审核 stub 的 `review_enqueued_at` / `created_at` 与新闻池
   条目的 `first_seen` 在同一轮扫描内产生。在「本轮只有一条新文章」的安静时段，
   时间接近可被用来做弱关联。彻底关闭需要把审核记录的时间戳粗化到天，
   会牵动 `_prune_events` 的保留策略，建议单独立项评估。
2. **`_review_route` 在生产中可能是死分支**：`_finalize_result()` 会先以
   `EVIDENCE_NOT_PUSHABLE` 拦下单一来源（E4 的典型来源），因此 E4+S 在真实
   链路里是否真能走到审核分支，需要一次真实数据回放确认。本次端到端测试
   为验证审核分支本身，显式绕过了证据闸门，并在测试里注明。
3. **`needs_manual` 目前没有运维入口**：`release()` 已就绪，但还没有对应的
   GitHub Actions 手动入口 / CLI。建议在启用审核功能前补一个显式的
   `mode=review_release` 入口，否则过期租约只能靠人改库放行。
4. **重复入队不再重发卡片**：这是行为变化。若产品上希望「提醒审核人这条还压着」，
   需要另做一个带节流的提醒机制，而不是靠每轮重发卡片。

---

## 十一、下一步（等待授权）

本报告完成后**停止**，不执行以下任何操作：

- ❌ `git push`
- ❌ 创建 / 更新 PR、合并 PR
- ❌ `wrangler deploy`
- ❌ 配置 GitHub / Cloudflare Secrets
- ❌ 调用 Telegram API、`setWebhook`
- ❌ 恢复 cron-job.org
- ❌ 启用生产 `IMPORTANCE_SORT`

需要用户明确授权后再继续。
