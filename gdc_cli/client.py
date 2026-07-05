from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import requests
from tenacity import retry, retry_if_exception_type, retry_if_result, stop_after_attempt, wait_exponential

from .config import get_settings


def _retryable_response(response: requests.Response) -> bool:
    return response.status_code >= 500


class GDCClient:
    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        request_delay_seconds: float | None = None,
        session: requests.Session | None = None,
    ) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.base_url).rstrip("/")
        self.token = token if token is not None else settings.token
        self.request_delay_seconds = (
            settings.request_delay_seconds
            if request_delay_seconds is None
            else request_delay_seconds
        )
        self.session = session or requests.Session()

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base_url}/{path.strip('/')}"

    def _headers(self, headers: dict[str, str] | None = None) -> dict[str, str]:
        merged = {"Accept": "application/json"}
        if self.token:
            merged["X-Auth-Token"] = self.token
        if headers:
            merged.update(headers)
        return merged

    @retry(
        retry=retry_if_exception_type(requests.RequestException)
        | retry_if_result(_retryable_response),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        if self.request_delay_seconds > 0:
            time.sleep(self.request_delay_seconds)
        headers = self._headers(kwargs.pop("headers", None))
        return self.session.request(
            method,
            self._url(path),
            headers=headers,
            timeout=kwargs.pop("timeout", 60),
            **kwargs,
        )

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._request("GET", path, params=params)
        response.raise_for_status()
        return response.json()

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._request("POST", path, json=payload)
        response.raise_for_status()
        return response.json()

    def text_post(self, path: str, payload: dict[str, Any]) -> str:
        response = self._request(
            "POST",
            path,
            json=payload,
            headers={"Accept": "text/plain"},
        )
        response.raise_for_status()
        return response.text

    def mapping(self, endpoint: str) -> dict[str, Any]:
        return self.get(f"{endpoint}/_mapping")

    def facets(self, endpoint: str, field: str) -> dict[str, Any]:
        return self.get(endpoint, params={"facets": field, "size": 0})

    def paginate(
        self,
        endpoint: str,
        payload: dict[str, Any],
        page_size: int = 1000,
        max_records: int | None = None,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        offset = 0
        while True:
            if max_records is not None:
                remaining = max_records - len(records)
                if remaining <= 0:
                    break
                current_size = min(page_size, remaining)
            else:
                current_size = page_size

            request_payload = dict(payload)
            request_payload["format"] = "JSON"
            request_payload["from"] = offset
            request_payload["size"] = current_size
            response = self.post(endpoint, request_payload)
            data = response.get("data", {})
            hits = data.get("hits", [])
            total = data.get("pagination", {}).get("total", len(hits))
            records.extend(hits)

            if not hits or len(records) >= total:
                break
            offset += len(hits)
        return records

    def manifest(self, file_ids: list[str]) -> str:
        return self.text_post("manifest", {"ids": file_ids})

    def download_data(
        self,
        file_id: str,
        destination: Path,
        on_progress=None,
        max_attempts: int = 5,
    ) -> None:
        """Stream a file to disk, resuming on mid-transfer failures.

        A dropped connection during the body is not covered by the request-level
        retry (that only guards connection setup), so on any streaming error we
        reconnect with an HTTP Range header and continue appending from the bytes
        already on disk. GDC serves 206 Partial Content for byte ranges; if the
        server ignores the range (200) we restart the file. An existing complete
        file yields 416 and is treated as done, so reruns are cheap.
        """
        destination.parent.mkdir(parents=True, exist_ok=True)
        total: int | None = None
        attempt = 0
        while True:
            attempt += 1
            done = destination.stat().st_size if destination.exists() else 0
            headers = {"Accept": "application/octet-stream"}
            if done:
                headers["Range"] = f"bytes={done}-"
            try:
                response = self._request(
                    "GET",
                    f"data/{file_id}",
                    stream=True,
                    headers=headers,
                    timeout=300,
                )
                if response.status_code == 416:  # requested range past EOF => already complete
                    response.close()
                    if on_progress and total:
                        on_progress(done, total)
                    return
                response.raise_for_status()
                if response.status_code == 206:
                    content_range = response.headers.get("Content-Range", "")
                    total = int(content_range.split("/")[-1]) if "/" in content_range else total
                    mode = "ab"
                else:  # server ignored Range (or none sent): stream from the start
                    done = 0
                    header_total = response.headers.get("Content-Length")
                    total = int(header_total) if header_total else None
                    mode = "wb"
                with destination.open(mode) as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            handle.write(chunk)
                            done += len(chunk)
                            if on_progress:
                                on_progress(done, total)
                return
            except requests.RequestException:
                if attempt >= max_attempts:
                    raise
                time.sleep(min(2 ** attempt, 30))
