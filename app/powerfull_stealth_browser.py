"""
================================================================================
EXTRA EXTRA POWERFUL STEALTH BROWSER v3.0
For TinyFish2 - Maximum Anti-Detection + Cloudflare resoultion
================================================================================
"""

from __future__ import annotations
import os
import random
import time
from typing import Optional, Dict, Any
from playwright.sync_api import sync_playwright, Error as PlaywrightError


class PowerfulStealthBrowser:
    def __init__(
        self,
        headless: bool = True,
        proxy: Optional[str] = None,
        user_agent: Optional[str] = None,
        viewport: Optional[Dict[str, int]] = None,
        locale: str = "en-US",
        timezone: str = "America/New_York",
        extra_stealth: bool = True,
    ):
        self.headless = headless
        self.proxy = proxy
        self.user_agent = user_agent or self._get_realistic_user_agent()
        self.viewport = viewport or {"width": 1920, "height": 1080}
        self.locale = locale
        self.timezone = timezone
        self.extra_stealth = extra_stealth

        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self._cookies = []

    def _get_realistic_user_agent(self) -> str:
        agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
        ]
        return random.choice(agents)

    def _get_strong_stealth_script(self) -> str:
        return """
            // === CORE STEALTH ===
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
            window.chrome = { runtime: {} };

            // === WEBGL SPOOFING ===
            const getParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(parameter) {
                if (parameter === 37445) return 'Intel Inc.';
                if (parameter === 37446) return 'Intel Iris OpenGL Engine';
                return getParameter.apply(this, arguments);
            };

            // === CANVAS SPOOFING ===
            const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
            HTMLCanvasElement.prototype.toDataURL = function(type) {
                const ctx = this.getContext('2d');
                if (ctx) {
                    ctx.fillStyle = 'rgba(0,0,0,0.01)';
                    ctx.fillRect(0, 0, 1, 1);
                }
                return originalToDataURL.apply(this, arguments);
            };

            // === AUDIO SPOOFING ===
            const originalGetChannelData = AudioBuffer.prototype.getChannelData;
            AudioBuffer.prototype.getChannelData = function(channel) {
                const data = originalGetChannelData.call(this, channel);
                for (let i = 0; i < data.length; i++) {
                    data[i] = data[i] + (Math.random() * 0.0001 - 0.00005);
                }
                return data;
            };

            // === FONTS & SCREEN ===
            Object.defineProperty(screen, 'width', { get: () => 1920 });
            Object.defineProperty(screen, 'height', { get: () => 1080 });
            Object.defineProperty(screen, 'availWidth', { get: () => 1920 });
            Object.defineProperty(screen, 'availHeight', { get: () => 1040 });

            // === EXTRA ANTI-DETECTION ===
            delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
            delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
            delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;
        """

    def start(self):
        self.playwright = sync_playwright().start()

        launch_args = {
            "headless": self.headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process,SitePerProcess",
                "--disable-site-isolation-trials",
                "--disable-web-security",
                "--disable-setuid-sandbox",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-accelerated-2d-canvas",
                "--no-first-run",
                "--no-zygote",
                "--disable-gpu",
                "--single-process",
            ],
        }

        if self.proxy:
            launch_args["proxy"] = {"server": self.proxy}

        executable_path = os.getenv("CHROMIUM_EXECUTABLE_PATH")
        if executable_path:
            launch_args["executable_path"] = executable_path

        try:
            self.browser = self.playwright.chromium.launch(**launch_args)
        except PlaywrightError as exc:
            if "Executable doesn't exist" in str(exc):
                raise RuntimeError("Run: playwright install chromium") from exc
            raise

        context_options = {
            "viewport": self.viewport,
            "user_agent": self.user_agent,
            "locale": self.locale,
            "timezone_id": self.timezone,
            "permissions": ["geolocation"],
            "color_scheme": "light",
            "ignore_https_errors": True,
        }

        self.context = self.browser.new_context(**context_options)

        if self.extra_stealth:
            self.context.add_init_script(self._get_strong_stealth_script())
            # playwright-stealth: deeper fingerprint patches (WebGL, canvas, navigator)
            try:
                from playwright_stealth import Stealth
                Stealth().apply_stealth_sync(self.context)
            except ImportError:
                pass  # optional dep

        self.page = self.context.new_page()

        if self._cookies:
            self.context.add_cookies(self._cookies)

        return self

    def goto(self, url: str, wait_until: str = "domcontentloaded", timeout: int = 60000):
        self.page.goto(url, wait_until=wait_until, timeout=timeout)
        time.sleep(random.uniform(1.2, 3.8))

        # Auto Cloudflare handling
        try:
            from .cloudflare_handler import handle_cloudflare
            handle_cloudflare(self.page, max_wait=60, verbose=False)
        except ImportError:
            pass

        return self.page

    def human_like_scroll(self, amount: int = 800):
        steps = random.randint(4, 9)
        for _ in range(steps):
            increment = amount // steps + random.randint(-80, 80)
            self.page.evaluate(f"window.scrollBy(0, {increment})")
            time.sleep(random.uniform(0.12, 0.45))

    def human_like_type(self, selector: str, text: str, delay_range=(40, 120)):
        element = self.page.locator(selector).first
        element.click()
        time.sleep(random.uniform(0.2, 0.6))
        for char in text:
            element.type(char, delay=random.randint(*delay_range))
            if random.random() < 0.08:
                time.sleep(random.uniform(0.3, 0.8))

    def screenshot(self, path: str = "stealth_screenshot.png"):
        self.page.screenshot(path=path, full_page=True)
        return path

    def save_cookies(self):
        self._cookies = self.context.cookies()
        return self._cookies

    def close(self):
        if self.context:
            self.save_cookies()
            self.context.close()
        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()


# ==================== QUICK TEST ====================
if __name__ == "__main__":
    browser = PowerfulStealthBrowser(headless=False).start()
    page = browser.goto("https://example.com")
    print("URL:", page.url)
    browser.close()
