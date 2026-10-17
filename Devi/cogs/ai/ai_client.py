"""OpenAI-compatible chat client for ProxyAPI, DeepSeek and Qwen (Alibaba Cloud Model Studio).

The whole bot uses ONE shared API key (AI_API_KEY in config/.env), so this module
is also the place where the money is protected:
  * hidden "thinking" is switched off (it is billed as output tokens),
  * output length is capped by the caller (max_tokens),
  * real token usage (including cache hits) is converted to the cost of every call,
  * a billing/auth error (401/402/403/quota 429) blocks the provider for a few minutes
    instead of hammering it with requests that will fail anyway,
  * the number of parallel requests is limited.
"""

import asyncio
import json
import time
from dataclasses import dataclass

import aiohttp

from paths import env_var, env_var_to_int

# Prices are per 1M tokens in the provider's currency: (input cache miss, input cache hit, output).
# They are used only to estimate the spend for the daily budget guard, so keep them conservative.
# Check the official pricing pages before relying on the numbers:
#   ProxyAPI: https://proxyapi.ru/models/browse  (RUB, VAT included; the model page may show a cheaper base tariff)
#   DeepSeek: https://api-docs.deepseek.com/quick_start/pricing  (USD, peak-hour list prices below)
#   Qwen:     https://www.alibabacloud.com/help/en/model-studio/model-pricing  (USD, Singapore, <=32K input)
#
# "extra_body" is a list of request variants for switching the hidden reasoning off. The first one is
# sent; if the API answers HTTP 400 (it does not know that spelling) the next one is tried, and the first
# variant that works is remembered. Only ProxyAPI needs several: its docs page about reasoning could not
# be verified, DeepSeek and Qwen document exactly one spelling each.
PROVIDERS: dict[str, dict] = {
    "proxyapi": {
        "base_url": "https://api.proxyapi.ru/v1",
        # Cheapest model of the catalog that also reads images. Ids look like "vendor/model".
        "text_model": "qwen/qwen3.7-flash",
        "vision_model": "qwen/qwen3.7-flash",
        "extra_body": ({"reasoning_effort": "none"}, {"reasoning": {"enabled": False}}, {}),
        "image_detail": "",
        "currency": "₽",
        "price": (7.58, 0.81, 21.05),
        "daily_budget": 50.0,
        "key_env": "PROXYAPI_API_KEY",
        "balance_url": "https://api.proxyapi.ru/proxyapi/balance",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "text_model": "deepseek-flash",
        "vision_model": "deepseek-flash",
        # V4 models think by default and bill the reasoning as output tokens.
        "extra_body": ({"thinking": {"type": "disabled"}},),
        # "low" downscales images to 512x512 before inference (fewer image tokens).
        "image_detail": "low",
        "currency": "$",
        "price": (0.30, 0.006, 1.20),
        "daily_budget": 1.0,
        "key_env": "DEEPSEEK_API_KEY",
        "balance_url": "",
    },
    "qwen": {
        # Singapore (international) endpoint. Alibaba now recommends the workspace-specific
        # domain, e.g. https://<WorkspaceId>.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1
        # - put it into AI_BASE_URL if you have it.
        "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "text_model": "qwen3.7-flash",
        "vision_model": "qwen3.7-flash",
        # Qwen3.7 is a hybrid-thinking model and thinking is on by default.
        "extra_body": ({"enable_thinking": False},),
        "image_detail": "",
        "currency": "$",
        "price": (0.03, 0.006, 0.13),
        "daily_budget": 1.0,
        "key_env": "DASHSCOPE_API_KEY",
        "balance_url": "",
    },
}

DEFAULT_PROVIDER = "proxyapi"
MAX_ESTIMATED_IMAGE_TOKENS = 1000  # only used when the API does not report usage


class AiError(Exception):
    """Base class for provider errors."""


class AiNotConfigured(AiError):
    """AI_API_KEY is not set."""


