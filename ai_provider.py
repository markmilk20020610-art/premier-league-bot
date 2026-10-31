"""AI Provider 层（模型调用统一封装）

为什么要有这一层
----------------
旧代码把「拼 prompt + 调模型 + 重试 + 降级」散在 bot.py 里，后果是：
  · Gemini 免费额度耗尽后，每轮扫描还在傻傻请求，日志刷满 429；
  · DeepSeek 失败时直接返回 None，主流程分不清「没新闻」和「模型挂了」；
  · 没有任何调用统计，无法回答「今天到底调了几次、花了多少 token」。

现在所有模型调用都必须经过 AIRouter：
    业务代码 → router.analyze_news(prompt) → provider → HTTP
主程序不允许再直接 import genai / requests 调模型。

默认配置（新增模型费用为 0）
--------------------------
AI_PRIMARY_PROVIDER=deepseek
AI_GEMINI_ENABLED=false      # 接口保留，默认禁用，禁用时不构造 client、不发任何请求
AI_OPENAI_ENABLED=false      # 未接入，占位
"""

import datetime
import json
import os
import re
import time
import concurrent.futures

# ==================== 调用结果类型 ====================
OK = "OK"
RATE_LIMITED = "RATE_LIMITED"
QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
AUTH_FAILED = "AUTH_FAILED"
SERVER_ERROR = "SERVER_ERROR"
TIMEOUT = "TIMEOUT"
NETWORK_ERROR = "NETWORK_ERROR"
BAD_REQUEST = "BAD_REQUEST"
PARSE_ERROR = "PARSE_ERROR"
EMPTY_RESPONSE = "EMPTY_RESPONSE"
DISABLED = "DISABLED"
CIRCUIT_OPEN = "CIRCUIT_OPEN"
BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
NO_PROVIDER = "NO_PROVIDER"

# 这些错误值得重试；其余重试无意义
RETRYABLE = {RATE_LIMITED, SERVER_ERROR, TIMEOUT, NETWORK_ERROR}

_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"bot\d{6,}:[A-Za-z0-9_\-]{10,}"),
    re.compile(r"[0-9]{8,10}:[A-Za-z0-9_\-]{30,}"),
)


def sanitize(text, limit=240):
    """错误信息脱敏：绝不把 API Key / Authorization / Bot Token 写进日志。"""
    s = str(text or "")
    for pattern in _SECRET_PATTERNS:
        s = pattern.sub("***REDACTED***", s)
    s = s.replace("\n", " ").replace("\r", " ").strip()
    return s[:limit]


def estimate_tokens(text):
    """粗估 token：中英混排约 2.5~3 字符/token，用于预算控制而非计费。"""
    return max(1, int(len(text or "") / 2.8))


def _env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


class AIConfig:
    """模型调用配置。全部可用环境变量覆盖，默认值保证「不加钱也能跑」。"""

    def __init__(self, **over):
        self.primary = os.environ.get("AI_PRIMARY_PROVIDER", "deepseek").strip().lower()
        self.gemini_enabled = _env_bool("AI_GEMINI_ENABLED", False)
        self.openai_enabled = _env_bool("AI_OPENAI_ENABLED", False)

        self.max_retries = _env_int("AI_MAX_RETRIES", 2)
        self.retry_base_delay = _env_float("AI_RETRY_BASE_DELAY", 2.0)
        self.request_timeout = _env_int("AI_REQUEST_TIMEOUT", 30)

        # 熔断冷却
        self.quota_cooldown_min = _env_int("AI_QUOTA_COOLDOWN_MIN", 120)
        self.gemini_quota_cooldown_min = _env_int("AI_GEMINI_QUOTA_COOLDOWN_MIN", 360)
        self.auth_cooldown_min = _env_int("AI_AUTH_COOLDOWN_MIN", 1440)
        self.server_error_threshold = _env_int("AI_SERVER_ERROR_THRESHOLD", 3)
        self.server_cooldown_min = _env_int("AI_SERVER_COOLDOWN_MIN", 10)

        # 每轮与每日预算
        self.max_articles_per_run = _env_int("AI_MAX_ARTICLES_PER_RUN", 25)
        self.max_input_chars = _env_int("AI_MAX_INPUT_CHARS", 20000)
        self.max_calls_per_day = _env_int("AI_MAX_CALLS_PER_DAY", 60)
        self.max_tokens_per_day = _env_int("AI_MAX_TOKENS_PER_DAY", 150000)

        # 输出长度（控制长文，省 token）
        self.max_output_tokens = {
            "analyze_news": _env_int("AI_MAX_TOKENS_ANALYZE", 2200),
            "generate_summary": _env_int("AI_MAX_TOKENS_SUMMARY", 3000),
            "generate_content": _env_int("AI_MAX_TOKENS_CONTENT", 900),
            "health_check": 16,
        }
        for k, v in over.items():
            setattr(self, k, v)

    def provider_order(self):
        """主模型优先；Gemini 只有显式启用才会进入候选队列。"""
        order = [self.primary]
        if self.gemini_enabled and "gemini" not in order:
            order.append("gemini")
        return order


