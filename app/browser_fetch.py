from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .powerfull_stealth_browser import PowerfulStealthBrowser

_BASE_BROWSER_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Connection": "keep-alive",
    "DNT": "1",
}

_EXCLUDED_RESPONSE_HEADERS = {
    "connection",
    "content-encoding",
    "content-length",
    "transfer-encoding",
}


@dataclass
class BrowserFetchResult:
    body: bytes
    status_code: int
    headers: dict[str, str]
    final_url: str


def _normalize_query_value(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def build_target_url(url: str, params: Mapping[str, Any] | None = None) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("url must be an absolute URL")

    if not params:
        return url

    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    for key, value in params.items():
        query_items = [item for item in query_items if item[0] != key]
        query_items.extend((key, item) for item in _normalize_query_value(value))

    return urlunparse(parsed._replace(query=urlencode(query_items, doseq=True)))


def derive_warm_up_url(target_url: str, warm_up_url: str | None = None) -> str:
    if warm_up_url:
        parsed = urlparse(warm_up_url)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError("warm_up_url must be an absolute URL")
        return warm_up_url

    parsed = urlparse(target_url)
    return urlunparse((parsed.scheme, parsed.netloc, "/", "", "", ""))


def _build_request_headers(
    target_url: str,
    warm_up_url: str,
    headers: Mapping[str, str] | None = None,
) -> dict[str, str]:
    target = urlparse(target_url)
    warm_up = urlparse(warm_up_url)
    merged_headers = dict(_BASE_BROWSER_HEADERS)
    merged_headers["Origin"] = f"{target.scheme}://{target.netloc}"
    merged_headers["Referer"] = warm_up_url
    merged_headers["Sec-Fetch-Site"] = "same-origin" if target.netloc == warm_up.netloc else "cross-site"

    if headers:
        merged_headers.update({key: value for key, value in headers.items() if value is not None})

    return merged_headers


def _filter_response_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in _EXCLUDED_RESPONSE_HEADERS
    }


def _fallback_body(page) -> bytes:
    try:
        pre_text = page.locator("pre").first.inner_text(timeout=5_000)
        if pre_text:
            return pre_text.encode("utf-8")
    except Exception:
        pass

    try:
        body_text = page.evaluate(
            "() => document.body.innerText || document.body.textContent || ''"
        )
        return body_text.encode("utf-8")
    except Exception:
        return b""


def fetch_with_stealth_browser(
    url: str,
    params: Mapping[str, Any] | None = None,
    warm_up_url: str | None = None,
    headers: Mapping[str, str] | None = None,
    timeout_ms: int = 30_000,
    wait_until: str = "domcontentloaded",
) -> BrowserFetchResult:
    target_url = build_target_url(url, params)
    resolved_warm_up_url = derive_warm_up_url(target_url, warm_up_url)

    browser = PowerfulStealthBrowser(
        headless=True,
        locale="en-US",
        timezone="America/New_York",
        extra_stealth=True,
    )

    browser.start()
    page = browser.page
    page.set_extra_http_headers(
        _build_request_headers(target_url, resolved_warm_up_url, headers)
    )

    captured_response = None

    def on_response(response):
        nonlocal captured_response
        if response.request.is_navigation_request():
            captured_response = response

    try:
        try:
            page.goto(resolved_warm_up_url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception:
            pass

        try:
            time.sleep(random.uniform(2.5, 5.0))
            browser.human_like_scroll(amount=random.randint(300, 700))
            time.sleep(random.uniform(0.8, 2.0))
        except Exception:
            pass

        page.on("response", on_response)
        goto_response = None
        goto_error = None

        try:
            goto_response = page.goto(target_url, wait_until=wait_until, timeout=timeout_ms)
        except Exception as exc:
            goto_error = exc

        response = captured_response or goto_response
        if response is None:
            body = _fallback_body(page)
            if not body and goto_error is not None:
                raise RuntimeError(str(goto_error)) from goto_error
            return BrowserFetchResult(
                body=body,
                status_code=200,
                headers={},
                final_url=page.url,
            )

        try:
            body = response.body()
        except Exception:
            body = b""

        if not body:
            body = _fallback_body(page)

        return BrowserFetchResult(
            body=body,
            status_code=response.status,
            headers=_filter_response_headers(dict(response.headers)),
            final_url=response.url,
        )
    finally:
        try:
            browser.close()
        except Exception:
            pass
