# Cloudflare Worker：调度接管 + 私有审核金库

这个 Worker 现在承担三件事，三件都**必须**在 Cloudflare 侧运行，不能搬回 GitHub：

| # | 职责 | 为什么不能在 GitHub 侧做 |
|---|---|---|
| 1 | **调度接管**：主调度器（cron-job.org）停摆时补触发 `scan` | 看门狗和被监控对象共用同一条链路，等于没有看门狗 |
| 2 | **故障告警**：同一故障周期只告警一次 | 需要跨 invocation 的持久化状态，Actions 没有 |
| 3 | **审核回调执行**：校验 Telegram 按钮 → 登记决定 → 触发 `review_action` | 需要原子状态机 + 私有存储，公开仓库不能放 |

---

## 一、数据边界（先看这个，它决定了后面所有配置）

本仓库是 **Public**，`state.json` 被 git 跟踪，`scanner.yml` 每轮都会提交它。
所以 `state.json` 里的每一个字节都是**公开发布**的。

E4 待审事件的本质是「未公开报道、单一来源、尚未核实」。把它的标题、事实摘要、
原始链接、审核意见、审核人写进 `state.json`，等于把「我们手上有一条未核实的独家」
连同证据一起公开。

因此数据被切成三份，**任何一份都不得越界**：

```
┌─────────────────────────┐   ┌──────────────────────────┐   ┌────────────────────┐
│  公开 state.json         │   │  私有审核金库（本 Worker） │   │  Actions 日志       │
│  （git 提交，全网可见）   │   │  SQLite Durable Object   │   │  （公开）           │
├─────────────────────────┤   ├──────────────────────────┤   ├────────────────────┤
│ event_id（URL 哈希）     │   │ 完整 review_payload      │   │ 只有状态与 message_id│
│ review_ref（opaque 句柄）│   │ 标题 / 事实摘要 / 证据   │   │ 正文只以            │
│ delivery_status          │   │ 审核人 / 审核意见        │   │ title_len /         │
│ 时间戳 / 重试计数        │   │ 操作日志 / 一次性标记    │   │ summary_len 出现    │
└─────────────────────────┘   └──────────────────────────┘   └────────────────────┘
        ↑ 只此三样                 ↑ 唯一存正文的地方              ↑ 结构摘要
```

三条硬约束（都在代码里有守卫，不是靠自觉）：

1. `state_store.save_state_atomic` 落盘前扫一遍，发现审核私有字段**直接抛异常**
   —— 宁可整轮红掉，也不泄露。
2. `state_store.ensure_shape` 载入即迁移，把历史遗留的公开审核快照清掉。
3. `review_store.redact_payload_for_log` 是日志里唯一允许的 payload 表示方式。

`workflow_dispatch` 的 inputs **只允许**携带 `review_id`（opaque 句柄）。
正文、审核意见、Telegram 管理员 ID、HMAC 签名、操作令牌一律不得进入 inputs
—— 它们会显示在 Actions 页面上。Action 需要完整 payload 时，用
`REVIEW_SERVICE_KEY` 走 `/internal/review/<id>/claim` 一次性换取。

---

## 二、部署前核查（**阻断项**，不要跳过）

### 2.1 官方事实

- Workers **Free** 计划**只能**使用 SQLite 存储后端的 Durable Object。
- `new_sqlite_classes` 是新建 SQLite 后端的唯一路径；
  `new_classes`（key-value 后端）需要 Workers Paid，且只对已有 KV 命名空间的账户开放。
- 存储后端**创建后不可更改**：一旦建了 key-value 命名空间，只能删掉重建（丢数据）。

参考：<https://developers.cloudflare.com/durable-objects/platform/pricing/>

### 2.2 本仓库的配置

```toml
[[durable_objects.bindings]]
name = "VAULT"
class_name = "Vault"

[[migrations]]
tag = "v1"
new_sqlite_classes = ["Vault"]
```

`wrangler.toml` 里**不得**出现生效的 `new_classes =` 行
（`tests/test_review_privacy_v11.py::test_wrangler_uses_sqlite_backed_durable_objects`
会检查这一点，注释里提到不算）。

### 2.3 核查步骤

```bash
cd deploy/cloudflare-worker
wrangler whoami                     # 确认登录的是哪个账号
wrangler deploy --dry-run --outdir dist
```

`--dry-run` 不产生任何线上资源，只验证配置能被接受。**真正能证明可用的只有一次
真实 `wrangler deploy`**，因为命名空间是在部署时创建的：

