"""
Groq chat client with multi-key rotation and conservative local rate limiting.

Environment variables:
    GROQ_API_KEYS          Comma/newline/space separated API keys. Preferred.
    GROQ_API_KEY           Single API key fallback.
    GROQ_API_KEY_1...N     Optional numbered keys; appended after GROQ_API_KEYS.
    GROQ_MODEL             Default chat model.
    GROQ_RPM               Requests per minute per configured key/project bucket.
    GROQ_TPM               Tokens per minute per configured key/project bucket.
    GROQ_RPD               Requests per day per configured key/project bucket.
    GROQ_TPD               Tokens per day per configured key/project bucket.
    GROQ_BASE_URL          Defaults to https://api.groq.com/openai/v1.

Important: Groq rate limits are normally organization/project scoped. Multiple API
keys from the same Groq project usually share the same upstream bucket. This client
still tracks each configured key locally so it can fail over between independent
keys/projects and honor retry-after/cooldown headers when a bucket is exhausted.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
import re
import threading
import time
from typing import Any, Deque, Dict, Iterable, List, Optional, Sequence, Tuple

import httpx


DEFAULT_GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
DEFAULT_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


DEFAULT_RPM = _env_int("GROQ_RPM", 30)
DEFAULT_TPM = _env_int("GROQ_TPM", 6000)
DEFAULT_RPD = _env_int("GROQ_RPD", 14400)
DEFAULT_TPD = _env_int("GROQ_TPD", 500000)


class GroqClientError(RuntimeError):
    """Base error for the routed Groq client."""


class GroqConfigurationError(GroqClientError):
    """Raised when no usable Groq key/configuration is available."""


class AllGroqKeysRateLimited(GroqClientError):
    """Raised when every configured key is currently exhausted or cooling down."""

    def __init__(self, retry_after: float, statuses: Sequence[Dict[str, Any]]):
        self.retry_after = max(0.0, retry_after)
        self.statuses = list(statuses)
        super().__init__(
            f"All Groq API keys are rate limited. Retry after about {self.retry_after:.1f}s."
        )


@dataclass
class GroqResponse:
    content: str
    model: str
    key_name: str
    usage: Dict[str, Any] = field(default_factory=dict)
    headers: Dict[str, str] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)


_DURATION_RE = re.compile(r"(?P<value>\d+(?:\.\d+)?)(?P<unit>ms|s|m|h|d)")


def parse_duration_seconds(value: Optional[str]) -> Optional[float]:
    """Parse Groq style reset durations such as '7.66s', '2m59.56s', '1h2m'."""
    if not value:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass

    total = 0.0
    for match in _DURATION_RE.finditer(text):
        amount = float(match.group("value"))
        unit = match.group("unit")
        if unit == "ms":
            total += amount / 1000.0
        elif unit == "s":
            total += amount
        elif unit == "m":
            total += amount * 60.0
        elif unit == "h":
            total += amount * 3600.0
        elif unit == "d":
            total += amount * 86400.0
    return total if total > 0 else None


def _split_keys(raw: str) -> List[str]:
    parts = re.split(r"[,\s]+", raw.strip())
    return [p.strip() for p in parts if p.strip()]


def load_groq_api_keys_from_env() -> List[str]:
    keys: List[str] = []
    raw_multi = os.getenv("GROQ_API_KEYS", "")
    if raw_multi:
        keys.extend(_split_keys(raw_multi))

    raw_single = os.getenv("GROQ_API_KEY", "")
    if raw_single:
        keys.extend(_split_keys(raw_single))

    numbered: List[Tuple[int, str]] = []
    for name, value in os.environ.items():
        m = re.fullmatch(r"GROQ_API_KEY_(\d+)", name)
        if m and value.strip():
            numbered.append((int(m.group(1)), value.strip()))
    for _, value in sorted(numbered):
        keys.extend(_split_keys(value))

    # Keep order but remove duplicates.
    seen = set()
    unique = []
    for key in keys:
        if key not in seen:
            unique.append(key)
            seen.add(key)
    return unique


def redact_key(key: str) -> str:
    if len(key) <= 10:
        return "****"
    return f"{key[:6]}...{key[-4:]}"


def estimate_tokens_from_messages(messages: Sequence[Dict[str, Any]], max_tokens: int = 0) -> int:
    """
    Conservative token estimator.

    It intentionally avoids model-specific tokenizers so the client remains light.
    For English-like text, 1 token ~= 4 characters is a reasonable estimate; we add
    overhead for roles/message boundaries and include max_tokens for output budget.
    """
    chars = 0
    message_count = 0
    for message in messages:
        message_count += 1
        chars += len(str(message.get("role", ""))) + 8
        content = message.get("content", "")
        if isinstance(content, str):
            chars += len(content)
        else:
            chars += len(json.dumps(content, ensure_ascii=False))
    estimated_prompt = max(1, (chars + 3) // 4 + message_count * 6)
    return estimated_prompt + max(0, int(max_tokens or 0))


@dataclass
class KeyLimits:
    rpm: int = DEFAULT_RPM
    tpm: int = DEFAULT_TPM
    rpd: int = DEFAULT_RPD
    tpd: int = DEFAULT_TPD


@dataclass
class KeyState:
    api_key: str
    key_name: str
    limits: KeyLimits
    request_times: Deque[float] = field(default_factory=deque)
    token_times: Deque[Tuple[float, int]] = field(default_factory=deque)
    day: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    day_requests: int = 0
    day_tokens: int = 0
    cooldown_until: float = 0.0
    cooldown_reason: str = ""
    last_error: Optional[str] = None
    last_headers: Dict[str, str] = field(default_factory=dict)
    last_usage: Dict[str, Any] = field(default_factory=dict)

    def _reset_if_needed(self, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        cutoff = now - 60.0
        while self.request_times and self.request_times[0] < cutoff:
            self.request_times.popleft()
        while self.token_times and self.token_times[0][0] < cutoff:
            self.token_times.popleft()

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.day != today:
            self.day = today
            self.day_requests = 0
            self.day_tokens = 0
            self.cooldown_reason = ""
            self.cooldown_until = 0.0

    def minute_tokens(self) -> int:
        return sum(tokens for _, tokens in self.token_times)

    def availability(self, estimated_tokens: int) -> Tuple[bool, float, str]:
        self._reset_if_needed()
        now = time.time()
        waits: List[Tuple[float, str]] = []

        if self.cooldown_until > now:
            waits.append((self.cooldown_until - now, self.cooldown_reason or "cooldown"))

        if self.limits.rpm and len(self.request_times) >= self.limits.rpm:
            waits.append((max(0.0, 60.0 - (now - self.request_times[0])), "local RPM"))

        if self.limits.tpm and self.minute_tokens() + estimated_tokens > self.limits.tpm:
            if self.token_times:
                waits.append((max(0.0, 60.0 - (now - self.token_times[0][0])), "local TPM"))
            else:
                waits.append((60.0, "local TPM"))

        if self.limits.rpd and self.day_requests + 1 > self.limits.rpd:
            waits.append((seconds_until_next_utc_day(), "local RPD"))

        if self.limits.tpd and self.day_tokens + estimated_tokens > self.limits.tpd:
            waits.append((seconds_until_next_utc_day(), "local TPD"))

        if waits:
            wait, reason = min(waits, key=lambda item: item[0])
            return False, wait, reason
        return True, 0.0, "ok"

    def reserve(self, estimated_tokens: int) -> None:
        self._reset_if_needed()
        now = time.time()
        self.request_times.append(now)
        self.token_times.append((now, max(1, estimated_tokens)))
        self.day_requests += 1
        self.day_tokens += max(1, estimated_tokens)

    def update_from_headers(self, headers: httpx.Headers | Dict[str, str]) -> None:
        normalized = {str(k).lower(): str(v) for k, v in dict(headers).items()}
        interesting = {
            key: value
            for key, value in normalized.items()
            if key.startswith("x-ratelimit") or key == "retry-after"
        }
        if interesting:
            self.last_headers = interesting

        limit_tokens = _parse_int_header(normalized.get("x-ratelimit-limit-tokens"))
        if limit_tokens and limit_tokens > 0:
            self.limits.tpm = limit_tokens

        # Groq documents x-ratelimit-limit-requests as RPD, not RPM.
        limit_requests = _parse_int_header(normalized.get("x-ratelimit-limit-requests"))
        if limit_requests and limit_requests > 0:
            self.limits.rpd = limit_requests

        remaining_tokens = _parse_int_header(normalized.get("x-ratelimit-remaining-tokens"))
        if remaining_tokens is not None and remaining_tokens <= 0:
            reset = parse_duration_seconds(normalized.get("x-ratelimit-reset-tokens"))
            if reset is not None:
                self.cooldown_until = max(self.cooldown_until, time.time() + reset)
                self.cooldown_reason = "Groq TPM header"

        remaining_requests = _parse_int_header(normalized.get("x-ratelimit-remaining-requests"))
        if remaining_requests is not None and remaining_requests <= 0:
            reset = parse_duration_seconds(normalized.get("x-ratelimit-reset-requests"))
            if reset is not None:
                self.cooldown_until = max(self.cooldown_until, time.time() + reset)
                self.cooldown_reason = "Groq request header"

    def mark_rate_limited(self, headers: httpx.Headers | Dict[str, str], fallback_retry_after: float = 30.0) -> None:
        self.update_from_headers(headers)
        normalized = {str(k).lower(): str(v) for k, v in dict(headers).items()}
        retry_after = parse_duration_seconds(normalized.get("retry-after"))
        token_reset = parse_duration_seconds(normalized.get("x-ratelimit-reset-tokens"))
        request_reset = parse_duration_seconds(normalized.get("x-ratelimit-reset-requests"))
        wait = retry_after or token_reset or request_reset or fallback_retry_after
        self.cooldown_until = max(self.cooldown_until, time.time() + wait)
        self.cooldown_reason = "Groq 429"
        self.last_error = f"429 rate limited; retry_after={wait:.1f}s"

    def mark_transient_error(self, message: str, retry_after: float = 2.0) -> None:
        self.cooldown_until = max(self.cooldown_until, time.time() + retry_after)
        self.cooldown_reason = "transient error"
        self.last_error = message

    def status(self) -> Dict[str, Any]:
        self._reset_if_needed()
        ok, wait, reason = self.availability(0)
        return {
            "key": self.key_name,
            "available": ok,
            "wait_seconds": round(wait, 2),
            "reason": reason,
            "rpm_used": len(self.request_times),
            "tpm_used_estimate": self.minute_tokens(),
            "rpd_used_estimate": self.day_requests,
            "tpd_used_estimate": self.day_tokens,
            "limits": {
                "rpm": self.limits.rpm,
                "tpm": self.limits.tpm,
                "rpd": self.limits.rpd,
                "tpd": self.limits.tpd,
            },
            "last_error": self.last_error,
            "last_headers": self.last_headers,
            "last_usage": self.last_usage,
        }


def _parse_int_header(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    multiplier = 1
    if text.lower().endswith("k"):
        multiplier = 1000
        text = text[:-1]
    try:
        return int(float(text) * multiplier)
    except ValueError:
        return None


def seconds_until_next_utc_day() -> float:
    now = datetime.now(timezone.utc)
    next_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    next_day = next_day.replace(day=next_day.day)  # keep mypy/simple linters happy
    # Avoid calendar edge cases by using epoch arithmetic.
    tomorrow_epoch = ((int(now.timestamp()) // 86400) + 1) * 86400
    return max(1.0, tomorrow_epoch - now.timestamp())


class GroqRouterClient:
    """HTTP-based Groq client with key rotation and local quota accounting."""

    def __init__(
        self,
        api_keys: Optional[Iterable[str]] = None,
        model: str = DEFAULT_GROQ_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        limits: Optional[KeyLimits] = None,
        timeout: float = 60.0,
        max_failover_attempts: Optional[int] = None,
        sleep_when_all_limited: bool = False,
        max_rate_limit_sleep: float = 10.0,
    ):
        keys = list(api_keys if api_keys is not None else load_groq_api_keys_from_env())
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.sleep_when_all_limited = sleep_when_all_limited
        self.max_rate_limit_sleep = max(0.0, float(max_rate_limit_sleep))
        self._lock = threading.RLock()
        self._client = httpx.Client(timeout=timeout)
        self._states: List[KeyState] = []
        self._cursor = 0

        base_limits = limits or KeyLimits()
        for index, key in enumerate(keys, start=1):
            cloned_limits = KeyLimits(
                rpm=base_limits.rpm,
                tpm=base_limits.tpm,
                rpd=base_limits.rpd,
                tpd=base_limits.tpd,
            )
            self._states.append(KeyState(api_key=key, key_name=f"groq-key-{index}:{redact_key(key)}", limits=cloned_limits))

        self.max_failover_attempts = max_failover_attempts or max(1, len(self._states) * 2)

    @property
    def configured(self) -> bool:
        return bool(self._states)

    def close(self) -> None:
        self._client.close()

    def status(self) -> Dict[str, Any]:
        return {
            "configured": self.configured,
            "model": self.model,
            "base_url": self.base_url,
            "keys": [state.status() for state in self._states],
        }

    def _select_state(self, estimated_tokens: int) -> KeyState:
        if not self._states:
            raise GroqConfigurationError(
                "No Groq API keys configured. Set GROQ_API_KEYS or GROQ_API_KEY."
            )

        best_wait = float("inf")
        best_reason = "unknown"
        statuses = []
        count = len(self._states)
        for offset in range(count):
            idx = (self._cursor + offset) % count
            state = self._states[idx]
            ok, wait, reason = state.availability(estimated_tokens)
            statuses.append(state.status())
            if ok:
                self._cursor = (idx + 1) % count
                return state
            if wait < best_wait:
                best_wait = wait
                best_reason = reason

        if self.sleep_when_all_limited and best_wait <= self.max_rate_limit_sleep:
            time.sleep(max(0.0, best_wait))
            return self._select_state(estimated_tokens)

        for s in statuses:
            if not s.get("reason"):
                s["reason"] = best_reason
        raise AllGroqKeysRateLimited(best_wait, statuses)

    def chat(
        self,
        messages: Sequence[Dict[str, Any]],
        model: Optional[str] = None,
        temperature: float = 0.1,
        max_tokens: int = 800,
        response_format: Optional[Dict[str, Any]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> GroqResponse:
        """Create a chat completion using the first available key."""
        selected_model = model or self.model
        estimated_tokens = estimate_tokens_from_messages(messages, max_tokens=max_tokens)
        last_error: Optional[BaseException] = None
        attempts = 0

        while attempts < self.max_failover_attempts:
            attempts += 1
            try:
                with self._lock:
                    state = self._select_state(estimated_tokens)
                    state.reserve(estimated_tokens)
            except AllGroqKeysRateLimited:
                raise

            payload: Dict[str, Any] = {
                "model": selected_model,
                "messages": list(messages),
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if response_format:
                payload["response_format"] = response_format
            if extra:
                payload.update(extra)

            try:
                response = self._client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {state.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                state.update_from_headers(response.headers)

                if response.status_code == 429:
                    state.mark_rate_limited(response.headers)
                    last_error = GroqClientError(f"{state.key_name} hit Groq 429")
                    continue

                if response.status_code >= 500:
                    message = f"Groq server error {response.status_code}: {response.text[:300]}"
                    state.mark_transient_error(message)
                    last_error = GroqClientError(message)
                    continue

                if response.status_code in (401, 403):
                    # Invalid / revoked key — permanently ban it and try the next one.
                    message = f"Groq API error {response.status_code} for {state.key_name}: {response.text[:200]}"
                    state.last_error = message
                    state.cooldown_until = time.time() + 86400 * 365  # effectively disabled
                    state.cooldown_reason = f"permanent ban: HTTP {response.status_code}"
                    last_error = GroqClientError(message)
                    continue

                if response.status_code >= 400:
                    message = f"Groq API error {response.status_code}: {response.text[:800]}"
                    state.last_error = message
                    raise GroqClientError(message)

                try:
                    data = response.json()
                except (ValueError, json.JSONDecodeError):
                    body_preview = response.text[:300] if response.text else "(empty)"
                    message = (
                        f"Groq returned non-JSON body "
                        f"(status={response.status_code}): {body_preview!r}"
                    )
                    state.mark_transient_error(message)
                    last_error = GroqClientError(message)
                    continue
                usage = data.get("usage", {}) or {}
                state.last_usage = usage
                actual_total = usage.get("total_tokens")
                if isinstance(actual_total, int) and actual_total > estimated_tokens:
                    # Add the difference so local tracking remains conservative.
                    delta = actual_total - estimated_tokens
                    now = time.time()
                    state.token_times.append((now, delta))
                    state.day_tokens += delta

                content = ""
                choices = data.get("choices", [])
                if choices:
                    message = choices[0].get("message", {}) or {}
                    content = message.get("content") or ""
                return GroqResponse(
                    content=content,
                    model=data.get("model", selected_model),
                    key_name=state.key_name,
                    usage=usage,
                    headers=state.last_headers,
                    raw=data,
                )
            except httpx.TimeoutException as exc:
                state.mark_transient_error(f"Groq timeout: {exc}")
                last_error = exc
                continue
            except httpx.RequestError as exc:
                state.mark_transient_error(f"Groq request error: {exc}")
                last_error = exc
                continue

        if last_error:
            raise GroqClientError(f"Groq request failed after failover attempts: {last_error}")
        raise GroqClientError("Groq request failed for an unknown reason.")


_default_client: Optional[GroqRouterClient] = None
_default_lock = threading.Lock()


def get_default_groq_client() -> GroqRouterClient:
    global _default_client
    with _default_lock:
        if _default_client is None:
            _default_client = GroqRouterClient()
        return _default_client
