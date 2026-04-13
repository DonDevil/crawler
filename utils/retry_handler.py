"""Retry helpers for network operations."""

from __future__ import annotations

from typing import Callable

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential, RetryError


def retry_on_exception(max_attempts: int = 3):
    """Return a decorator that retries on any exception."""

    def decorator(func: Callable):
        @retry(
            retry=retry_if_exception_type(Exception),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            stop=stop_after_attempt(max_attempts),
            reraise=True,
        )
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)

        return wrapper

    return decorator


def run_with_retries(func: Callable, *args, max_attempts: int = 3, **kwargs):
    """Run a callable with retries and return the result.

    Raises RetryError if all retries fail.
    """

    decorated = retry_on_exception(max_attempts=max_attempts)(func)
    return decorated(*args, **kwargs)