class AiUnavailable(AiError):
    """The provider rejected the key / has no balance. Calls are paused for a while."""


class AiRateLimited(AiError):
    """HTTP 429 without a quota problem."""


class AiRequestError(AiError):
    """Any other failed request or an unusable response."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    text_model: str
    vision_model: str
    extra_body_options: tuple[dict, ...]
    image_detail: str
    currency: str
    price_input: float
    price_cached: float
    price_output: float
    daily_budget: float
    balance_url: str
    max_tokens_param: str


@dataclass
class ChatResult:
    text: str
    finish_reason: str | None
    model: str
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int  # includes hidden reasoning tokens, they are billed as output
    reasoning_tokens: int
    cost_micro: int  # estimated cost in millionths of the provider's currency


def env_float(name: str, default: float) -> float:
    raw = env_var(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be a number, got {raw!r}") from None


def money(cost_micro: int, currency: str) -> str:
    return f"{cost_micro / 1_000_000:.4f} {currency}"


def load_provider() -> Provider:
    """Builds the provider config from AI_PROVIDER plus optional overrides in the environment."""
    name = env_var("AI_PROVIDER", DEFAULT_PROVIDER).strip().lower()
    if name not in PROVIDERS:
        raise RuntimeError(f"AI_PROVIDER must be one of {', '.join(PROVIDERS)}, got {name!r}")
    preset = PROVIDERS[name]

    options = tuple(preset["extra_body"])
    raw_extra = env_var("AI_EXTRA_BODY", "").strip()
    if raw_extra:
        try:
            custom = json.loads(raw_extra)
            if not isinstance(custom, dict):
                raise ValueError
        except ValueError:
            raise RuntimeError("AI_EXTRA_BODY must be a JSON object, e.g. {\"thinking\": {\"type\": \"disabled\"}}") from None
        options = (custom,)  # an explicit setting is used as is, without fallbacks

    text_model = env_var("AI_TEXT_MODEL", "").strip() or preset["text_model"]
    # Used only for messages that carry images, so it must be a vision-capable model.
    vision_model = env_var("AI_VISION_MODEL", "").strip() or preset["vision_model"]
    price_input, price_cached, price_output = preset["price"]

    return Provider(
        name=name,
        base_url=(env_var("AI_BASE_URL", "").strip() or preset["base_url"]).rstrip("/"),
        text_model=text_model,
        vision_model=vision_model,
        extra_body_options=options,
        image_detail=env_var("AI_IMAGE_DETAIL", preset["image_detail"]).strip(),
        currency=env_var("AI_CURRENCY", "").strip() or preset["currency"],
        price_input=env_float("AI_PRICE_INPUT", price_input),
        price_cached=env_float("AI_PRICE_CACHED", price_cached),
        price_output=env_float("AI_PRICE_OUTPUT", price_output),
        daily_budget=preset["daily_budget"],
        balance_url=preset["balance_url"],
        # Newer OpenAI models want "max_completion_tokens" instead.
        max_tokens_param=env_var("AI_MAX_TOKENS_PARAM", "max_tokens").strip() or "max_tokens",
    )


def load_api_key(provider: Provider) -> str:
    """The single shared key: AI_API_KEY, or the provider's own variable name as a fallback."""
    return (env_var("AI_API_KEY", "").strip() or env_var(PROVIDERS[provider.name]["key_env"], "").strip())


def build_user_message(text: str, images: list[dict], image_detail: str = "") -> dict:
    """Builds the final user message. `images` items look like {"mime_type": ..., "data": <base64>}."""
    if not images:
        return {"role": "user", "content": text}

    parts: list[dict] = []
    if text:
        parts.append({"type": "text", "text": text})
    for image in images:
        image_url = {"url": f"data:{image['mime_type']};base64,{image['data']}"}
        if image_detail:
            image_url["detail"] = image_detail
        parts.append({"type": "image_url", "image_url": image_url})
    return {"role": "user", "content": parts}


