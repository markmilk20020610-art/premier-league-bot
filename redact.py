"""日志脱敏（V1.2 任务 8）。

历史问题
--------
Telegram 推送异常时，requests 的异常原文里带着完整的 Bot API URL：

    ConnectionError: failed request https://api.telegram.org/bot123456:AAH.../sendMessage

旧代码把这段原文直接 print 出来，还写进 `::error` 注解、run_telemetry.log
和 state.json 的 last_error。GitHub Actions 日志对所有仓库协作者可见，
Token 一旦进日志就等于把机器人交出去——攻击者拿到 Token 可以冒充本号发任意消息、
读取 bot 的 update 队列。Token 泄露不需要「被黑」，一次网络抖动就够了。

这里只做一件事：把已知的凭证形态替换成占位符，其余内容原样保留。
日志仍然要能看清「哪个块、第几次、什么错」，只是看不到钥匙。
"""

import re

REDACTED = "<redacted>"

# 顺序有讲究：先按 URL 形态削掉整段 token，再兜底削裸 token，
# 否则「/bot123456:AAH...」被裸 token 规则先命中后会留下「/bot」残渣。
_PATTERNS = (
    # Telegram Bot API URL：/bot<token>  →  /bot<redacted>
    (re.compile(r"(?i)(/bot)([A-Za-z0-9_:\-]{8,})"), r"\1" + REDACTED),
    # 裸 Telegram token：<bot_id>:<secret>，bot_id 为 6 位以上纯数字
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_\-]{20,}\b"), REDACTED),
    # 常见大模型 key
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}"), "sk-" + REDACTED),
    # Authorization: Bearer xxx
    (re.compile(r"(?i)\b(Bearer)(\s+)([A-Za-z0-9._\-]{12,})"), r"\1\2" + REDACTED),
    # 查询串里的凭证：?token=... / &api_key=...
    (re.compile(r"(?i)([?&](?:token|api_key|apikey|access_token|key)=)([^&\s\"'<>]+)"),
     r"\1" + REDACTED),
)


def sanitize(text):
    """返回脱敏后的字符串。None / 非字符串一律转成字符串再处理。"""
    if text is None:
        return ""
    out = str(text)
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out


def contains_secret(text, secret):
    """测试与自检用：判断 text 里是否还残留 secret（空 secret 恒为 False）。"""
    if not secret:
        return False
    return str(secret) in str(text or "")
