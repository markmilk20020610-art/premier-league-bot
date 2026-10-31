# 调度单点故障解决方案

## 问题

现在的链路是：

```
cron-job.org ──dispatch──> GitHub Actions ──> bot.py ──> Telegram
```

`cron-job.org` 是**唯一**触发器。它一停，GitHub 那边什么都不会发生。

更糟的是：仓库里本来有个 `scripts/watchdog.py` 负责发现「扫描停了」，但它
**自己也是靠 GitHub 的 schedule 触发的**——而 GitHub schedule 在这个仓库
实测 3 天只触发 7 次、早报迟到 207 分钟。**看门狗和被监控对象共用同一条
不可靠的链路，等于没有看门狗。**

## 解决思路

不追求「让某一个调度器变可靠」，而是**做成多点冗余 + 幂等**：

| 层级 | 角色 | 触发频率 | 依赖 | 现状 |
|---|---|---|---|---|
| 主 | cron-job.org | 1 分钟（需你改） | cron-job.org | ✅ 已在使用 |
| 备 | **Cloudflare Worker**（本目录） | 1 分钟 | Cloudflare 免费档 | ⬜ 待部署 |
| 兜底 | GitHub `schedule` | 5 分钟（极不规律） | GitHub | ✅ 已启用 |
| 观察 | Worker 兼做外部看门狗 | 1 分钟 | Cloudflare | ⬜ 待部署 |

关键点：**备份不是「同时开两份」，而是「按心跳缺口接管」。**

Worker 每分钟读一次 `state.json` 的 `last_scan_at`：

- 心跳 < 6 分钟 → 主调度器正常，**什么都不做**；
- 心跳 ≥ 6 分钟 → 主调度器疑似停摆，**主动 dispatch 一次**把流水线拉起来；
- 心跳 ≥ 20 分钟 → 额外发一条 Telegram 告警，告诉你「cron-job.org 可能挂了」。

主调度器恢复后 Worker 自动闭嘴，不需要人工切换。

### 为什么多重触发不会重复推送

- `concurrency` 会把并发的 run 串行化；
- `bot.py` 的 `state.json` 里有 `processed_urls` / `news_pool` / `events` 去重，
  第二轮发现没有新内容就 0 推送退出。

这是本项目早就有的性质（不是本方案新增的假设）。所以「多点触发」是安全的。

---

## 部署步骤（Cloudflare Worker，约 5 分钟，免费）

### 1. 建一个 GitHub PAT

GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → New：

- Repository access：只选 `premier-league-bot`
- Permissions → Repository permissions：
  - **Actions: Read and write**（必须，用于 dispatch）
  - **Contents: Read-only**（必须，用于读 state.json）
- 有效期建议 1 年，到期前记得续

> ⚠️ 这个 token 只有这两个权限，泄漏的影响面被限制在「能触发本仓库的 workflow」，
> 拿不到代码以外的任何东西。

### 2. 部署

```bash
cd deploy/cloudflare-worker
npm i -g wrangler
wrangler login

wrangler secret put GITHUB_TOKEN          # 粘贴第 1 步的 PAT
wrangler secret put TELEGRAM_BOT_TOKEN    # 与仓库 Secrets 里同一个
wrangler secret put TELEGRAM_CHAT_ID

wrangler deploy
```

部署成功后会打印一个 `https://yuewei-oversight.<你的子域>.workers.dev` 地址。

### 3. 验证（重要，别跳过）

```bash
# 只读探活：不会 dispatch，只报告心跳年龄
curl https://yuewei-oversight.<你的子域>.workers.dev
```

正常应看到 `"action": "healthy"` 和 `"age_minutes": 1.x`。

再手动试一次接管（会真的 dispatch 一轮）：

```bash
curl "https://yuewei-oversight.<你的子域>.workers.dev/?force=1"
```

然后去 Actions 页面确认多了一次 `workflow_dispatch` 运行。

### 4. 确认 cron 生效

Cloudflare Dashboard → Workers & Pages → yuewei-oversight → Triggers，
应看到 `* * * * *`。等 5 分钟后用探活地址看 `age_minutes` 是否一直保持在个位数。

---

## 不想用 Cloudflare？用 Google Apps Script（同样免费）

如果不想注册 Cloudflare，可以用 Google 账号自带的 Apps Script，
时间触发器同样支持每分钟：

1. 打开 https://script.google.com → 新建项目
2. 粘贴下面的代码，把三个占位符换成真实值
3. 左侧「触发器」→ 添加触发器 → 函数 `tick` → 时间驱动 → **每分钟**
4. 手动运行一次 `tick()` 授权

```javascript
const REPO = 'markmilk20020610-art/premier-league-bot';
const TOKEN = 'ghp_xxx';                 // 同第 1 步的 PAT
const TG_TOKEN = '123456:AAA';           // Telegram
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

  if (age < 6) return;                     // 主调度器正常

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

  if (age >= 20 && age < 23) {             // 只在刚跨过阈值时告警一次
    UrlFetchApp.fetch('https://api.telegram.org/bot' + TG_TOKEN + '/sendMessage', {
      method: 'post',
      contentType: 'application/json',
      payload: JSON.stringify({
        chat_id: TG_CHAT,
        text: '⚠️ 调度中断约 ' + Math.round(age) + ' 分钟，已自动补触发',
      }),
      muteHttpExceptions: true,
    });
  }
}
```

Apps Script 的缺点是免费额度每天只有 90 分钟执行时间、且单次最快 1 分钟，
对本场景够用；优点是不用新注册账号。

---

## 两个都不要？那至少把 GitHub 兜底打开

仓库已经启用 `cron: '*/5 * * * *'` 作为第三层兜底。它**极不可靠**
（实测 3 天 7 次），但能在「cron-job.org 和 Worker 同时挂掉」时
偶尔把流水线拉起来，避免彻底静默。

代价：几乎为零（public 仓库 Actions 免费）。

---

## 这套方案能保证什么、不能保证什么

**能保证**：任何**单一**调度器故障都不会让扫描永久停止——最多延迟约 6 分钟，
且会收到 Telegram 告警。

**不能保证**：所有外部调度同时挂掉时仍能准时。这种情况只能靠 GitHub 的
`schedule` 兜底，而它本来就不可靠。要做到真正零单点，需要至少两个不同厂商的
外部调度器同时在线（本方案已经提供了两个实现，建议至少部署一个）。

**一个诚实的提醒**：Cloudflare Worker 与 Google Apps Script 这两份代码
**尚未在真实账号上部署验证过**（我没有你的账号）。代码逻辑简单、有探活接口，
但请在部署后按「第 3 步验证」走一遍，确认 `age_minutes` 会随心跳更新再放心。