- 部署成功且输出里有 `Vault` 的 Durable Object 命名空间 → ✅ 可用，继续。
- 报错类似 `new_sqlite_classes is not supported` / `requires a paid plan`
  → ❌ **阻断**，按 2.4 处置。

### 2.4 不可用时的处置（fail-closed，不许绕过）

如果当前 Cloudflare 账号用不了免费的 SQLite Durable Object：

1. **不要升级付费计划。** 这是预算决定，不是工程决定。
2. **不要退回 KV。** KV 是最终一致的，两个并发写入都可能「成功」，
   最后谁赢取决于复制顺序——那不是锁，是抽奖。
3. **不要把 payload 放回 `state.json`。** 那正是本次改造要消灭的缺陷。
4. 报阻断，等决策。

代码本身已经按 fail-closed 实现，缺 DO 时不会「降级运行」，而是直接拒绝：

| 场景 | 行为 |
|---|---|
| `/internal/*` 且 `env.VAULT` 未绑定 | `503 {"error":"durable_object_not_bound"}` |
| `tick()` 且 DO 不可用 | `action: "skip_no_durable_lock"`，**不 dispatch** |
| `bot.py` 且 `REVIEW_STORE_URL` 未配 | E4 事件挂起，打印 `::error title=Review Store Missing::`，**不写 state.json** |
| `bot.py` 且私有存储不可用 | 同上，`::error title=Review Store Failed::` |

换句话说：**配置不完整时系统会拒绝工作，而不是带着隐患工作。**
看到这几条报错就是「需要人去配」，不是「可以忽略的告警」。

---

## 三、首次部署

### 3.1 建 PAT

GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained**：

- Repository access：只选 `premier-league-bot`
- Permissions → Repository permissions：
  - **Actions: Read and write**（dispatch 必需）
  - **Contents: Read-only**（读 `state.json` 必需）

> 这个 token 只有这两个权限，泄漏的影响面被限制在「能触发本仓库的 workflow」。

### 3.2 注入 Worker Secrets

```bash
cd deploy/cloudflare-worker
npm i -g wrangler
wrangler login

wrangler secret put GITHUB_TOKEN                  # 3.1 的 PAT
wrangler secret put TELEGRAM_BOT_TOKEN            # 与 GitHub Secrets 同一个
wrangler secret put TELEGRAM_OPS_CHAT_ID          # 私有运维群，不要用公开情报频道
wrangler secret put TELEGRAM_REVIEW_CHAT_ID       # 私有审核群
wrangler secret put TELEGRAM_WEBHOOK_SECRET       # setWebhook 的 secret_token
wrangler secret put REVIEW_OPERATOR_IDS           # 逗号分隔的 Telegram User ID 白名单
wrangler secret put REVIEW_CALLBACK_HMAC_SECRET   # 必须与 GitHub Secret 同值
wrangler secret put INTERNAL_SERVICE_KEY          # 必须与 GitHub Secret REVIEW_SERVICE_KEY 同值
```

### 3.3 部署

```bash
wrangler deploy
```

成功后会打印 `https://yuewei-oversight.<你的子域>.workers.dev`。

### 3.4 注册 Webhook（**最后一步**，且只在审核群已就绪后做）

```bash
curl "https://api.telegram.org/bot<TOKEN>/setWebhook" \
  -d "url=https://yuewei-oversight.<你的子域>.workers.dev" \
  -d "secret_token=<TELEGRAM_WEBHOOK_SECRET>"
```

`secret_token` 必须与 Worker 里的 `TELEGRAM_WEBHOOK_SECRET` 完全一致：
Worker 用 `X-Telegram-Bot-Api-Secret-Token` 头校验来源，不一致会直接 401。

> ⚠️ 这一步会让 Worker 开始接收 Telegram 回调。**在 3.2 全部 secret 注入完成之前
> 不要执行**——否则第一个回调就会因为缺 HMAC 密钥而失败。

---

## 四、验证清单

