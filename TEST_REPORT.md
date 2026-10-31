# 测试报告 · 第一批（阶段 1 + 阶段 2）

执行时间：2026-10-31
执行命令：`python tests/run_all.py`
结果：**58 个用例全部通过（失败 0 / 错误 0），耗时 40.1s**

```
Ran 58 tests in 40.114s

OK
模块数 5 | 用例 58 | 失败 0 | 错误 0 | 耗时 40.1s
```

> 说明：`tests/test_git_commit.py` 会创建真实裸仓库与两个 clone，跑真实的
> `git pull --rebase` / `git merge` / `git push`，因此耗时约 40 秒。
> 只跑纯逻辑用例可用 `python tests/run_all.py --fast`（53 个，约 0.6s）。

---

## 一、阶段 1.1 证据核验（tests/test_evidence.py，11 个用例）

| 编号 | 场景 | 期望状态 | 结果 |
|---|---|---|---|
| 01 | 有 Grounding URL，但与新闻无关 | UNVERIFIED，不推送 | 通过 |
| 02 | evidence_url 不在证据列表 | UNVERIFIED，不推送 | 通过 |
| 03 | 只有单一爆料来源（太阳报） | SINGLE_SOURCE，评级压到 Tier 3 | 通过 |
| 04 | 三家媒体转载同一篇稿 | 独立来源数 = 1，不是多源 | 通过 |
| 04b | 两家真正独立报道 | MULTIPLE_INDEPENDENT_SOURCES | 通过 |
| 05 | 来源说法冲突（supports vs denies） | CONFLICTING，不推送 | 通过 |
| 06 | 官方渠道（thefa.com） | OFFICIAL_CONFIRMED | 通过 |
| 07 | 证据只有 Google News 聚合链接 | UNVERIFIED，不推送 | 通过 |
| 08 | 完全没有 grounding 记录 | UNVERIFIED，不推送 | 通过 |
| 09 | 域名归一 / URL 规范化 | bbc.co.uk、google.com、去参数 | 通过 |
| 10 | 黑标评级 | 任何证据等级下都不被降级 | 通过 |

关键实现：独立来源数按「标题指纹」聚合，转载不算独立来源；evidence_url 必须
命中证据集合的 canonical URL；聚合器链接一律不可作为原始证据。

## 二、阶段 1.2 DeepSeek 降级（tests/test_pipeline.py）

| 用例 | 断言 | 结果 |
|---|---|---|
| DeepSeek 成功但无证据 | 证据状态 UNVERIFIED，不推送 | 通过 |
| DeepSeek 自带 evidence_status=OFFICIAL_CONFIRMED | 字段被强制剥离，记 DEEPSEEK_NO_GROUNDING | 通过 |

## 三、阶段 1.3 Schema 校验（tests/test_schema.py，12 个用例）

覆盖：has_valid_news 必须是布尔、8 个必填文本字段缺失逐个拦截、evidence_url 非法协议、
evidence_status 非法枚举、evidence_sources 为空、var_rating 非法枚举、评级归一化、
has_valid_news=false 短路。全部通过。

## 四、阶段 2.1 原子持久化（tests/test_state_store.py）

| 用例 | 断言 | 结果 |
|---|---|---|
| 正常读写往返 | 内容一致 | 通过 |
| 文件不存在 | 返回默认状态 | 通过 |
| 文件损坏（半截 JSON） | 抛 StateError，不静默退空 | 通过 |
| os.replace 失败 | 抛 StateError，且不留半截文件 | 通过 |
| 落盘内容被篡改 | 回读校验失败并抛错 | 通过 |
| 不可序列化对象 | 抛 StateError | 通过 |

## 五、阶段 2.2 投递状态机

| 用例 | 断言 | 结果 |
|---|---|---|
| 正常路径 DISCOVERED→…→COMMITTED | 全链路迁移成功，记录 message_id | 通过 |
| 非法迁移 DISCOVERED→COMMITTED | 抛 StateError | 通过 |
| event_id 稳定性 | 同 URL 多次计算结果一致 | 通过 |
| 已送达未提交（Telegram 成功 + 状态没存） | recovery=COMMIT_ONLY，只补提交不重发 | 通过 |
| 卡在 PENDING_DELIVERY 且 attempts>0 | recovery=UNCERTAIN，不自动重发 | 通过 |
| 已 COMMITTED 的事件 | 不再产生恢复动作 | 通过 |

## 六、阶段 2.3 Git 回写（tests/test_git_commit.py，真实 git）

| 用例 | 断言 | 结果 |
|---|---|---|
| 正常推送 | 退出码 0，远端 state.json 已更新 | 通过 |
| 无变化 | 退出码 0，打印「无变化」不产生空提交 | 通过 |
| 两个 clone 同时改 state.json | rebase 冲突后 merge -X ours，退出码 0，远端保留本地状态 | 通过 |
| 远端不可达 | 退出码非 0 且输出 `::error` | 通过 |
| 工作区有其它脏文件 | 只提交 state.json，bot.py 保持未提交 | 通过 |

## 七、联调回归（tests/test_pipeline.py）

| 用例 | 结果 |
|---|---|
| UNVERIFIED / CONFLICTING / Schema 不合法 → 不推送 | 通过 |
| 单一小报来源推送但评级被压到 Tier 3 | 通过 |
| 推送成功 → 状态写为 COMMITTED 且记录 message_id | 通过 |
| Telegram 成功 + 状态落盘失败 → 退出码 1，不写 COMMITTED | 通过 |
| 发送前状态落盘失败 → 中止发送（0 条消息） | 通过 |
| 启动时恢复 DELIVERED 未 COMMITTED 的事件，不重发 | 通过 |
| 本轮不推送也要把新闻池落盘 | 通过 |
| 早报：成功写日期锁 / 同日不重复 / 发送成功但状态失败返回 1 | 通过 |

---

## 本轮测试发现并修复的真实缺陷

1. **`has_valid_news=false` 仍会进入推送分支**：重构 gate 时 `model_result` 未置空，
   导致「模型判定无新闻」也会继续走投递流程。由 `test_pool_saved_even_when_nothing_pushed`
   暴露，已修复（`model_result = None`）。
2. **黑标会被评级复核误降级**：黑标是内容安全标记，不是可信度分级，
   原先会被 `enforce_rating` 降成 Tier 2/Tier 3，导致严肃新闻卡片模板切错。已修复。
3. **新闻池只在推送成功时才落盘**：导致「没推送的那天池子全丢」，早报继续缺原料。已修复。

## 尚未解决 / 已知限制

1. 证据「内容相关性」目前靠 URL 归属 + 标题指纹判断，未做正文语义比对。
   同一 URL 被模型拿来描述另一件事，仍可能通过。阶段 4 会补 RSS 侧交叉校验。
2. 标题指纹对「同一事件不同角度报道」会判为多个独立来源（偏宽松），
   对「同一稿不同标题改写」会判为单一来源（偏严格）。属于启发式，不是语义判断。
3. 冲突检测依赖模型返回结构化的 source_claims；模型不返回时只能靠 evidence_conflict 布尔值。
4. `git merge -X ours` 冲突策略会丢弃远端在同一提交里的其它改动——因为本脚本只提交
   state.json，影响面可控，但若将来有人手工在 Actions 里改别的文件需要重新评估。
5. 未做真实 API 联调（无可用 Key），Gemini/DeepSeek/Telegram 的真实响应字段
   （如 grounding_chunks 结构变化）仍需上线后观察一轮。
