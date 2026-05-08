"""
fetch_trustpilot.py
───────────────────
Uses TinyFish2's PowerfulStealthBrowser to hit the Trustpilot internal API
endpoint and return the raw JSON, bypassing AWS WAF bot-detection.

AWS WAF bypass strategy:
  1. PowerfulStealthBrowser — full stealth stack (no webdriver leak, canvas/audio
     noise, playwright-stealth fingerprint patches, realistic UA).
  2. Warm-up visit — land on trustpilot.com homepage first so the session
     accumulates real first-party cookies that WAF rules expect.
  3. Browser-grade headers — sec-fetch-*, sec-ch-ua, Accept-Encoding, Referer,
     Origin etc. injected via extra_http_headers so every request looks
     indistinguishable from Chrome 124 on macOS.
  4. Network interception — capture the raw XHR/fetch response bytes before
     the browser renders them.
  5. Human timing — random delays between warm-up and the actual API request.

Usage:
    cd tinyFish2
    python fetch_trustpilot.py
"""

from __future__ import annotations

import json
import os
import random
import sys
import time

# Allow direct execution from inside tinyFish2/
sys.path.insert(0, os.path.dirname(__file__))

from app.powerfull_stealth_browser import PowerfulStealthBrowser

# ── Target ────────────────────────────────────────────────────────────────────
WARM_UP_URL = "https://www.trustpilot.com/"
TARGET_URL  = (
    "https://www.trustpilot.com/api/consumersitesearch-api/businessunits/search"
    "?country=US&page=1&pageSize=100&query=no"
)

# ── Headers that Chrome 124 sends on macOS ─────────────────────────────────
# These are the headers AWS WAF rules commonly check.
BROWSER_HEADERS = {
    "Accept":             "application/json, text/plain, */*",
    "Accept-Language":    "en-US,en;q=0.9",
    "Accept-Encoding":    "gzip, deflate, br",
    "sec-ch-ua":          '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile":   "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Sec-Fetch-Dest":     "empty",
    "Sec-Fetch-Mode":     "cors",
    "Sec-Fetch-Site":     "same-origin",
    "Referer":            "https://www.trustpilot.com/",
    "Origin":             "https://www.trustpilot.com",
    "Connection":         "keep-alive",
    "DNT":                "1",
}


def fetch_json_via_browser(api_url: str) -> dict:
    browser = PowerfulStealthBrowser(
        headless=True,
        locale="en-US",
        timezone="America/New_York",
        extra_stealth=True,
    )

    browser.start()
    page = browser.page

    # ── Step 1: inject browser-grade headers ──────────────────────────────
    page.set_extra_http_headers(BROWSER_HEADERS)

    # ── Step 2: wire up response interceptor ──────────────────────────────
    captured: dict = {}

    def on_response(response):
        if "consumersitesearch-api/businessunits/search" in response.url:
            try:
                captured["data"]   = response.json()
                captured["status"] = response.status
                captured["url"]    = response.url
                captured["headers"]= dict(response.headers)
            except Exception:
                captured["raw"]    = response.text()
                captured["status"] = response.status
                captured["url"]    = response.url

    page.on("response", on_response)

    try:
        # ── Step 3: warm-up — visit homepage to acquire cookies ───────────
        print(f"[TinyFish] Warm-up  → {WARM_UP_URL}")
        try:
            page.goto(WARM_UP_URL, wait_until="domcontentloaded", timeout=30_000)
        except Exception as e:
            print(f"[TinyFish] Warm-up warning (non-fatal): {e}")

        # Human-like pause after landing on homepage
        delay = random.uniform(2.5, 5.0)
        print(f"[TinyFish] Pausing {delay:.1f}s (human timing) …")
        time.sleep(delay)

        # Small scroll to mimic reading
        browser.human_like_scroll(amount=random.randint(300, 700))
        time.sleep(random.uniform(0.8, 2.0))

        # ── Step 4: hit the API endpoint (same browser session / cookies) ─
        print(f"[TinyFish] Fetching → {api_url}")
        try:
            page.goto(api_url, wait_until="networkidle", timeout=30_000)
        except Exception as e:
            print(f"[TinyFish] API goto warning (non-fatal): {e}")

        # Give the interceptor a moment to fire
        time.sleep(2)

        # ── Step 5: fallback — read body text if interception missed ──────
        if not captured:
            print("[TinyFish] Interceptor miss — reading page body …")
            body = page.evaluate(
                "() => document.body.innerText || document.body.textContent"
            )
            try:
                captured["data"]   = json.loads(body)
                captured["status"] = 200
                captured["url"]    = page.url
            except json.JSONDecodeError:
                # Last resort: grab pre-formatted JSON Chromium renders in its
                # built-in JSON viewer (it wraps content in <pre>)
                try:
                    pre = page.locator("pre").first.inner_text(timeout=5_000)
                    captured["data"]   = json.loads(pre)
                    captured["status"] = 200
                    captured["url"]    = page.url
                except Exception:
                    captured["raw"]    = body
                    captured["status"] = None
                    captured["url"]    = page.url

    finally:
        browser.close()

    return captured


def main():
    result = fetch_json_via_browser(TARGET_URL)

    status = result.get("status", "unknown")
    url    = result.get("url", TARGET_URL)

    print(f"\n{'═'*60}")
    print(f"  HTTP status : {status}")
    print(f"  Final URL   : {url}")

    resp_headers = result.get("headers", {})
    if resp_headers:
        waf = resp_headers.get("x-amzn-waf-action") or resp_headers.get("x-amzn-requestid", "")
        if waf:
            print(f"  AWS WAF     : {waf}")
    print(f"{'═'*60}\n")

    if "data" in result:
        data = result["data"]

        if isinstance(data, dict):
            keys = list(data.keys())
            print(f"Top-level keys: {keys}\n")

            for key in ("businessUnits", "results", "data", "items", "hits"):
                if key in data:
                    units = data[key]
                    count = len(units) if isinstance(units, list) else "N/A"
                    print(f"  '{key}' → {count} items found")
                    if isinstance(units, list) and units:
                        print(f"\n  First item preview:")
                        print(json.dumps(units[0], indent=2, ensure_ascii=False)[:1500])
                    break

        print(f"\n{'─'*60}")
        print("Full JSON (first 4000 chars):")
        print("─"*60)
        pretty = json.dumps(data, indent=2, ensure_ascii=False)
        print(pretty[:4000])
        if len(pretty) > 4000:
            print(f"\n… (truncated, total {len(pretty):,} chars)")

        out_path = os.path.join(os.path.dirname(__file__), "trustpilot_response.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"\n[TinyFish] Full response saved → {out_path}")

    elif "raw" in result:
        print("Raw body (not JSON — WAF may have blocked):")
        print(result["raw"][:3000])

        out_path = os.path.join(os.path.dirname(__file__), "trustpilot_raw.html")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(result["raw"])
        print(f"[TinyFish] Raw body saved → {out_path}")


if __name__ == "__main__":
    main()