def _message_chars(message: dict) -> int:
    content = message.get("content")
    if isinstance(content, str):
        return len(content)
    total = 0
    for part in content or []:
        if part.get("type") == "text":
            total += len(part.get("text", ""))
        else:
            total += MAX_ESTIMATED_IMAGE_TOKENS * 3
    return total


class AiClient:
    def __init__(self, provider: Provider, api_key: str, *, timeout: int = 60, concurrency: int = 4, block_seconds: int = 600):
        self.provider = provider
        self.api_key = api_key
        self.timeout = timeout
        self.block_seconds = block_seconds
        self.last_error: str | None = None
        self.calls = 0              # successful provider calls since the bot started
        self.reasoning_calls = 0    # of them: calls that still billed hidden reasoning tokens

        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self._session: aiohttp.ClientSession | None = None
        self._blocked_until = 0.0
        self._extras_index = 0      # which of provider.extra_body_options is known to work

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def reasoning_switch(self) -> str:
        """The request variant for switching the reasoning off that is currently in use."""
        options = self.provider.extra_body_options
        return json.dumps(options[min(self._extras_index, len(options) - 1)], ensure_ascii=False) if options else "{}"

    def blocked_for(self) -> int:
        """Seconds the provider stays paused after a billing/auth error (0 = not paused)."""
        return max(0, int(self._blocked_until - time.monotonic()))

    def _block(self, reason: str):
        self.last_error = reason
        self._blocked_until = time.monotonic() + self.block_seconds
        print(f"❌ AI provider paused for {self.block_seconds}s: {reason}")

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout))
        return self._session

    async def close(self):
        if self._session is not None and not self._session.closed:
            await self._session.close()

    @staticmethod
    def _error_text(body: str) -> str:
        """OpenAI-style errors are {"error": {...}}; ProxyAPI's own checks answer {"detail": "..."}."""
        try:
            data = json.loads(body)
        except ValueError:
            return body[:300]

        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict):
                return f"{error.get('code') or error.get('type') or ''} {error.get('message') or ''}".strip()[:300]
            if error:
                return str(error)[:300]
            detail = data.get("detail")
            if detail:
                return (detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False))[:300]
        return body[:300]

    def _raise_for_status(self, status: int, error: str):
        lowered = error.lower()
        quota_problem = any(word in lowered for word in ("quota", "arrear", "balance", "billing", "insufficient", "budget"))

        # 402 = no money / key budget / monthly budget (ProxyAPI, DeepSeek), 401/403 = bad key, IP or model not allowed
        if status in (401, 402, 403) or (status == 429 and quota_problem):
            self._block(f"HTTP {status}: {error}")
            raise AiUnavailable(f"HTTP {status}: {error}")
        if status == 429:
            raise AiRateLimited(f"HTTP 429: {error}")
        raise AiRequestError(f"HTTP {status}: {error}", status)

    async def _post(self, payload: dict) -> dict:
        url = f"{self.provider.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        attempts = 2

        for attempt in range(attempts):
            try:
                session = await self._get_session()
                async with session.post(url, json=payload, headers=headers) as resp:
                    status = resp.status
                    body = await resp.text()
            except (aiohttp.ClientError, asyncio.TimeoutError):
                if attempt + 1 < attempts:
                    await asyncio.sleep(1.5)
                    continue
                raise

            if status == 200:
                try:
                    return json.loads(body)
                except ValueError:
                    raise AiRequestError(f"Provider returned invalid JSON: {body[:200]}") from None

            if status in (500, 502, 503, 504) and attempt + 1 < attempts:
                await asyncio.sleep(2)
                continue

            self._raise_for_status(status, self._error_text(body))

        raise AiRequestError("unreachable")  # pragma: no cover

    async def _send(self, base_payload: dict) -> dict:
        """Posts the request with the first request variant that the API accepts (see PROVIDERS)."""
        options = self.provider.extra_body_options or ({},)
        start = min(self._extras_index, len(options) - 1)
        last_error: AiRequestError | None = None

        for index in range(start, len(options)):
            try:
                data = await self._post({**base_payload, **options[index]})
            except AiRequestError as error:
                if error.status == 400 and index + 1 < len(options):
                    last_error = error
                    print(f"⚠️ AI request variant {json.dumps(options[index], ensure_ascii=False)} was rejected ({error}), trying the next one")
                    continue
                raise

            if index != self._extras_index:
                print(f"AI: the API accepted {json.dumps(options[index], ensure_ascii=False)} as the way to switch the hidden reasoning off"
                      if options[index] else "⚠️ AI: no way to switch the hidden reasoning off was accepted, it stays ON (costs more, answers are slower)")
            self._extras_index = index
            return data

        raise last_error or AiRequestError("no request variant was accepted")

    def _parse(self, data: dict, model: str, messages: list[dict]) -> ChatResult:
        try:
            choice = data["choices"][0]
            message = choice.get("message") or {}
        except (KeyError, IndexError, TypeError, AttributeError):
            raise AiRequestError(f"Response has no choices: {str(data)[:200]}") from None

        # Only `content` is the answer; `reasoning_content` (if any) is never shown or stored.
        text = (message.get("content") or "").strip()

        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)

        if not prompt_tokens and not completion_tokens:
            prompt_tokens = max(1, sum(_message_chars(m) for m in messages) // 3)
            completion_tokens = max(1, len(text) // 3)

        # DeepSeek reports cache hits as prompt_cache_hit_tokens, Qwen/OpenAI/ProxyAPI as prompt_tokens_details.cached_tokens.
        cached = usage.get("prompt_cache_hit_tokens")
        if cached is None:
            cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        cached_tokens = min(int(cached or 0), prompt_tokens)

        reasoning_tokens = int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)

        provider = self.provider
        cost = (
            (prompt_tokens - cached_tokens) * provider.price_input
            + cached_tokens * provider.price_cached
            + completion_tokens * provider.price_output
        )  # tokens * (currency / 1M tokens) = millionths of the currency

        return ChatResult(
            text=text,
            finish_reason=choice.get("finish_reason"),
            model=model,
            prompt_tokens=prompt_tokens,
            cached_tokens=cached_tokens,
            completion_tokens=completion_tokens,
            reasoning_tokens=reasoning_tokens,
            cost_micro=round(cost),
        )

    async def chat(self, messages: list[dict], *, has_images: bool = False, max_tokens: int = 600) -> ChatResult:
        if not self.configured:
            raise AiNotConfigured("AI_API_KEY is not set")

        remaining = self.blocked_for()
        if remaining:
            raise AiUnavailable(f"provider is paused for {remaining}s: {self.last_error}")

        model = self.provider.vision_model if has_images else self.provider.text_model
        base_payload = {"model": model, "messages": messages, self.provider.max_tokens_param: max_tokens, "stream": False}

        async with self._semaphore:
            data = await self._send(base_payload)

        result = self._parse(data, model, messages)
        self.calls += 1
        if result.reasoning_tokens:
            self.reasoning_calls += 1
        return result

    async def fetch_balance(self) -> dict | None:
        """ProxyAPI only: what is left on the account (or in the key's budget). Needs the "balance request"
        permission on the key. Returns None when the provider has no such API, {"error": ...} when it failed."""
        if not self.provider.balance_url or not self.configured:
            return None

        try:
            session = await self._get_session()
            async with session.get(
                self.provider.balance_url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    return {"error": f"HTTP {resp.status}"}
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            return {"error": repr(e)}


def create_client() -> AiClient:
    provider = load_provider()
    return AiClient(
        provider,
        load_api_key(provider),
        timeout=env_var_to_int("AI_REQUEST_TIMEOUT_SECONDS", "60"),
        concurrency=env_var_to_int("AI_MAX_CONCURRENCY", "4"),
        block_seconds=env_var_to_int("AI_PROVIDER_BLOCK_SECONDS", "600"),
    )
