#!/usr/bin/env python3
"""
Async IDM-style downloader for Google Drive and direct HTTP links.

Use this only for files you own or are authorized to access. The downloader
does not bypass access control; cookies only make the request match your
authorized browser session.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Awaitable, Callable, Optional
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse

import aiohttp


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
DRIVE_UC_URL = "https://drive.google.com/uc"
RETRY_STATUSES = {403, 429, 500, 502, 503, 504}


class IDMDownloadError(RuntimeError):
    pass


@dataclass
class ParsedForm:
    action: str
    method: str
    inputs: dict[str, str]


@dataclass
class DownloadPlan:
    final_url: str
    total_size: int
    etag: str = ""
    last_modified: str = ""


@dataclass
class Segment:
    start: int
    end: int
    next_offset: int
    downloaded: int = 0
    started_at: float = field(default_factory=time.monotonic)

    @property
    def remaining(self) -> int:
        return max(0, self.end - self.next_offset + 1)

    @property
    def speed(self) -> float:
        elapsed = max(time.monotonic() - self.started_at, 0.001)
        return self.downloaded / elapsed


class DriveHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[ParsedForm] = []
        self.links: list[str] = []
        self._form: Optional[dict[str, object]] = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        values = {name.lower(): value or "" for name, value in attrs}
        tag = tag.lower()

        if tag == "form":
            self._form = {
                "action": values.get("action", ""),
                "method": values.get("method", "get").lower(),
                "inputs": {},
            }
            return

        if tag == "input" and self._form is not None:
            name = values.get("name")
            if name:
                inputs = self._form["inputs"]
                assert isinstance(inputs, dict)
                inputs[name] = values.get("value", "")
            return

        if tag == "a":
            href = values.get("href")
            if href:
                self.links.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "form" or self._form is None:
            return

        inputs = self._form["inputs"]
        assert isinstance(inputs, dict)
        self.forms.append(
            ParsedForm(
                action=str(self._form["action"]),
                method=str(self._form["method"]),
                inputs={str(key): str(value) for key, value in inputs.items()},
            )
        )
        self._form = None


ProgressCallback = Callable[[int, int, float], None]
AsyncProgressCallback = Callable[[int, int, float], Awaitable[None]]


class IDMDownloader:
    """
    Download one file with parallel HTTP Range workers, dynamic segmentation,
    connection reuse, Google Drive confirmation handling, retry, and resume.
    """

    def __init__(
        self,
        url: str,
        output_path: str | Path,
        *,
        headers: Optional[dict[str, str]] = None,
        cookies: Optional[dict[str, str] | str] = None,
        concurrency: int = 8,
        chunk_size: int = 1024 * 1024,
        min_split_size: int = 8 * 1024 * 1024,
        checkpoint_path: str | Path | None = None,
        timeout: float = 30.0,
        max_retries: int = 5,
        insecure: bool = False,
        resume: bool = True,
        total_size: Optional[int] = None,
        final_url: Optional[str] = None,
        seed_path: str | Path | None = None,
        initial_completed_ranges: Optional[list[tuple[int, int]]] = None,
        progress_callback: Optional[ProgressCallback | AsyncProgressCallback] = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        if chunk_size < 1:
            raise ValueError("chunk_size must be greater than zero")

        self.url = url
        self.output_path = Path(output_path)
        self.part_path = self.output_path.with_name(self.output_path.name + ".part")
        self.checkpoint_path = (
            Path(checkpoint_path)
            if checkpoint_path is not None
            else self.output_path.with_name(self.output_path.name + ".idm.json")
        )
        self.base_headers = dict(headers or {})
        self.base_headers.setdefault("User-Agent", DEFAULT_USER_AGENT)
        self.base_headers.setdefault("Accept", "*/*")
        self.base_headers.setdefault("Accept-Language", "en-US,en;q=0.9")
        self.cookies = cookies
        self.concurrency = concurrency
        self.chunk_size = chunk_size
        self.min_split_size = min_split_size
        self.timeout = timeout
        self.max_retries = max_retries
        self.insecure = insecure
        self.resume = resume
        self._known_total_size = total_size
        self._known_final_url = final_url
        self.seed_path = Path(seed_path) if seed_path else None
        self.initial_completed_ranges = initial_completed_ranges or []
        self.progress_callback = progress_callback

        self._session: Optional[aiohttp.ClientSession] = None
        self._plan: Optional[DownloadPlan] = None
        self._queue: asyncio.Queue[Segment] = asyncio.Queue()
        self._active: dict[int, Segment] = {}
        self._state_lock = asyncio.Lock()
        self._file_lock = asyncio.Lock()
        self._refresh_lock = asyncio.Lock()
        self._completed: list[tuple[int, int]] = []
        self._written = 0
        self._last_checkpoint = 0.0
        self._started_at = 0.0
        self._last_report = 0.0

    def download_sync(self) -> Path:
        return asyncio.run(self.download())

    async def download(self) -> Path:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        timeout = aiohttp.ClientTimeout(
            total=None,
            sock_connect=self.timeout,
            sock_read=self.timeout,
        )
        connector = aiohttp.TCPConnector(
            limit_per_host=max(self.concurrency + 2, 8),
            ssl=False if self.insecure else None,
        )

        async with aiohttp.ClientSession(
            headers=self.base_headers,
            timeout=timeout,
            connector=connector,
            cookie_jar=aiohttp.CookieJar(unsafe=True),
        ) as session:
            self._session = session
            self._install_cookies(session)
            self._plan = await self._resolve_plan()
            await self._load_or_create_checkpoint(self._plan)
            await self._prepare_part_file(self._plan)
            await self._enqueue_missing_segments(self._plan.total_size)

            self._started_at = time.monotonic()
            print(
                f"IDM dynamic download: {self.concurrency} workers, "
                f"{format_bytes(self._plan.total_size)} total",
                file=sys.stderr,
            )
            workers = [
                asyncio.create_task(self._worker(worker_id))
                for worker_id in range(self.concurrency)
            ]
            await asyncio.gather(*workers)

            if self._completed != [(0, self._plan.total_size - 1)]:
                raise IDMDownloadError("Download did not complete all byte ranges")

            await self._flush_checkpoint(force=True)
            self.part_path.replace(self.output_path)
            self._delete_checkpoint()
            self._print_progress(final=True)
            print(f"\nSaved to: {self.output_path.resolve()}", file=sys.stderr)
            return self.output_path

    def _install_cookies(self, session: aiohttp.ClientSession) -> None:
        if not self.cookies:
            return

        if isinstance(self.cookies, str):
            session.headers.setdefault("Cookie", self.cookies.strip())
            return

        session.cookie_jar.update_cookies(self.cookies)

    async def _resolve_plan(self) -> DownloadPlan:
        assert self._session is not None

        if self._known_final_url and self._known_total_size:
            probe = await self._open_file_response(
                self._known_final_url,
                headers={"Range": "bytes=0-0"},
            )
            async with probe:
                if probe.status != 206:
                    raise IDMDownloadError(
                        f"Server did not honor Range probe: HTTP {probe.status}"
                    )
                total = parse_total_size(probe)
                return DownloadPlan(
                    final_url=str(probe.url),
                    total_size=total or self._known_total_size,
                    etag=probe.headers.get("ETag", ""),
                    last_modified=probe.headers.get("Last-Modified", ""),
                )

        file_id = extract_file_id(self.url)
        if file_id and is_probably_drive_url(self.url):
            drive_url = f"{DRIVE_UC_URL}?{urlencode({'export': 'download', 'id': file_id})}"
            response = await self._open_file_response(
                drive_url,
                headers={"Range": "bytes=0-0"},
                file_id=file_id,
            )
            async with response:
                return self._plan_from_range_response(response)

        response = await self._open_file_response(
            self.url,
            headers={"Range": "bytes=0-0"},
        )
        async with response:
            return self._plan_from_range_response(response)

    def _plan_from_range_response(self, response: aiohttp.ClientResponse) -> DownloadPlan:
        if response.status != 206:
            raise IDMDownloadError(f"Server does not support Range: HTTP {response.status}")

        total = parse_total_size(response)
        if total is None:
            raise IDMDownloadError("Could not determine total file size from Content-Range")

        return DownloadPlan(
            final_url=str(response.url),
            total_size=total,
            etag=response.headers.get("ETag", ""),
            last_modified=response.headers.get("Last-Modified", ""),
        )

    async def _open_file_response(
        self,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        file_id: Optional[str] = None,
        method: str = "GET",
        data: Optional[dict[str, str]] = None,
    ) -> aiohttp.ClientResponse:
        assert self._session is not None
        request_headers = dict(headers or {})

        for _ in range(5):
            response = await self._session.request(
                method,
                url,
                headers=request_headers,
                data=data if method.upper() == "POST" else None,
                allow_redirects=True,
            )
            if response.status >= 400:
                return response
            if not is_html_response(response):
                return response

            html = await response.text(errors="ignore")
            response.release()
            next_request = self._drive_confirmation_request(
                html,
                str(response.url),
                request_headers,
                file_id,
            )
            if next_request is None:
                raise IDMDownloadError(
                    "Google Drive returned an HTML page instead of file bytes"
                )
            method, url, data = next_request

        raise IDMDownloadError("Too many Google Drive confirmation redirects")

    def _drive_confirmation_request(
        self,
        html: str,
        base_url: str,
        range_headers: dict[str, str],
        file_id: Optional[str],
    ) -> Optional[tuple[str, str, Optional[dict[str, str]]]]:
        parser = DriveHtmlParser()
        parser.feed(html[:500_000])

        form = choose_download_form(parser.forms)
        if form is not None:
            url = urljoin(base_url, form.action or base_url)
            if form.method == "post":
                return "POST", url, form.inputs
            query = urlencode(form.inputs)
            separator = "&" if "?" in url else "?"
            return "GET", f"{url}{separator}{query}" if query else url, None

        link = choose_confirm_link(parser.links)
        if link:
            return "GET", urljoin(base_url, link), None

        token = self._download_warning_token()
        if file_id and token:
            url = f"{DRIVE_UC_URL}?{urlencode({'export': 'download', 'id': file_id, 'confirm': token})}"
            return "GET", url, None

        if file_id:
            url = f"{DRIVE_UC_URL}?{urlencode({'export': 'download', 'id': file_id, 'confirm': 't'})}"
            return "GET", url, None

        return None

    def _download_warning_token(self) -> str:
        assert self._session is not None
        for cookie in self._session.cookie_jar:
            if cookie.key.startswith("download_warning"):
                return cookie.value
        return ""

    async def _load_or_create_checkpoint(self, plan: DownloadPlan) -> None:
        if self.resume and self.checkpoint_path.exists():
            data = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
            if data.get("total_size") != plan.total_size:
                raise IDMDownloadError(
                    "Checkpoint total size differs from the remote file. "
                    "Delete the .idm.json/.part files if you want to restart."
                )
            for key, value in (("etag", plan.etag), ("last_modified", plan.last_modified)):
                saved = data.get(key) or ""
                if saved and value and saved != value:
                    raise IDMDownloadError(
                        "Checkpoint identity differs from the remote file. "
                        "Delete the .idm.json/.part files if you want to restart."
                    )
            self._completed = normalize_ranges(
                [tuple(item) for item in data.get("completed", [])],
                plan.total_size,
            )
        else:
            self._completed = normalize_ranges(self.initial_completed_ranges, plan.total_size)

        self._written = sum(end - start + 1 for start, end in self._completed)
        await self._flush_checkpoint(force=True)

    async def _prepare_part_file(self, plan: DownloadPlan) -> None:
        if not self.part_path.exists():
            with self.part_path.open("wb") as file:
                file.truncate(plan.total_size)

        current_size = self.part_path.stat().st_size
        if current_size != plan.total_size:
            with self.part_path.open("r+b") as file:
                file.truncate(plan.total_size)

        if self.seed_path and self.seed_path.exists() and self.initial_completed_ranges:
            with self.seed_path.open("rb") as source, self.part_path.open("r+b") as target:
                for start, end in self.initial_completed_ranges:
                    source.seek(start)
                    target.seek(start)
                    remaining = end - start + 1
                    while remaining > 0:
                        chunk = source.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        target.write(chunk)
                        remaining -= len(chunk)

    async def _enqueue_missing_segments(self, total_size: int) -> None:
        missing = missing_ranges(total_size, self._completed)
        initial = split_ranges_for_workers(missing, self.concurrency)
        for start, end in initial:
            await self._queue.put(Segment(start=start, end=end, next_offset=start))

    async def _worker(self, worker_id: int) -> None:
        while True:
            segment = await self._next_segment()
            if segment is None:
                return

            async with self._state_lock:
                self._active[worker_id] = segment
            try:
                await self._download_segment(segment)
            finally:
                async with self._state_lock:
                    active = self._active.get(worker_id)
                    if active is segment:
                        del self._active[worker_id]

    async def _next_segment(self) -> Optional[Segment]:
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            pass

        async with self._state_lock:
            split = self._split_slowest_active_locked()
            if split is not None:
                return split
            return None

    def _split_slowest_active_locked(self) -> Optional[Segment]:
        candidates = [
            segment
            for segment in self._active.values()
            if segment.remaining >= max(self.min_split_size * 2, self.chunk_size * 2)
        ]
        if not candidates:
            return None

        victim = min(candidates, key=lambda segment: (segment.speed, -segment.remaining))
        old_end = victim.end
        split_start = victim.next_offset + victim.remaining // 2
        if split_start <= victim.next_offset or split_start > old_end:
            return None
        victim.end = split_start - 1
        return Segment(start=split_start, end=old_end, next_offset=split_start)

    async def _download_segment(self, segment: Segment) -> None:
        assert self._plan is not None
        attempt = 0
        while segment.remaining > 0:
            range_start = segment.next_offset
            range_end = segment.end
            try:
                await self._request_range(segment, range_start, range_end)
                attempt = 0
            except Exception as exc:
                attempt += 1
                if attempt > self.max_retries:
                    raise
                if isinstance(exc, IDMDownloadError) and "HTTP 403" in str(exc):
                    await self._refresh_final_url()
                delay = min(2**attempt, 30)
                await asyncio.sleep(delay)

    async def _request_range(self, segment: Segment, start: int, end: int) -> None:
        assert self._session is not None
        assert self._plan is not None

        headers = {"Range": f"bytes={start}-{end}"}
        async with self._session.get(self._plan.final_url, headers=headers) as response:
            if response.status in RETRY_STATUSES:
                raise IDMDownloadError(f"HTTP {response.status} while downloading range")
            if response.status != 206:
                raise IDMDownloadError(
                    f"Range {start}-{end} returned HTTP {response.status}"
                )

            async for chunk in response.content.iter_chunked(self.chunk_size):
                if not chunk:
                    continue

                async with self._state_lock:
                    if segment.next_offset > segment.end:
                        break
                    allowed = min(len(chunk), segment.end - segment.next_offset + 1)
                    write_at = segment.next_offset
                    segment.next_offset += allowed
                    segment.downloaded += allowed
                    should_stop = allowed < len(chunk)

                await self._write_at(write_at, chunk[:allowed])
                await self._mark_completed(write_at, write_at + allowed - 1)
                self._print_progress()

                if should_stop:
                    break

    async def _write_at(self, offset: int, data: bytes) -> None:
        async with self._file_lock:
            with self.part_path.open("r+b") as file:
                file.seek(offset)
                file.write(data)

    async def _mark_completed(self, start: int, end: int) -> None:
        async with self._state_lock:
            self._completed = normalize_ranges(self._completed + [(start, end)])
            self._written = sum(item_end - item_start + 1 for item_start, item_end in self._completed)
        await self._flush_checkpoint()

    async def _flush_checkpoint(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_checkpoint < 1.0:
            return
        self._last_checkpoint = now

        if self._plan is None:
            return

        data = {
            "source_url": self.url,
            "final_url": self._plan.final_url,
            "output_path": str(self.output_path),
            "total_size": self._plan.total_size,
            "etag": self._plan.etag,
            "last_modified": self._plan.last_modified,
            "completed": self._completed,
            "updated_at": int(time.time()),
        }
        temp_path = self.checkpoint_path.with_suffix(self.checkpoint_path.suffix + ".tmp")
        temp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temp_path.replace(self.checkpoint_path)

    async def _refresh_final_url(self) -> None:
        async with self._refresh_lock:
            self._plan = await self._resolve_plan()

    def _delete_checkpoint(self) -> None:
        for path in (self.checkpoint_path, self.checkpoint_path.with_suffix(self.checkpoint_path.suffix + ".tmp")):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def _print_progress(self, *, final: bool = False) -> None:
        if self._plan is None:
            return
        now = time.monotonic()
        if not final and now - self._last_report < 0.5:
            return
        self._last_report = now

        elapsed = max(now - self._started_at, 0.001)
        speed = self._written / elapsed
        total = self._plan.total_size
        if self.progress_callback is not None:
            result = self.progress_callback(self._written, total, speed)
            if asyncio.iscoroutine(result):
                asyncio.create_task(result)
            return

        percent = min(self._written / total * 100, 100)
        message = (
            f"{format_bytes(self._written)} / {format_bytes(total)} "
            f"({percent:5.1f}%) at {format_bytes(speed)}/s"
        )
        print("\r" + message.ljust(90), end="", file=sys.stderr, flush=True)


def extract_file_id(value: str) -> Optional[str]:
    value = value.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", value):
        return value

    parsed = urlparse(value)
    query = parse_qs(parsed.query)
    for key in ("id", "file_id"):
        if query.get(key):
            return query[key][0]

    for pattern in (r"/file/d/([^/?#]+)", r"/uc/([^/?#]+)", r"/d/([^/?#]+)"):
        match = re.search(pattern, parsed.path)
        if match:
            return unquote(match.group(1))
    return None


def is_probably_drive_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host.endswith("drive.google.com") or host.endswith("drive.usercontent.google.com")


def is_html_response(response: aiohttp.ClientResponse) -> bool:
    content_type = response.headers.get("Content-Type", "").lower()
    disposition = response.headers.get("Content-Disposition", "").lower()
    return "text/html" in content_type and "filename=" not in disposition


def choose_download_form(forms: list[ParsedForm]) -> Optional[ParsedForm]:
    for form in forms:
        action = form.action.lower()
        input_keys = set(form.inputs)
        if (
            "download" in action
            or "/uc" in action
            or {"id", "confirm"}.issubset(input_keys)
            or {"uuid", "confirm"}.issubset(input_keys)
        ):
            return form
    return None


def choose_confirm_link(links: list[str]) -> Optional[str]:
    for link in links:
        lower = link.lower()
        if "confirm=" in lower and ("download" in lower or "/uc?" in lower):
            return link
    return None


def parse_total_size(response: aiohttp.ClientResponse) -> Optional[int]:
    content_range = response.headers.get("Content-Range", "")
    match = re.search(r"/(\d+)$", content_range)
    if match:
        return int(match.group(1))
    content_length = response.headers.get("Content-Length", "")
    if content_length.isdigit():
        return int(content_length)
    return None


def missing_ranges(total_size: int, completed: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if total_size <= 0:
        return []

    missing: list[tuple[int, int]] = []
    cursor = 0
    for start, end in normalize_ranges(completed, total_size):
        if cursor < start:
            missing.append((cursor, start - 1))
        cursor = max(cursor, end + 1)
    if cursor < total_size:
        missing.append((cursor, total_size - 1))
    return missing


def normalize_ranges(
    ranges: list[tuple[int, int]],
    total_size: Optional[int] = None,
) -> list[tuple[int, int]]:
    cleaned: list[tuple[int, int]] = []
    for start, end in ranges:
        start = int(start)
        end = int(end)
        if total_size is not None:
            start = max(0, min(start, total_size - 1))
            end = max(0, min(end, total_size - 1))
        if start <= end:
            cleaned.append((start, end))

    merged: list[tuple[int, int]] = []
    for start, end in sorted(cleaned):
        if not merged or start > merged[-1][1] + 1:
            merged.append((start, end))
            continue
        merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def split_ranges_for_workers(
    ranges: list[tuple[int, int]],
    workers: int,
) -> list[tuple[int, int]]:
    total_missing = sum(end - start + 1 for start, end in ranges)
    if total_missing <= 0:
        return []

    target = max(1, (total_missing + workers - 1) // workers)
    result: list[tuple[int, int]] = []
    for start, end in ranges:
        cursor = start
        while cursor <= end:
            chunk_end = min(cursor + target - 1, end)
            result.append((cursor, chunk_end))
            cursor = chunk_end + 1
    return result


def format_bytes(value: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TB"