# ==================== 调用记录 ====================
class CallResult:
    def __init__(self, ok, data=None, provider="", model="", error_type="", error_message="",
                 status=OK, input_tokens=None, output_tokens=None, response_ms=0):
        self.ok = ok
        self.data = data
        self.provider = provider
        self.model = model
        self.error_type = error_type
        self.error_message = sanitize(error_message)
        self.status = status
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.response_ms = response_ms
        self.request_time = datetime.datetime.now(datetime.timezone.utc).isoformat()

    def to_record(self):
        """落盘用的记录，字段与任务书要求一一对应（不含任何密钥）。"""
        return {
            "provider": self.provider,
            "model": self.model,
            "request_time": self.request_time,
            "response_ms": self.response_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "success": self.ok,
            "error_type": self.error_type,
            "error_message": self.error_message,
        }

    def __repr__(self):
        return f"<CallResult {self.provider}/{self.model} ok={self.ok} status={self.status}>"


def _result_error(provider, model, status, message, started):
    return CallResult(
        False, None, provider, model,
        error_type=status, error_message=message, status=status,
        response_ms=int((time.time() - started) * 1000),
    )


# ==================== 熔断器 ====================
class CircuitBreaker:
    """持久化熔断状态。配额耗尽后不再每轮重复请求，而是等冷却期结束。"""

    def __init__(self, data=None):
        self.data = data if isinstance(data, dict) else {}

    @property
    def is_open(self):
        until = self.data.get("open_until") or ""
        if not until:
            return False
        try:
            return datetime.datetime.fromisoformat(until) > datetime.datetime.now(datetime.timezone.utc)
        except ValueError:
            return False

    def remaining_minutes(self):
        until = self.data.get("open_until") or ""
        try:
            delta = datetime.datetime.fromisoformat(until) - datetime.datetime.now(datetime.timezone.utc)
            return max(0, int(delta.total_seconds() // 60))
        except ValueError:
            return 0

    def trip(self, reason, cooldown_minutes, error_type):
        now = datetime.datetime.now(datetime.timezone.utc)
        self.data.update({
            "state": "open",
            "opened_at": now.isoformat(),
            "open_until": (now + datetime.timedelta(minutes=max(1, cooldown_minutes))).isoformat(),
            "reason": sanitize(reason, 200),
            "error_type": error_type,
            "failures": int(self.data.get("failures", 0)) + 1,
        })

    def record_failure(self):
        self.data["failures"] = int(self.data.get("failures", 0)) + 1
        self.data["last_failure_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        return self.data["failures"]

    def reset(self):
        self.data.clear()


# ==================== Provider 基类 ====================
class BaseProvider:
    name = "base"
    model = "unknown"

    def __init__(self, config, http=None, sleep=None):
        self.config = config
        self.http = http
        self.sleep = sleep or time.sleep

    @property
    def enabled(self):
        return False

    def analyze_news(self, prompt, **kw):
        return self.generate_content(prompt, task="analyze_news", **kw)

    def generate_summary(self, prompt, **kw):
        return self.generate_content(prompt, task="generate_summary", **kw)

    def generate_content(self, prompt, task="generate_content", **kw):
        raise NotImplementedError

    def health_check(self):
        return CallResult(False, None, self.name, self.model, status=DISABLED,
                          error_type=DISABLED, error_message="provider 未启用")


def _extract_json(text):
    """剥掉 markdown 代码围栏后解析 JSON。"""
    raw = (text or "").strip()
    if raw.startswith("```json"):
        raw = raw[7:]
    elif raw.startswith("```"):
        raw = raw[3:]
    if raw.endswith("```"):
        raw = raw[:-3]
    return json.loads(raw.strip())


# ==================== DeepSeek（当前唯一启用） ====================
class DeepSeekProvider(BaseProvider):
    name = "deepseek"
    model = "deepseek-chat"
    BASE_URL = "https://api.deepseek.com/chat/completions"

    def __init__(self, config, api_key=None, http=None, sleep=None):
        super().__init__(config, http=http, sleep=sleep)
        self.api_key = api_key if api_key is not None else os.environ.get("DEEPSEEK_API_KEY", "")
        if self.http is None:
            import requests
            self.http = requests

    @property
    def enabled(self):
        return bool(self.api_key)

    def generate_content(self, prompt, task="generate_content", **kw):
        if not self.enabled:
            return CallResult(False, None, self.name, self.model, status=DISABLED,
                              error_type=DISABLED, error_message="未配置 DEEPSEEK_API_KEY")
        started = time.time()
        max_tokens = kw.get("max_tokens") or self.config.max_output_tokens.get(task, 1200)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "你必须严格只返回 JSON 格式字符串，不得带有任何 markdown 反引号标记。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}

        last = None
        for attempt in range(self.config.max_retries + 1):
            try:
                resp = self.http.post(self.BASE_URL, headers=headers, json=payload,
                                      timeout=self.config.request_timeout)
            except Exception as e:
                last = _result_error(self.name, self.model, TIMEOUT if "timeout" in type(e).__name__.lower()
                                     else NETWORK_ERROR, f"{type(e).__name__}: {e}", started)
            else:
                last = self._handle_response(resp, started)
                if last.ok:
                    return last

            if last.status not in RETRYABLE:
                return last
            if attempt < self.config.max_retries:
                wait = self.config.retry_base_delay * (2 ** attempt)
                print(f"[WARN] DeepSeek {last.status}，{wait:.0f}s 后重试（{attempt + 1}/{self.config.max_retries}）")
                self.sleep(wait)
        return last

    def _handle_response(self, resp, started):
        status_code = getattr(resp, "status_code", 0)
        body = (getattr(resp, "text", "") or "")[:400]
        if status_code == 200:
            try:
                payload = resp.json()
                text = payload["choices"][0]["message"]["content"]
            except Exception as e:
                return _result_error(self.name, self.model, PARSE_ERROR, f"响应结构异常 {type(e).__name__}: {e}", started)
            try:
                data = _extract_json(text)
            except Exception as e:
                return _result_error(self.name, self.model, PARSE_ERROR, f"JSON 解析失败: {e}", started)
            if not isinstance(data, dict):
                return _result_error(self.name, self.model, PARSE_ERROR, "模型返回的不是 JSON 对象", started)
            usage = payload.get("usage") or {}
            return CallResult(True, data, self.name, self.model, status=OK,
                              input_tokens=usage.get("prompt_tokens"),
                              output_tokens=usage.get("completion_tokens"),
                              response_ms=int((time.time() - started) * 1000))

        lowered = body.lower()
        if status_code == 402 or "insufficient balance" in lowered or "quota" in lowered:
            return _result_error(self.name, self.model, QUOTA_EXHAUSTED, body, started)
        if status_code == 429:
            return _result_error(self.name, self.model, RATE_LIMITED, body, started)
        if status_code in (401, 403):
            return _result_error(self.name, self.model, AUTH_FAILED, body, started)
        if status_code >= 500:
            return _result_error(self.name, self.model, SERVER_ERROR, body, started)
        return _result_error(self.name, self.model, BAD_REQUEST, body, started)

    def health_check(self):
        if not self.enabled:
            return CallResult(False, None, self.name, self.model, status=DISABLED,
                              error_type=DISABLED, error_message="未配置 API Key")
        started = time.time()
        try:
            resp = self.http.get("https://api.deepseek.com/models",
                                 headers={"Authorization": f"Bearer {self.api_key}"},
                                 timeout=self.config.request_timeout)
            if getattr(resp, "status_code", 0) == 200:
                return CallResult(True, None, self.name, self.model, status=OK,
                                  response_ms=int((time.time() - started) * 1000))
            return self._handle_response(resp, started)
        except Exception as e:
            return _result_error(self.name, self.model, NETWORK_ERROR, f"{type(e).__name__}: {e}", started)


# ==================== Gemini（保留接口，默认禁用） ====================
class GeminiProvider(BaseProvider):
    """Gemini 只在 AI_GEMINI_ENABLED=true 时才构造 client。

    禁用状态下 enabled=False，router 不会调用它，也就不会产生任何网络请求。
    """

    name = "gemini"
    MODELS = ("gemini-2.5-flash", "gemini-1.5-flash")

    def __init__(self, config, api_key=None, http=None, sleep=None):
        super().__init__(config, http=http, sleep=sleep)
        self.api_key = api_key if api_key is not None else os.environ.get("GEMINI_API_KEY", "")
        self._client = None
        self._types = None
        self._model_index = 0
        self.call_count = 0          # 便于测试断言「禁用时零请求」

    @property
    def enabled(self):
        return bool(self.config.gemini_enabled and self.api_key)

    @property
    def model(self):
        return self.MODELS[self._model_index]

    def _ensure_client(self):
        if self._client is not None:
            return True
        try:
            from google import genai
            from google.genai import types
        except Exception as e:
            print(f"[WARN] google-genai 不可用，Gemini 降级为禁用：{e}")
            return False
        self._client = genai.Client(api_key=self.api_key)
        self._types = types
        return True

    def _config(self):
        types = self._types
        kwargs = dict(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            response_mime_type="application/json",
            temperature=0.2,
        )
        try:
            return types.GenerateContentConfig(
                **kwargs, http_options=types.HttpOptions(timeout=self.config.request_timeout * 1000 - 5000)
            )
        except Exception:
            return types.GenerateContentConfig(**kwargs)

    def generate_content(self, prompt, task="generate_content", **kw):
        if not self.enabled:
            return CallResult(False, None, self.name, self.model, status=DISABLED,
                              error_type=DISABLED, error_message="Gemini 未启用")
        if not self._ensure_client():
            return CallResult(False, None, self.name, self.model, status=DISABLED,
                              error_type=DISABLED, error_message="google-genai 未安装")

        started = time.time()
        self.call_count += 1

        def _invoke():
            return self._client.models.generate_content(
                model=self.model, contents=prompt, config=self._config()
            )

        # 沿用已验证的「线程池硬超时」：SDK 内部超时不一定生效，卡住会拖死整个 Job
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(_invoke)
        try:
            response = future.result(timeout=self.config.request_timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            return _result_error(self.name, self.model, TIMEOUT, f"{self.config.request_timeout}s 未返回", started)
        except Exception as e:
            return _result_error(self.name, self.model, self._classify_exception(e), f"{type(e).__name__}: {e}", started)
        finally:
            executor.shutdown(wait=False)

        try:
            text = (response.text or "").strip()
        except Exception as e:
            return _result_error(self.name, self.model, EMPTY_RESPONSE, f"读取文本失败: {e}", started)
        if not text:
            return _result_error(self.name, self.model, EMPTY_RESPONSE, "模型返回空文本", started)

        try:
            data = _extract_json(text)
        except Exception as e:
            return _result_error(self.name, self.model, PARSE_ERROR, f"JSON 解析失败: {e}", started)
        if not isinstance(data, dict):
            return _result_error(self.name, self.model, PARSE_ERROR, "模型返回的不是 JSON 对象", started)

        records = []
        try:
            gm = response.candidates[0].grounding_metadata
            for chunk in (getattr(gm, "grounding_chunks", None) or []):
                web = getattr(chunk, "web", None)
                if web and getattr(web, "uri", None):
                    records.append({"url": web.uri, "title": getattr(web, "title", "") or ""})
        except Exception:
            pass
        data["grounding_records"] = records

        usage = getattr(response, "usage_metadata", None)
        return CallResult(True, data, self.name, self.model, status=OK,
                          input_tokens=getattr(usage, "prompt_token_count", None),
                          output_tokens=getattr(usage, "candidates_token_count", None),
                          response_ms=int((time.time() - started) * 1000))

    @staticmethod
    def _classify_exception(exc):
        text = f"{type(exc).__name__}: {exc}".lower()
        code = getattr(exc, "code", None)
        try:
            code = int(code) if code is not None else None
        except (TypeError, ValueError):
            code = None
        if "resource_exhausted" in text or "quota" in text:
            return QUOTA_EXHAUSTED
        if code == 429 or "429" in text:
            return QUOTA_EXHAUSTED if "quota" in text else RATE_LIMITED
        if code in (401, 403) or "permission" in text or "unauthenticated" in text:
            return AUTH_FAILED
        if "timeout" in text or "deadline" in text:
            return TIMEOUT
        if code and code >= 500:
            return SERVER_ERROR
        return NETWORK_ERROR

    def health_check(self):
        if not self.enabled:
            return CallResult(False, None, self.name, self.model, status=DISABLED,
                              error_type=DISABLED, error_message="Gemini 未启用")
        return self.generate_content('{"ping": true}', task="health_check")


# ==================== OpenAI（未接入，占位） ====================
class OpenAIProvider(BaseProvider):
    name = "openai"
    model = "not-configured"

    @property
    def enabled(self):
        return False


def build_providers(config, http=None, sleep=None):
    return {
        "deepseek": DeepSeekProvider(config, http=http, sleep=sleep),
        "gemini": GeminiProvider(config, http=http, sleep=sleep),
        "openai": OpenAIProvider(config, http=http, sleep=sleep),
    }


# ==================== 路由器 ====================
class AIRouter:
    """所有模型调用的唯一入口。

    职责：选 provider → 查熔断 → 查预算 → 调用 → 记录统计 → 决定是否熔断。
    """

    def __init__(self, state, config=None, providers=None, http=None, sleep=None):
        self.state = state if isinstance(state, dict) else {}
        self.config = config or AIConfig()
        self.providers = providers or build_providers(self.config, http=http, sleep=sleep)
        self.records = []

    # ---------- 统计 ----------
    def _stats(self):
        today = datetime.datetime.now(datetime.timezone.utc).astimezone(
            datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d")
        stats = self.state.get("ai_stats") or {}
        if stats.get("date") != today:
            stats = {"date": today, "calls": 0, "errors": 0,
                     "input_tokens": 0, "output_tokens": 0, "by_provider": {}}
            self.state["ai_stats"] = stats
        return stats

    def _record(self, res):
        self.records.append(res)
        stats = self._stats()
        stats["calls"] += 1
        if not res.ok:
            stats["errors"] += 1
        stats["input_tokens"] += int(res.input_tokens or 0)
        stats["output_tokens"] += int(res.output_tokens or 0)
        bucket = stats.setdefault("by_provider", {}).setdefault(
            res.provider or "unknown", {"calls": 0, "errors": 0})
        bucket["calls"] += 1
        if not res.ok:
            bucket["errors"] += 1
        print(f"[AI] provider={res.provider} model={res.model} status={res.status} "
              f"ok={res.ok} {res.response_ms}ms tokens={res.input_tokens}/{res.output_tokens}")

    def budget_exceeded(self):
        stats = self._stats()
        if stats["calls"] >= self.config.max_calls_per_day:
            return True, f"今日调用次数已达上限 {self.config.max_calls_per_day}"
        used = stats["input_tokens"] + stats["output_tokens"]
        if used >= self.config.max_tokens_per_day:
            return True, f"今日 token 已用 {used}，达到上限 {self.config.max_tokens_per_day}"
        return False, ""

    # ---------- 熔断 ----------
    def _breaker(self, provider_name):
        store = self.state.setdefault("circuit_breakers", {})
        return CircuitBreaker(store.setdefault(provider_name, {}))

    def _trip_if_needed(self, provider_name, res):
        breaker = self._breaker(provider_name)
        if res.status == QUOTA_EXHAUSTED:
            cooldown = (self.config.gemini_quota_cooldown_min
                        if provider_name == "gemini" else self.config.quota_cooldown_min)
            breaker.trip(res.error_message, cooldown, res.status)
            print(f"::warning title=AI Quota Exhausted::{provider_name} 配额耗尽，熔断 {cooldown} 分钟")
        elif res.status == AUTH_FAILED:
            breaker.trip(res.error_message, self.config.auth_cooldown_min, res.status)
            print(f"::error title=AI Auth Failed::{provider_name} 鉴权失败，停止调用并熔断 {self.config.auth_cooldown_min} 分钟")
        elif res.status in (SERVER_ERROR, TIMEOUT, NETWORK_ERROR):
            if breaker.record_failure() >= self.config.server_error_threshold:
                breaker.trip(res.error_message, self.config.server_cooldown_min, res.status)
                print(f"::warning title=AI Circuit Open::{provider_name} 连续失败 {breaker.data['failures']} 次，熔断 {self.config.server_cooldown_min} 分钟")

    # ---------- 对外接口 ----------
    def analyze_news(self, prompt, **kw):
        return self._call("analyze_news", prompt, **kw)

    def generate_summary(self, prompt, **kw):
        return self._call("generate_summary", prompt, **kw)

    def generate_content(self, prompt, **kw):
        return self._call("generate_content", prompt, **kw)

    def health_check(self):
        report = {}
        for name, provider in self.providers.items():
            if not provider.enabled:
                report[name] = {"enabled": False, "status": DISABLED}
                continue
            breaker = self._breaker(name)
            if breaker.is_open:
                report[name] = {"enabled": True, "status": CIRCUIT_OPEN,
                                "remaining_minutes": breaker.remaining_minutes()}
                continue
            res = provider.health_check()
            report[name] = {"enabled": True, "status": res.status, "ok": res.ok}
        return report

    def _call(self, task, prompt, **kw):
        exceeded, why = self.budget_exceeded()
        if exceeded:
            print(f"::warning title=AI Budget Exceeded::{why}，本轮暂停 AI 调用")
            return CallResult(False, None, "router", "-", status=BUDGET_EXCEEDED,
                              error_type=BUDGET_EXCEEDED, error_message=why)

        tried = []
        for name in self.config.provider_order():
            provider = self.providers.get(name)
            if provider is None:
                continue
            if not provider.enabled:
                print(f"[INFO] provider {name} 未启用，跳过")
                continue

            breaker = self._breaker(name)
            if breaker.is_open:
                print(f"[INFO] provider {name} 熔断中（剩余 {breaker.remaining_minutes()} 分钟），跳过")
                tried.append(f"{name}:{CIRCUIT_OPEN}")
                continue

            res = getattr(provider, task)(prompt, **kw)
            self._record(res)
            if res.ok:
                breaker.reset()
                return res

            self._trip_if_needed(name, res)
            tried.append(f"{name}:{res.status}")
            print(f"[WARN] provider {name} 失败：{res.status} | {res.error_message}")

        if not tried:
            return CallResult(False, None, "router", "-", status=NO_PROVIDER,
                              error_type=NO_PROVIDER, error_message="没有任何可用的 AI provider")
        return CallResult(False, None, "router", "-", status=tried[-1].split(":")[-1],
                          error_type=tried[-1].split(":")[-1],
                          error_message=f"全部 provider 失败：{', '.join(tried)}")


def stats_snapshot(state):
    """给日志/诊断用的当日统计快照。"""
    stats = (state or {}).get("ai_stats") or {}
    return {
        "date": stats.get("date"),
        "calls": stats.get("calls", 0),
        "errors": stats.get("errors", 0),
        "input_tokens": stats.get("input_tokens", 0),
        "output_tokens": stats.get("output_tokens", 0),
        "by_provider": stats.get("by_provider", {}),
    }
