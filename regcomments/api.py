"""Thin client for the Regulations.gov v4 API (https://open.gsa.gov/api/regulationsgov/)."""

from __future__ import annotations

import time
from typing import Any, Callable, Iterator

import requests

from . import __version__
from .textutil import to_eastern_filter_value

API_BASE = "https://api.regulations.gov/v4"
USER_AGENT = f"regcomments/{__version__} (+https://github.com/9hn6w9bzyk-wq/idkgirl)"


class ApiError(Exception):
    """Raised when the API returns an unrecoverable error."""


class NotFound(ApiError):
    """Raised when the requested resource does not exist."""


def _log_stderr(message: str) -> None:
    import sys

    print(message, file=sys.stderr, flush=True)


class RegulationsGovClient:
    # The comments endpoint returns at most 250 results per page and at most
    # 20 pages (5,000 results) per query. Larger sets are walked by re-querying
    # with a lastModifiedDate lower bound (the approach recommended by the
    # Regulations.gov API documentation).
    page_size = 250
    max_pages = 20

    def __init__(
        self,
        api_key: str,
        *,
        session: requests.Session | None = None,
        base_url: str = API_BASE,
        min_interval: float = 0.0,
        max_retries: int = 10,
        rate_limit_wait: float = 60.0,
        max_rate_limit_wait: float = 900.0,
        timeout: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = _log_stderr,
    ) -> None:
        self.api_key = api_key
        self.session = session or requests.Session()
        self.base_url = base_url.rstrip("/")
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.rate_limit_wait = rate_limit_wait
        self.max_rate_limit_wait = max_rate_limit_wait
        self.timeout = timeout
        self.sleep = sleep
        self.log = log
        self.request_count = 0
        self.rate_limit_remaining: int | None = None
        self._last_request = 0.0

    # ------------------------------------------------------------------ HTTP

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        url = f"{self.base_url}{path}"
        headers = {"X-Api-Key": self.api_key, "User-Agent": USER_AGENT}
        rate_limit_hits = 0
        errors = 0
        while True:
            if self.min_interval:
                wait = self._last_request + self.min_interval - time.monotonic()
                if wait > 0:
                    self.sleep(wait)
            self._last_request = time.monotonic()
            try:
                resp = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
            except requests.RequestException as exc:
                errors += 1
                if errors > self.max_retries:
                    raise ApiError(f"GET {path} failed: {exc}") from exc
                backoff = min(2 ** errors, 120)
                self.log(f"  network error ({exc}); retrying in {backoff}s")
                self.sleep(backoff)
                continue

            self.request_count += 1
            remaining = resp.headers.get("X-RateLimit-Remaining")
            if remaining is not None and remaining.isdigit():
                self.rate_limit_remaining = int(remaining)

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 429:
                rate_limit_hits += 1
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    wait = float(retry_after)
                else:
                    wait = min(self.rate_limit_wait * 2 ** (rate_limit_hits - 1), self.max_rate_limit_wait)
                self.log(
                    f"  API rate limit reached after {self.request_count} requests; "
                    f"waiting {int(wait)}s before retrying (progress is saved, Ctrl-C is safe)"
                )
                self.sleep(wait)
                continue

            if resp.status_code == 404:
                raise NotFound(f"Not found: {path}")

            if resp.status_code in (401, 403):
                raise ApiError(
                    f"The API rejected the request ({resp.status_code}). Check your API key "
                    f"(get a free one at https://open.gsa.gov/api/regulationsgov/). "
                    f"Response: {resp.text[:300]}"
                )

            if resp.status_code >= 500 or resp.status_code == 408:
                errors += 1
                if errors > self.max_retries:
                    raise ApiError(f"GET {path} failed with HTTP {resp.status_code}: {resp.text[:300]}")
                backoff = min(2 ** errors, 120)
                self.log(f"  server error {resp.status_code}; retrying in {backoff}s")
                self.sleep(backoff)
                continue

            raise ApiError(f"GET {path} failed with HTTP {resp.status_code}: {resp.text[:300]}")

    # ------------------------------------------------------------- endpoints

    def get_document(self, document_id: str) -> dict:
        return self._get(f"/documents/{document_id}")["data"]

    def get_docket(self, docket_id: str) -> dict:
        return self._get(f"/dockets/{docket_id}")["data"]

    def get_comment(self, comment_id: str) -> tuple[dict, list[dict]]:
        """Return ``(comment_data, attachments)`` for a single comment."""
        body = self._get(f"/comments/{comment_id}", {"include": "attachments"})
        attachments = [item for item in body.get("included") or [] if item.get("type") == "attachments"]
        return body["data"], attachments

    def count_comments(self, filter_key: str, filter_value: str) -> int | None:
        body = self._get(
            "/comments",
            {f"filter[{filter_key}]": filter_value, "page[size]": 5, "page[number]": 1},
        )
        return (body.get("meta") or {}).get("totalElements")

    def iter_comments(self, filter_key: str, filter_value: str) -> Iterator[dict]:
        """Yield every comment summary matching ``filter[filter_key]=filter_value``.

        ``filter_key`` is ``docketId`` (all comments in a docket) or
        ``commentOnId`` (comments on one document, by its objectId).
        """
        seen: set[str] = set()
        lower_bound: str | None = None
        while True:
            new_in_window = 0
            last_item: dict | None = None
            for page in range(1, self.max_pages + 1):
                params: dict[str, Any] = {
                    f"filter[{filter_key}]": filter_value,
                    "page[size]": self.page_size,
                    "page[number]": page,
                    "sort": "lastModifiedDate,documentId",
                }
                if lower_bound:
                    params["filter[lastModifiedDate][ge]"] = lower_bound
                body = self._get("/comments", params)
                items = body.get("data") or []
                for item in items:
                    last_item = item
                    if item["id"] in seen:
                        continue
                    seen.add(item["id"])
                    new_in_window += 1
                    yield item
                has_next = (body.get("meta") or {}).get("hasNextPage", False)
                if len(items) < self.page_size:
                    return
                if not has_next and page < self.max_pages:
                    return
            # Hit the 5,000-result cap: start a new window at the last timestamp.
            if last_item is None or new_in_window == 0:
                return
            last_modified = (last_item.get("attributes") or {}).get("lastModifiedDate")
            if not last_modified:
                self.log("  warning: cannot page past 5,000 comments (no lastModifiedDate)")
                return
            next_bound = to_eastern_filter_value(last_modified)
            if next_bound == lower_bound:
                self.log(
                    "  warning: more than 5,000 comments share one lastModifiedDate; "
                    "some comments may be missed"
                )
                return
            lower_bound = next_bound
