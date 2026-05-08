"""
Retry logic with exponential backoff, screenshot on failure,
and automatic healing attempts.
"""
import time
import random
from dataclasses import dataclass
from typing import Callable, Any, Optional
from functools import wraps


@dataclass
class RetryConfig:
    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 10.0
    exponential_base: float = 2.0
    jitter: bool = True
    screenshot_on_failure: bool = True
    heal_on_failure: bool = True


def with_retries(config: Optional[RetryConfig] = None):
    if config is None:
        config = RetryConfig()

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            service = args[0] if args else None

            for attempt in range(config.max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e

                    if attempt >= config.max_retries:
                        break

                    delay = min(
                        config.base_delay * (config.exponential_base ** attempt),
                        config.max_delay,
                    )
                    if config.jitter:
                        delay = delay * (0.5 + random.random())

                    print(f"[Retry] Attempt {attempt + 1}/{config.max_retries} failed: {e}")
                    print(f"[Retry] Waiting {delay:.1f}s before retry...")

                    if config.screenshot_on_failure and service and hasattr(service, "screenshot"):
                        try:
                            path = f"failure_attempt_{attempt}.png"
                            service.screenshot(path)
                            print(f"[Retry] Screenshot saved: {path}")
                        except Exception:
                            pass

                    if config.heal_on_failure and service and hasattr(service, "heal_selector"):
                        try:
                            healed = service.heal_selector(str(e))
                            if healed:
                                print(f"[Retry] Selector healed: {healed}")
                        except Exception:
                            pass

                    time.sleep(delay)

            raise last_exception

        return wrapper
    return decorator
