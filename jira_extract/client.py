"""
jira_extract/client.py
----------------------
A robust JIRA REST API client utilizing connection-pooling (requests.Session)
and unified exponential backoff retry logic for sequential requests.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

import requests

from . import config

MAX_RETRIES = 5


class JiraClient:
    """Session-based JIRA API Client with built-in retry and backoff mechanisms."""

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
        })
        self.logger = logger or logging.getLogger("jira_client")

    def request(self, method: str, path_or_url: str, **kwargs) -> Optional[dict]:
        """Perform a request with persistent Keep-Alive and exponential backoff retry."""
        if path_or_url.startswith("http"):
            url = path_or_url
        else:
            url = f"{config.BASE_URL}/{path_or_url.lstrip('/')}"

        kwargs.setdefault("timeout", 60)

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self.session.request(method, url, **kwargs)

                if resp.status_code == 200:
                    return resp.json()

                if resp.status_code == 404:
                    self.logger.warning("404 (Resource unavailable or deleted): %s", url)
                    return None

                if resp.status_code in (429, 502, 503, 504):
                    wait = min(60, 2 ** attempt)
                    self.logger.warning("HTTP %d on attempt %d. Waiting %ds.", resp.status_code, attempt, wait)
                    time.sleep(wait)
                    continue

                self.logger.error("HTTP %d (not retrying): %s", resp.status_code, url)
                return None

            except Exception as exc:
                wait = min(60, 2 ** attempt)
                self.logger.warning("Exception on attempt %d: %s. Waiting %ds.", attempt, exc, wait)
                time.sleep(wait)

        self.logger.error("Max retries exceeded: %s", url)
        return None

    def get(self, path_or_url: str, **kwargs) -> Optional[dict]:
        """Perform a GET request."""
        return self.request("GET", path_or_url, **kwargs)

    def post(self, path_or_url: str, payload: Dict[str, Any], **kwargs) -> Optional[dict]:
        """Perform a POST request."""
        return self.request("POST", path_or_url, json=payload, **kwargs)

    def close(self) -> None:
        """Close the underlying requests session."""
        self.session.close()

    def __enter__(self) -> JiraClient:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