```bash
# 1. 只读探活：不 dispatch，只报心跳年龄
curl https://yuewei-oversight.<你的子域>.workers.dev
#    期望："action": "healthy"，"age_minutes" 个位数

# 2. 内部接口必须拒绝无密钥请求
curl -s -o /dev/null -w '%{http_code}\n' \
  https://yuewei-oversight.<你的子域>.workers.dev/internal/review/by-event
#    期望：401

# 3. 带正确密钥应能查询（用一个不存在的 event_id）
curl -X POST -H "X-Service-Key: <INTERNAL_SERVICE_KEY>" \
  -H "content-type: application/json" -d '{"event_id":"none"}' \
  "https://yuewei-oversight.<你的子域>.workers.dev/internal/review/by-event"
#    期望：{"ok":true,"found":false,"review_ref":"","status":""}
#    注意 by-event 是 POST（body 带 event_id），GET 会 404

# 4. Webhook 来源校验
curl -X POST https://yuewei-oversight.<你的子域>.workers.dev/ -d '{}'
#    期望：401（缺 secret token 头）

# 5. 确认 cron 生效
#    Dashboard → Workers & Pages → yuewei-oversight → Triggers，应看到 * * * * *
```

部署后 10 分钟内观察：`age_minutes` 应稳定在个位数，且 Actions 页面
**不应该**出现由 Worker 触发的额外 run（主调度器健康时它什么都不做）。

---

## 五、配置清单

### GitHub Secrets（Settings → Secrets and variables → Actions）

| Secret | 用途 | 谁读它 |
|---|---|---|
| `REVIEW_STORE_URL` | Worker 的根地址（`https://...workers.dev`） | `scan` / `review_action` |
| `REVIEW_SERVICE_KEY` | 调 `/internal/*` 的服务密钥，**必须等于 Worker 的 `INTERNAL_SERVICE_KEY`** | 同上 |
| `TELEGRAM_REVIEW_CHAT_ID` | 私有审核群 ID，卡片只发这里 | `scan` / `review_action` |
| `REVIEW_CALLBACK_HMAC_SECRET` | 按钮签名密钥，**必须等于 Worker 同名 secret** | `scan` |

原有的 `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` 继续使用。

### Worker Secrets

| Secret | 用途 | 对应 GitHub Secret |
|---|---|---|
| `GITHUB_TOKEN` | dispatch workflow / 读 `state.json` | — |
| `TELEGRAM_BOT_TOKEN` | 发运维告警、发审核卡片 | `TELEGRAM_BOT_TOKEN` |
| `TELEGRAM_OPS_CHAT_ID` | 运维告警收件人 | — |
| `TELEGRAM_REVIEW_CHAT_ID` | 审核卡片收件人 | `TELEGRAM_REVIEW_CHAT_ID` |
| `TELEGRAM_WEBHOOK_SECRET` | 校验 POST 来自 Telegram | — |
| `REVIEW_OPERATOR_IDS` | 谁能点按钮（Telegram User ID 白名单） | — |
| `REVIEW_CALLBACK_HMAC_SECRET` | 按钮签名 | `REVIEW_CALLBACK_HMAC_SECRET` |
| `INTERNAL_SERVICE_KEY` | `/internal/*` 鉴权 | `REVIEW_SERVICE_KEY` |

**三组「必须同值」**：`REVIEW_CALLBACK_HMAC_SECRET`、`INTERNAL_SERVICE_KEY` ↔
`REVIEW_SERVICE_KEY`、`TELEGRAM_REVIEW_CHAT_ID`。
不同值不会报「配置错误」，而是表现为「签名验不过」或「401」——排查时先看这里。

### 阈值（`wrangler.toml` 的 `[vars]`，无需保密）

| 变量 | 值 | 依据 |
|---|---|---|
| `EXPECTED_SCAN_INTERVAL_MINUTES` | 5 | GitHub schedule 兜底间隔 |
| `FAILOVER_MINUTES` | 12 | 5 + scan 超时 6 + 1 分钟余量 |
| `DISPATCH_COOLDOWN_MINUTES` | 10 | 接管后冷却窗口，存 DO，重启不丢 |
| `ALERT_MINUTES` | 30 | 必须 > FAILOVER + COOLDOWN（22） |
| `READ_FAIL_ALERT_MINUTES` | 8 | 躲过 GitHub API 抖动 |
| `MORNING_GRACE_MINUTES` | 180 | 必须与 `scripts/ensure_morning_running.py` 的 `RETRY_BAND_MINUTES` 一致 |

阈值校准依据：仓库 `state.json` 400 条历史提交里提取的 394 个 `last_scan_at`
间隔 —— `p50 = 1.01` 分钟、`p90 = 1.29` 分钟（93.4% ≤ 2 分钟），
其余 6.6% 为 20~511 分钟的真实停摆。正常节奏是 1 分钟（cron-job.org），
5 分钟是 GitHub schedule 的兜底。

