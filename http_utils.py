"""
Shared HTTP helper. A single flaky request (timeout, brief 5xx) shouldn't
mean a whole user request fails — one quick retry fixes most of that.
"""
import logging
import time
import requests

log = logging.getLogger(__name__)


def post_with_retry(url: str, json: dict, headers: dict, timeout: int = 15,
                     retries: int = 1, backoff_seconds: int = 2):
    last_exc = None
    for attempt in range(retries + 1):
        try:
            resp = requests.post(url, json=json, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp
        except Exception as e:
            last_exc = e
            if attempt < retries:
                log.warning("Request to %s failed (attempt %d/%d): %s — retrying in %ds",
                            url, attempt + 1, retries + 1, e, backoff_seconds)
                time.sleep(backoff_seconds)
    raise last_exc
