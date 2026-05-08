"""
Browser session with anti-bot, stealth, and resilience features.
"""
from playwright.sync_api import Error as PlaywrightError, sync_playwright
import os
import random
import shutil
import time
from typing import Optional, Dict, Any


class BrowserSession:
    """
    Robust browser session with:
    - Stealth mode (anti-bot)
    - User-Agent rotation
    - Session persistence (cookies)
    - Human-like behavior
    """

    def __init__(
        self,
        headless: bool = True,
        proxy: Optional[str] = None,
        user_agent: Optional[str] = None,
        viewport: Optional[Dict[str, int]] = None,
        locale: str = "en-US",
        timezone: str = "America/New_York",
    ):
        self.headless = headless
        self.proxy = proxy
        self.user_agent = user_agent or self._random_user_agent()
        self.viewport = viewport or {"width": 1920, "height": 1080}
        self.locale = locale
        self.timezone = timezone

        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self._cookies: list = []

    def _random_user_agent(self) -> str:
        agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        ]
        return random.choice(agents)

    def start(self):
        self.playwright = sync_playwright().start()

        launch_args = {
            "headless": self.headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        }
        executable_path = os.getenv("CHROMIUM_EXECUTABLE_PATH")
        if executable_path:
            launch_args["executable_path"] = executable_path
        if self.proxy:
            launch_args["proxy"] = {"server": self.proxy}

        try:
            self.browser = self.playwright.chromium.launch(**launch_args)
        except PlaywrightError as exc:
            message = str(exc)
            system_chromium = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
            if ("Executable doesn't exist" in message or "playwright install" in message.lower()) and system_chromium:
                launch_args["executable_path"] = system_chromium
                self.browser = self.playwright.chromium.launch(**launch_args)
            elif "Executable doesn't exist" in message or "playwright install" in message.lower():
                raise RuntimeError(
                    "Playwright browser binaries are missing. Run: playwright install chromium, "
                    "or set CHROMIUM_EXECUTABLE_PATH=/path/to/chromium."
                ) from exc
            else:
                raise

        context_options = {
            "viewport": self.viewport,
            "user_agent": self.user_agent,
            "locale": self.locale,
            "timezone_id": self.timezone,
            "permissions": ["geolocation"],
            "color_scheme": "light",
        }

        self.context = self.browser.new_context(**context_options)

        # Inject anti-detection scripts
        self.context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            window.chrome = { runtime: {} };
        """)

        self.page = self.context.new_page()

        # Restore cookies if any
        if self._cookies:
            self.context.add_cookies(self._cookies)

        return self

    def goto(self, url: str, wait_until: str = "domcontentloaded", timeout: int = 60000):
        """Navigate with human-like delays."""
        self.page.goto(url, wait_until=wait_until, timeout=timeout)
        time.sleep(random.uniform(1.5, 3.5))
        return self.page

    def human_like_scroll(self, amount: int = 800):
        """Scroll with random increments."""
        steps = random.randint(3, 6)
        for _ in range(steps):
            increment = amount // steps + random.randint(-50, 50)
            self.page.evaluate(f"window.scrollBy(0, {increment})")
            time.sleep(random.uniform(0.1, 0.3))

    def screenshot(self, path: str = "debug.png"):
        self.page.screenshot(path=path, full_page=True)
        return path

    def save_state(self):
        """Save cookies and storage state."""
        self._cookies = self.context.cookies()
        return self._cookies

    def restore_state(self, cookies: list):
        """Restore cookies and storage state."""
        self._cookies = cookies
        if self.context:
            self.context.add_cookies(cookies)

    def close(self):
        if self.context:
            self.save_state()
            self.context.close()
        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()