### cron-job.org（待核实）

- 当前状态：**用户主动停止中**，等代码与流程验收通过后恢复每分钟触发。
- 恢复前需确认：它 dispatch 的 `mode` 仍是 `scan`（新增 mode 若未在
  `scan` job 的否定式 `if` 条件里排除，会「漏」进 scan → 真实抓取 + 推送）。

---

## 六、回滚

按影响面从小到大：

1. **只停审核回调**：`curl ".../deleteWebhook"`。Worker 仍负责调度接管，
   审核卡片照常发到审核群，但按钮点了没反应（这是安全的降级：不发布）。
2. **停全部接管**：Dashboard 删除 cron trigger。Worker 保留，不再自动 dispatch。
3. **完全下线**：`wrangler delete`。注意这会**删除 Durable Object 命名空间
   及其中的审核记录**，不可恢复。下线前先导出待审记录。

---

## 七、为什么这套设计是安全的（给未来的自己）

**调度风暴**：旧实现靠 `age >= alertMin && age < alertMin + 3` 的 3 分钟窗口
「去重」，且 dispatch 完不记账。结果是每分钟重复 dispatch。现在冷却时间存在
DO 的 `ops_kv` 表里，`mark: true` 是一次原子的「检查并占用」——
Worker 被回收、重启、换实例都不影响。

**告警刷屏**：旧 `alertOnce` 没有任何持久化，名字骗人。现在
`ops_fault.alerted` 用条件 UPDATE 抢占名额（`WHERE kind=? AND alerted=0`），
一个故障周期只有第一个请求拿到 `rowsWritten === 1`。
抢到名额但 Telegram 发送失败时**归还名额**，否则这次故障会被永久静音。

**审核按钮伪造**：`callback_data` 里带的是 HMAC 签名，不是裸 `event_id`。
签名覆盖 `v1|action|review_ref|exp`，改动作、改句柄、改有效期都会验签失败。
用 `review_ref`（8 字节随机）而不是 `event_id`（URL 哈希，拿到候选 URL 就能算）
—— 「知道 ID」不等于「能点按钮」。

**重复发布**：批准 → 认领 → 投递 → 回写，四步里每一步都是条件 UPDATE。
同一 `event_id` 只有一条「还有效」的审核记录（部分唯一索引，
终态不占位）。重复点击、旧按钮重放、两个 job 并发，都只有一个能成功。

**投递顺序**：**先确认 Telegram 发送结果，再迁移私有审核状态**。
反过来的话「Telegram 失败但私有状态已 delivered」会让这条永远不再重试，
而读者一条都没收到。三态必须区分：已送达 / 结果未知（不自动重发）/
确定性失败（可重试）。

---

## 附录：不想用 Cloudflare 的降级方案

`Google Apps Script` 版本只能做「调度接管」，**没有**私有审核金库。
如果只能选它，则审核功能必须关闭（`_enqueue_review` 会因
`REVIEW_STORE_URL` 未配置而挂起所有 E4 事件，这是预期的安全行为）。

```javascript
const REPO = 'markmilk20020610-art/premier-league-bot';
const TOKEN = 'ghp_xxx';                 // 同 3.1 的 PAT
const TG_TOKEN = '123456:AAA';
const TG_CHAT = '123456789';

function tick() {
  const url = 'https://api.github.com/repos/' + REPO + '/contents/state.json';
  const res = UrlFetchApp.fetch(url, {
    headers: { Authorization: 'Bearer ' + TOKEN, Accept: 'application/vnd.github.raw' },
    muteHttpExceptions: true,
  });
  if (res.getResponseCode() !== 200) return;
  const state = JSON.parse(res.getContentText());
  const age = (Date.now() - Date.parse(state.last_scan_at)) / 60000;
  if (age < 12) return;                    // FAILOVER_MINUTES
  UrlFetchApp.fetch(
    'https://api.github.com/repos/' + REPO + '/actions/workflows/scanner.yml/dispatches',
    {
      method: 'post',
      contentType: 'application/json',
      headers: { Authorization: 'Bearer ' + TOKEN, Accept: 'application/vnd.github+json' },
      payload: JSON.stringify({ ref: 'main', inputs: { mode: 'scan' } }),
      muteHttpExceptions: true,
    },
  );
}
```

注意这个简化版**没有冷却锁**（Apps Script 无法做原子条件写），
所以它只能作为「最后兜底」，不能替代 Worker。
