"""
Windows Drive Sync Tool — Improved
====================================
Các cải tiến so với bản gốc:

1. SeekableHttpRangeStream: sửa vòng lặp vô hạn, thêm max_empty_reads guard
2. StreamDownloader: thêm refresh URL khi hết hạn (stream URL chỉ sống ~1 giờ)
3. is_blocked_error: mở rộng nhận diện 403/cannotDownload/downloadQuotaExceeded
4. Fallback chain rõ ràng: API copy → stream upload → tải tạm rồi upload
5. Terminal dashboard 2 tab: copy bên trái, stream/fallback bên phải
6. Session pool: tái sử dụng session thay vì tạo mới mỗi lần
7. Non-video bị block: thử export PDF/office nếu là Google Workspace file
8. Retry với exponential backoff cho cả copy và stream
"""

from __future__ import annotations

import os
import sys
import time
import json
import hashlib
import threading
import traceback
import argparse
import re
import io
import tempfile
import shutil
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

import requests as req_lib

from drive_common import (
    FOLDER_MIME_TYPE,
    drive_query_literal,
    find_matching_item,
    is_blocked_drive_error,
    is_retryable_drive_error,
    is_shortcut,
    normalize_drive_name,
    same_drive_name,
    shortcut_target_id,
    skipped_file_reason_for_mimes,
    skipped_shortcut_reason,
    without_drive_shortcuts,
)
from main import SyncState

# Fix Unicode output on Windows (Vietnamese filenames) — must run before any print
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    from batch_cookie_downloader import (
        download_direct_uc,
        download_playwright_viewer,
        load_cookies,
        load_oauth_token,
        _validate_pdf,
    )
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

try:
    from rich.console import Console
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    RICH_TERMINAL_AVAILABLE = True
except ImportError:
    Console = Live = Panel = Table = Text = None
    RICH_TERMINAL_AVAILABLE = False


class TerminalUI:
    """Thread-safe terminal output with split copy/stream dashboard."""

    COLORS = {
        "reset": "\033[0m",
        "bold": "\033[1m",
        "dim": "\033[2m",
        "red": "\033[31m",
        "green": "\033[32m",
        "yellow": "\033[33m",
        "blue": "\033[34m",
        "magenta": "\033[35m",
        "cyan": "\033[36m",
        "white": "\033[37m",
    }
    STATUS = {
        "INFO": ("•", "white"),
        "OK": ("✓", "green"),
        "DONE": ("✓", "green"),
        "COPY": ("✓", "green"),
        "SKIP": ("↷", "cyan"),
        "SMART": ("◆", "cyan"),
        "SCAN": ("•", "blue"),
        "WORKER": ("▸", "blue"),
        "STREAM": ("▶", "blue"),
        "EXPORT": ("⇧", "blue"),
        "FALLBACK": ("⇣", "magenta"),
        "COOKIE": ("●", "yellow"),
        "RETRY": ("↻", "yellow"),
        "WARN": ("!", "yellow"),
        "BLOCKED": ("■", "red"),
        "ERROR": ("×", "red"),
    }
    COPY_COUNTERS = (
        ("File xử lý", "files_seen"),
        ("Folder quét", "folders_scanned"),
        ("Copy API", "copied"),
        ("Bỏ qua", "skipped"),
        ("Checkpoint", "checkpoint"),
        ("Export", "exported"),
        ("Smart skip", "smart_skipped"),
        ("Blocked", "blocked"),
        ("Lỗi", "errors"),
    )
    STREAM_COUNTERS = (
        ("Stream OK", "streamed"),
        ("Fallback OK", "fallback_uploaded"),
        ("Lỗi", "errors"),
    )
    STREAM_LABELS = {"STREAM", "FALLBACK", "COOKIE"}
    STREAM_HINTS = (
        "stream",
        "cookie",
        "fallback",
        "url",
        "tải về tạm",
        "tai ve tam",
    )

    def __init__(self):
        self._lock = threading.RLock()
        self.width = max(78, min(shutil.get_terminal_size((100, 20)).columns, 120))
        self.color = self._detect_color()
        self.console = Console() if RICH_TERMINAL_AVAILABLE else None
        self.live = None
        self.copy_lines: list[str] = []
        self.stream_lines: list[str] = []
        self.counters: Counter[str] = Counter()
        self.stream_tasks: dict[str, dict[str, object]] = {}
        self._last_refresh = 0.0
        self._max_log_lines = 12

    @staticmethod
    def _detect_color() -> bool:
        if os.environ.get("NO_COLOR"):
            return False
        if os.environ.get("PLAIN_UI"):
            return False
        return bool(getattr(sys.stdout, "isatty", lambda: False)())

    @staticmethod
    def _detect_dashboard() -> bool:
        if os.environ.get("PLAIN_UI") or os.environ.get("NO_LIVE_UI"):
            return False
        return bool(getattr(sys.stdout, "isatty", lambda: False)())

    def paint(self, text: str, style: str) -> str:
        if not self.color or style not in self.COLORS:
            return text
        return f"{self.COLORS[style]}{text}{self.COLORS['reset']}"

    def write(self, text: str = ""):
        with self._lock:
            if self.live:
                self._append_panel_line("copy", text)
                self._refresh_live_locked()
                return
            print(text)

    def line(self, char: str = "─", style: str = "dim"):
        self.write(self.paint(char * self.width, style))

    def banner(self, title: str, rows: list[tuple[str, str]] | None = None):
        with self._lock:
            if self.live:
                self.stop_dashboard()
            print()
            print(self.paint("═" * self.width, "cyan"))
            print(self.paint(title.center(self.width), "bold"))
            if rows:
                print(self.paint("─" * self.width, "cyan"))
                label_width = max(len(label) for label, _ in rows)
                for label, value in rows:
                    print(f"  {self.paint(label.ljust(label_width), 'dim')} : {value}")
            print(self.paint("═" * self.width, "cyan"))

    def section(self, title: str):
        with self._lock:
            if self.live:
                self._append_panel_line("copy", f"─ {title}")
                self._refresh_live_locked(force=True)
                return
        self.write(f"\n{self.paint('─', 'dim')} {self.paint(title, 'bold')}")

    def status(self, label: str, message: str, indent: int = 0):
        key = label.upper()
        icon, style = self.STATUS.get(key, ("•", "white"))
        raw = f"{' ' * indent}{icon} {key:<8} {message}"
        with self._lock:
            if self.live:
                self._append_panel_line(self._pane_for_status(key, message), raw)
                self._refresh_live_locked()
                return
        tag = self.paint(f"{icon} {key:<8}", style)
        self.write(f"{' ' * indent}{tag} {message}")

    def info(self, message: str, indent: int = 0):
        self.status("INFO", message, indent)

    def success(self, message: str, indent: int = 0):
        self.status("OK", message, indent)

    def warn(self, message: str, indent: int = 0):
        self.status("WARN", message, indent)

    def error(self, message: str, indent: int = 0):
        self.status("ERROR", message, indent)

    def summary(self, title: str, rows: list[tuple[str, object]]):
        with self._lock:
            if self.live:
                self.stop_dashboard()
        self.section(title)
        label_width = max((len(label) for label, _ in rows), default=0)
        for label, value in rows:
            self.write(f"  {self.paint(label.ljust(label_width), 'dim')} : {value}")

    def start_dashboard(self) -> bool:
        with self._lock:
            if self.live or not RICH_TERMINAL_AVAILABLE or not self._detect_dashboard():
                return False
            try:
                self.live = Live(
                    self._render_dashboard(),
                    console=self.console,
                    refresh_per_second=4,
                    transient=False,
                )
                self.live.start(refresh=True)
                return True
            except Exception:
                self.live = None
                return False

    def stop_dashboard(self) -> None:
        live = None
        with self._lock:
            live = self.live
            if live is None:
                return
            self._refresh_live_locked(force=True)
            self.live = None
        live.stop()

    def dashboard_active(self) -> bool:
        with self._lock:
            return self.live is not None

    def update_counter(self, key: str, value: int) -> None:
        with self._lock:
            self.counters[key] = value
            if self.live:
                self._refresh_live_locked()

    def start_stream_task(self, name: str, total: int | None, phase: str = "stream") -> str | None:
        with self._lock:
            if not self.live:
                return None
            task_id = f"{phase}:{threading.get_ident()}:{time.monotonic_ns()}"
            self.stream_tasks[task_id] = {
                "name": name,
                "phase": phase,
                "current": 0,
                "total": int(total or 0),
                "started_at": time.monotonic(),
                "last_time": time.monotonic(),
                "last_current": 0,
                "speed": 0.0,
            }
            self._append_panel_line("stream", f"▶ {phase}: {name}")
            self._refresh_live_locked(force=True)
            return task_id

    def update_stream_task(self, task_id: str | None, current: int, total: int | None = None) -> None:
        if not task_id:
            return
        with self._lock:
            task = self.stream_tasks.get(task_id)
            if task is None:
                return
            
            now = time.monotonic()
            last_time = task.get("last_time", now)
            last_current = task.get("last_current", 0)
            
            if now - last_time >= 0.5:
                diff = max(0, int(current) - last_current)
                task["speed"] = diff / (now - last_time)
                task["last_time"] = now
                task["last_current"] = max(last_current, int(current))
                
            task["current"] = max(task.get("current", 0), int(current))
            if total:
                task["total"] = int(total)
            self._refresh_live_locked()

    def finish_stream_task(self, task_id: str | None, result: str = "OK") -> None:
        if not task_id:
            return
        with self._lock:
            task = self.stream_tasks.pop(task_id, None)
            if task:
                self._append_panel_line(
                    "stream",
                    f"✓ {result}: {task.get('phase', 'stream')} - {task.get('name', '')}",
                )
            if self.live:
                self._refresh_live_locked(force=True)

    def _pane_for_status(self, key: str, message: str) -> str:
        lower = message.lower()
        if key in self.STREAM_LABELS:
            return "stream"
        if key in {"RETRY", "WARN", "ERROR", "BLOCKED"} and any(
            hint in lower for hint in self.STREAM_HINTS
        ):
            return "stream"
        return "copy"

    def _append_panel_line(self, pane: str, line: str) -> None:
        target = self.stream_lines if pane == "stream" else self.copy_lines
        target.append(self._shorten(line, 110))
        del target[:-self._max_log_lines]

    def _refresh_live_locked(self, force: bool = False) -> None:
        if not self.live:
            return
        now = time.monotonic()
        if not force and now - self._last_refresh < 0.15:
            return
        self.live.update(self._render_dashboard(), refresh=True)
        self._last_refresh = now

    def _render_dashboard(self):
        grid = Table.grid(expand=True)
        grid.add_column(ratio=1)
        grid.add_column(ratio=1)
        grid.add_row(
            Panel(
                self._render_copy_panel(),
                title="[bold cyan]Tab 1 - Tiến trình copy[/]",
                border_style="cyan",
                padding=(1, 1),
            ),
            Panel(
                self._render_stream_panel(),
                title="[bold magenta]Tab 2 - Check stream[/]",
                border_style="magenta",
                padding=(1, 1),
            ),
        )
        
        total_speed = sum(task.get("speed", 0.0) for task in self.stream_tasks.values())
        speed_text = f"  [dim]▼ Tốc độ Stream:[/] [bold green]{self._format_bytes(int(total_speed))}/s[/]"
        
        main_grid = Table.grid(expand=True)
        main_grid.add_row(grid)
        main_grid.add_row(Text.from_markup(speed_text, justify="left"))
        return main_grid

    def _render_copy_panel(self):
        body = Table.grid(expand=True)
        body.add_row(self._render_counter_table(self.COPY_COUNTERS))
        body.add_row(Text(""))
        body.add_row(Text("Log copy gần nhất", style="bold"))
        body.add_row(Text("\n".join(self.copy_lines[-self._max_log_lines:]) or "Chưa có log copy."))
        return body

    def _render_stream_panel(self):
        body = Table.grid(expand=True)
        body.add_row(self._render_counter_table(self.STREAM_COUNTERS))
        body.add_row(Text(""))
        body.add_row(Text("Stream đang chạy", style="bold"))
        active = self._render_stream_tasks()
        body.add_row(active)
        body.add_row(Text(""))
        body.add_row(Text("Log stream gần nhất", style="bold"))
        body.add_row(Text("\n".join(self.stream_lines[-self._max_log_lines:]) or "Chưa có stream."))
        return body

    def _render_counter_table(self, rows: tuple[tuple[str, str], ...]):
        table = Table.grid(expand=True)
        table.add_column(ratio=1)
        table.add_column(justify="right", width=10)
        for label, key in rows:
            table.add_row(label, str(self.counters.get(key, 0)))
        return table

    def _render_stream_tasks(self):
        if not self.stream_tasks:
            return Text("Không có stream đang chạy.")
        table = Table.grid(expand=True)
        table.add_column(ratio=1)
        table.add_column(justify="right", width=18)
        for task in list(self.stream_tasks.values())[:6]:
            total = int(task.get("total") or 0)
            current = int(task.get("current") or 0)
            pct = (current / total * 100.0) if total else 0.0
            name = self._shorten(str(task.get("name") or ""), 46)
            phase = str(task.get("phase") or "stream")
            table.add_row(f"{phase}: {name}", f"{pct:5.1f}%")
            table.add_row(
                self._progress_bar(pct),
                f"{self._format_bytes(current)}/{self._format_bytes(total)}",
            )
        remaining = len(self.stream_tasks) - 6
        if remaining > 0:
            table.add_row(f"... thêm {remaining} stream", "")
        return table

    @staticmethod
    def _progress_bar(percent: float, width: int = 24) -> str:
        percent = max(0.0, min(100.0, percent))
        filled = int(width * percent / 100.0)
        return "[" + "#" * filled + "-" * (width - filled) + "]"

    @staticmethod
    def _format_bytes(value: int) -> str:
        units = ("B", "KB", "MB", "GB", "TB")
        size = float(max(0, value))
        for unit in units:
            if size < 1024 or unit == units[-1]:
                return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
            size /= 1024
        return f"{value}B"

    @staticmethod
    def _shorten(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 3)] + "..."


UI = TerminalUI()


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


ROOT_DIR = Path(__file__).resolve().parent
GDRIVE_DOWNLOAD_DIR = ROOT_DIR / "gdrive-download"
REQUIRED_DOWNLOADER_FILES = (
    GDRIVE_DOWNLOAD_DIR / "gdrive_stream_downloader.py",
    GDRIVE_DOWNLOAD_DIR / "idm_downloader.py",
)

missing_downloader_files = [path for path in REQUIRED_DOWNLOADER_FILES if not path.exists()]
if missing_downloader_files:
    UI.error("Thiếu file downloader. Hãy copy nguyên thư mục gdrive-download đi cùng windows_sync_tool_improved.py.")
    UI.info(f"Thư mục cần có: {GDRIVE_DOWNLOAD_DIR}", indent=3)
    for path in missing_downloader_files:
        UI.error(f"Thiếu: {path.name}", indent=3)
    sys.exit(1)

sys.path.insert(0, str(GDRIVE_DOWNLOAD_DIR))
try:
    import gdrive_stream_downloader as stream_dl
except ModuleNotFoundError as exc:
    UI.error(f"Thiếu thư viện Python hoặc module phụ: {exc.name}")
    UI.info("Chạy lệnh sau trong thư mục drive_copy:", indent=3)
    UI.info("python -m pip install -r requirements.txt", indent=3)
    sys.exit(1)
except ImportError as exc:
    UI.error("Không import được gdrive_stream_downloader.py.")
    UI.info(f"Chi tiết lỗi: {exc}", indent=3)
    sys.exit(1)

def _load_netscape_cookie_fallback(session: req_lib.Session, cookie_path: Path) -> int:
    loaded = 0
    text = cookie_path.read_text(encoding="utf-8-sig", errors="ignore")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#"):
            continue

        parts = line.split("\t")
        if len(parts) < 7:
            continue

        domain, _include_subdomains, path, secure, _expires, name, value = parts[:7]
        if not name:
            continue
        session.cookies.set(
            name,
            value,
            domain=domain,
            path=path or "/",
            secure=secure.upper() == "TRUE",
        )
        loaded += 1
    return loaded


def _truthy_cookie_flag(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _json_cookie_items(raw: object) -> list[dict]:
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]

    if isinstance(raw, dict):
        for key in ("cookies", "data"):
            value = raw.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]

        # Also accept a compact {"SID": "...", "HSID": "..."} mapping.
        if all(not isinstance(value, (list, dict)) for value in raw.values()):
            return [
                {"name": str(name), "value": "" if value is None else str(value)}
                for name, value in raw.items()
            ]

    return []


def _load_json_cookie_file(cookie_path: Path) -> list[dict]:
    with cookie_path.open("r", encoding="utf-8-sig") as f:
        return _json_cookie_items(json.load(f))


def _load_json_cookie_file_into_session(
    session: req_lib.Session,
    cookie_path: Path,
    default_domain: str = ".google.com",
) -> int:
    loaded = 0
    for cookie in _load_json_cookie_file(cookie_path):
        name = cookie.get("name") or cookie.get("key")
        value = cookie.get("value")
        if not name or value is None:
            continue

        domain = str(cookie.get("domain") or default_domain).strip()
        if domain.startswith(("http://", "https://")):
            domain = urlparse(domain).hostname or default_domain

        session.cookies.set(
            str(name),
            str(value),
            domain=domain or default_domain,
            path=str(cookie.get("path") or "/"),
            secure=_truthy_cookie_flag(cookie.get("secure", False)),
        )
        loaded += 1
    return loaded


def _load_cookie_file_as_dict(cookie_path: Path) -> dict[str, str]:
    cookie_dict: dict[str, str] = {}
    for cookie in _load_json_cookie_file(cookie_path):
        name = cookie.get("name") or cookie.get("key")
        value = cookie.get("value")
        if name and value is not None:
            cookie_dict[str(name)] = str(value)
    return cookie_dict


def _load_cookie_file_into_session(session: req_lib.Session, cookie_path: Path) -> None:
    text = cookie_path.read_text(encoding="utf-8-sig", errors="ignore").lstrip()
    if text.startswith("[") or text.startswith("{") or cookie_path.suffix.lower() == ".json":
        if _load_json_cookie_file_into_session(session, cookie_path) <= 0:
            raise ValueError(f"Khong doc duoc cookie JSON: {cookie_path}")
        return

    try:
        stream_dl.load_cookie_file(session, cookie_path, ".google.com")
    except Exception:
        if _load_netscape_cookie_fallback(session, cookie_path) <= 0:
            raise


try:
    from tqdm import tqdm
    TQDM_AVAILABLE = True
except ImportError:
    TQDM_AVAILABLE = False

# ─── Constants ────────────────────────────────────────────────────────────────

HTTP_RANGE_BUFFER_SIZE = 32 * 1024 * 1024   # 32MB để progress cập nhật nhanh hơn với mạng siêu tốc
UPLOAD_CHUNK_SIZE      = 256 * 1024 * 1024 # 256MB tối đa hoá tốc độ upload
STREAM_URL_TTL_SECONDS = 3000              # stream URL sống ~50 phút (thực tế ~60 phút)

COOKIE_AUTH_ERROR_KEYWORDS = [
    "cookie",
    "unauthorized",
    "not authorized",
    "login",
    "sign in",
    "servicelogin",
    "accounts.google",
    "khong du quyen",
    "không đủ quyền",
    "het han",
    "hết hạn",
]
SKIPPED_MIME_TYPES = {
    "application/vnd.google-apps.spreadsheet": "Google Sheet",
}
GOOGLE_WORKSPACE_EXPORT = {
    "application/vnd.google-apps.document":     ("pdf", "application/pdf"),
    "application/vnd.google-apps.presentation": ("pdf", "application/pdf"),
    "application/vnd.google-apps.drawing":      ("png", "image/png"),
}

BLOCKED_PDF_FILE_RE = re.compile(r"/file/d/([A-Za-z0-9_-]+)/view")
BLOCKED_PDF_FOLDER_RE = re.compile(r"/folders/([A-Za-z0-9_-]+)")


@dataclass(frozen=True)
class BlockedPdfEntry:
    file_id: str
    name: str
    dest_folder_id: str

    @property
    def source_link(self) -> str:
        return f"https://drive.google.com/file/d/{self.file_id}/view"

    @property
    def dest_folder_link(self) -> str:
        return f"https://drive.google.com/drive/folders/{self.dest_folder_id}"


def sanitize_local_filename(name: str, limit: int = 200) -> str:
    safe_name = re.sub(r'[<>:"/\\|?*]', "_", name).strip()
    return (safe_name or "blocked.pdf")[:limit]


def parse_blocked_pdf_markdown(path: str | os.PathLike[str]) -> list[BlockedPdfEntry]:
    entries: list[BlockedPdfEntry] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line.startswith("|") or "|---|" in line:
                continue

            file_match = BLOCKED_PDF_FILE_RE.search(line)
            folder_match = BLOCKED_PDF_FOLDER_RE.search(line)
            if not file_match or not folder_match:
                continue

            link_marker = "](https://drive.google.com/file/d/"
            marker_pos = line.find(link_marker)
            if marker_pos == -1:
                continue
            name_start = line.find("[", 0, marker_pos)
            if name_start == -1:
                continue

            name = line[name_start + 1:marker_pos].strip()
            if not name:
                continue

            entries.append(
                BlockedPdfEntry(
                    file_id=file_match.group(1),
                    name=name,
                    dest_folder_id=folder_match.group(1),
                )
            )
    return entries

# ─── Helpers ──────────────────────────────────────────────────────────────────

def exponential_backoff(attempt: int, base: float = 2.0, cap: float = 60.0) -> float:
    return min(base ** attempt, cap)

def skipped_file_reason(file_info: dict) -> str | None:
    return skipped_file_reason_for_mimes(file_info, SKIPPED_MIME_TYPES)

# ─── Error classifiers ────────────────────────────────────────────────────────

def is_blocked_error(e: HttpError) -> bool:
    return is_blocked_drive_error(e)

def is_retryable_error(e: HttpError) -> bool:
    return is_retryable_drive_error(e)

def looks_like_cookie_auth_error(error: Exception | str) -> bool:
    text = str(error).lower()
    return any(keyword in text for keyword in COOKIE_AUTH_ERROR_KEYWORDS)

# ─── VideoStreamSource ────────────────────────────────────────────────────────

@dataclass
class VideoStreamSource:
    url:       str
    headers:   dict[str, str]
    size:      int
    itag:      str
    fetched_at: float = field(default_factory=time.monotonic)

    def is_expired(self) -> bool:
        return (time.monotonic() - self.fetched_at) > STREAM_URL_TTL_SECONDS


# ─── SeekableHttpRangeStream (FIXED) ─────────────────────────────────────────

class SeekableHttpRangeStream(io.RawIOBase):
    """
    File-like HTTP stream dùng Range request, không ghi xuống ổ đĩa.

    Fix so với bản gốc:
    - `read()` có MAX_EMPTY_READS guard tránh vòng lặp vô hạn
    - `_fetch_buffer` luôn raise RuntimeError khi thất bại (không return silent)
    - Thêm callback progress (tuỳ chọn)
    - Hỗ trợ refresh URL khi hết hạn qua `refresh_callback`
    """

    MAX_EMPTY_READS = 3

    def __init__(
        self,
        session: req_lib.Session,
        source: VideoStreamSource,
        buffer_size: int = HTTP_RANGE_BUFFER_SIZE,
        progress_callback=None,        # callable(bytes_read: int, total: int)
        refresh_callback=None,         # callable() -> VideoStreamSource | None
    ):
        super().__init__()
        self.session          = session
        self.source           = source
        self.buffer_size      = buffer_size
        self.progress_callback = progress_callback
        self.refresh_callback  = refresh_callback
        self.pos              = 0
        self.buffer           = b""
        self.buffer_start     = 0
        self._total_read      = 0
        self._max_reported    = 0

    def _report_progress(self, val: int):
        if val > self._max_reported:
            self._max_reported = val
            if self.progress_callback:
                self.progress_callback(val, self.source.size)

    # ── io.RawIOBase interface ────────────────────────────────────────────────

    def readable(self)  -> bool: return True
    def seekable(self)  -> bool: return True
    def tell(self)      -> int:  return self.pos

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if whence == os.SEEK_SET:
            new_pos = offset
        elif whence == os.SEEK_CUR:
            new_pos = self.pos + offset
        elif whence == os.SEEK_END:
            new_pos = self.source.size + offset
        else:
            raise ValueError(f"whence không hợp lệ: {whence}")
        self.pos = max(0, min(new_pos, self.source.size))
        return self.pos

    def read(self, size: int = -1) -> bytes:
        if self.closed:
            raise ValueError("I/O operation on closed stream")
        if self.pos >= self.source.size:
            return b""

        if size is None or size < 0:
            size = self.source.size - self.pos
        if size == 0:
            return b""

        size = min(size, self.source.size - self.pos)
        chunks: list[bytes] = []
        remaining = size
        empty_reads = 0

        while remaining > 0 and self.pos < self.source.size:
            # Kiểm tra URL có hết hạn không, thử refresh
            if self.source.is_expired() and self.refresh_callback:
                new_source = self.refresh_callback()
                if new_source:
                    self.source = new_source
                    self.buffer = b""       # xoá cache buffer cũ

            if not self._buffer_has_pos(self.pos):
                self._fetch_buffer()        # raise RuntimeError nếu thất bại

            if not self.buffer:
                empty_reads += 1
                if empty_reads >= self.MAX_EMPTY_READS:
                    break                   # tránh vòng lặp vô hạn
                continue

            empty_reads = 0
            offset    = self.pos - self.buffer_start
            available = len(self.buffer) - offset
            if available <= 0:
                self.buffer = b""
                continue

            take = min(available, remaining)
            chunks.append(self.buffer[offset: offset + take])
            self.pos             += take
            remaining            -= take
            self._total_read     += take

            self._report_progress(self._total_read)

        return b"".join(chunks)

    def close(self):
        self.buffer = b""
        super().close()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _buffer_has_pos(self, position: int) -> bool:
        return self.buffer_start <= position < self.buffer_start + len(self.buffer)

    def _fetch_buffer(self):
        """Tải một đoạn buffer từ stream URL bằng Range request.

        Luôn raise RuntimeError khi thất bại — không bao giờ return silent.
        """
        start = self.pos
        if start >= self.source.size:
            self.buffer       = b""
            self.buffer_start = start
            return

        end     = min(start + self.buffer_size, self.source.size) - 1
        headers = dict(self.source.headers)
        headers["Range"]           = f"bytes={start}-{end}"
        headers["Accept-Encoding"] = "identity"

        last_err = None
        for attempt in range(1, 4):
            resp = None
            try:
                resp = self.session.get(
                    self.source.url, headers=headers, timeout=60, stream=True
                )
                if resp.status_code == 416 and start >= self.source.size:
                    self.buffer = b""; self.buffer_start = start; return
                if resp.status_code == 403:
                    raise RuntimeError(
                        f"HTTP 403 — stream URL có thể đã hết hạn (đã dùng "
                        f"{(time.monotonic()-self.source.fetched_at)/60:.1f} phút)"
                    )
                if resp.status_code != 206:
                    raise RuntimeError(
                        f"HTTP {resp.status_code} — server không trả về 206 Partial Content"
                    )
                
                chunks = []
                downloaded_for_this_range = 0
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        chunks.append(chunk)
                        downloaded_for_this_range += len(chunk)
                        self._report_progress(self._total_read + downloaded_for_this_range)
                        
                data = b"".join(chunks)
                if not data:
                    raise RuntimeError("Range request trả về dữ liệu rỗng")
                self.buffer = data; self.buffer_start = start; return
            except RuntimeError:
                raise
            except Exception as e:
                last_err = e
                if attempt < 3:
                    time.sleep(exponential_backoff(attempt))
            finally:
                if resp is not None:
                    resp.close()

        raise RuntimeError(
            f"Không thể đọc stream range {start}-{end} sau 3 lần thử: {last_err}"
        )


# ─── StreamDownloader (với URL refresh) ──────────────────────────────────────

class StreamDownloader:
    """
    Cải tiến so với bản gốc:
    - get_video_source trả về VideoStreamSource với timestamp để detect expiry
    - refresh_source() gọi lại get_video_info để lấy URL mới
    - Hỗ trợ fallback tải file tạm khi stream upload thất bại
    """

    def __init__(self, session: req_lib.Session):
        self.session = session
        self.cookie_version = 0
        self.last_error: str | None = None

    def get_video_source(self, file_id: str) -> VideoStreamSource:
        self.last_error = None
        info_resp = self.session.get(
            stream_dl.DRIVE_VIDEO_INFO_URL,
            params={"docid": file_id, "authuser": "0"},
            timeout=30,
        )
        if info_resp.status_code >= 400:
            raise RuntimeError(
                f"Không lấy được video info (HTTP {info_resp.status_code}). "
                "Cookie có thể đã hết hạn hoặc không đủ quyền truy cập."
            )

        from urllib.parse import parse_qs
        info   = parse_qs(info_resp.text, keep_blank_values=True)
        status = stream_dl.first_value(info, "status")
        if status and status.lower() != "ok":
            reason = stream_dl.first_value(info, "reason") or "Không có luồng video"
            raise RuntimeError(f"Lỗi lấy video stream: {reason}")

        streams = stream_dl.parse_video_streams(info)
        if not streams:
            raise RuntimeError("Không tìm thấy stream URL (streams list rỗng).")

        best   = stream_dl.choose_video_stream(streams)
        headers = {
            "Referer":         f"https://drive.google.com/file/d/{file_id}/preview",
            "Accept":          "*/*",
            "Accept-Encoding": "identity",
        }

        # Probe để lấy kích thước và kiểm tra Range support
        probe_headers = {**headers, "Range": "bytes=0-0"}
        probe = self.session.get(
            best.url, headers=probe_headers, stream=True, timeout=30
        )
        try:
            if probe.status_code == 403:
                raise RuntimeError(
                    "Stream URL trả về 403 ngay khi probe — cookie hết hạn hoặc "
                    "file bị hạn chế hoàn toàn."
                )
            if probe.status_code != 206:
                raise RuntimeError(
                    f"Luồng video không hỗ trợ HTTP Range (HTTP {probe.status_code}). "
                    "Cần fallback về tải file tạm."
                )
            cr    = probe.headers.get("Content-Range", "")
            match = re.search(r"/(\d+)$", cr)
            if not match:
                raise RuntimeError(
                    f"Không xác định được tổng kích thước từ Content-Range: {cr!r}"
                )
            size = int(match.group(1))
        finally:
            probe.close()

        UI.status(
            "STREAM",
            f"ID={file_id} | itag={best.itag} | {size/1_048_576:.1f} MB",
            indent=4,
        )
        return VideoStreamSource(
            url=best.url, headers=headers, size=size, itag=best.itag
        )

    def refresh_source(self, file_id: str) -> Optional[VideoStreamSource]:
        """Lấy lại stream URL mới khi URL cũ hết hạn."""
        try:
            UI.status("STREAM", f"Refresh URL cho file {file_id}...", indent=4)
            return self.get_video_source(file_id)
        except Exception as e:
            self.last_error = str(e)
            UI.warn(f"Refresh stream thất bại: {e}", indent=4)
            return None

    def download_to_temp(
        self, file_id: str, suffix: str = ".mp4", display_name: str | None = None
    ) -> Optional[Path]:
        """Fallback: tải file về đĩa tạm khi upload stream thất bại."""
        self.last_error = None
        try:
            source = self.get_video_source(file_id)
        except Exception as e:
            self.last_error = str(e)
            UI.warn(f"Không lấy được stream URL: {e}", indent=4)
            return None

        # mkstemp tạo file an toàn (không race condition như mktemp đã deprecated).
        # Đóng fd ngay vì IDMDownloader sẽ tự mở và ghi theo output_path.
        fd, tmp_name = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        tmp = Path(tmp_name)
        headers = dict(source.headers)
        size    = source.size
        written = 0
        progress_task = UI.start_stream_task(
            f"Tải tạm: {display_name or file_id}", size, phase="download"
        )
        success = False

        try:
            import idm_downloader
            import contextlib
            import io

            def _progress(downloaded: int, total: int, speed: float):
                UI.update_stream_task(progress_task, downloaded, total)

            downloader = idm_downloader.IDMDownloader(
                url=source.url,
                output_path=tmp,
                headers=headers,
                cookies=self.session.cookies.get_dict(),
                concurrency=32, # Tăng lên 32 luồng để tận dụng mạng 3500Mbps
                chunk_size=4 * 1024 * 1024, # 4MB chunk size
                min_split_size=16 * 1024 * 1024, # 16MB min split
                total_size=size,
                final_url=source.url,
                progress_callback=_progress,
            )
            # Tắt output stderr của idm_downloader để không dính chéo UI
            with contextlib.redirect_stderr(io.StringIO()):
                downloader.download_sync()
            success = True
            return tmp
        except Exception as e:
            self.last_error = str(e)
            UI.warn(f"Tải về tạm (IDM) thất bại: {e}", indent=4)
            tmp.unlink(missing_ok=True)
            return None
        finally:
            UI.finish_stream_task(progress_task, "OK" if success else "Lỗi")


# ─── DriveAPI ─────────────────────────────────────────────────────────────────

class DriveAPI:
    def __init__(self, creds: Credentials):
        self.service = build("drive", "v3", credentials=creds, cache_discovery=False)

    def list_children(self, folder_id: str) -> list:
        items, page_token = [], None
        while True:
            try:
                resp = self.service.files().list(
                    q=f"'{folder_id}' in parents and trashed = false",
                    pageSize=1000,
                    fields="files(id,name,mimeType,size,modifiedTime,shortcutDetails),nextPageToken",
                    pageToken=page_token,
                    corpora="allDrives",
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                ).execute()
                items.extend(without_drive_shortcuts(resp.get("files", [])))
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
            except HttpError as e:
                UI.warn(f"list_children({folder_id}): {e}", indent=2)
                break
        return items

    def find_in_parent(
        self, parent_id: str, name: str, mime_type: str | None = None
    ) -> str | None:
        try:
            sp = drive_query_literal(parent_id)
            sn = drive_query_literal(name)
            q  = [f"'{sp}' in parents", f"name = '{sn}'", "trashed = false"]
            if mime_type:
                q.append(f"mimeType = '{drive_query_literal(mime_type)}'")

            resp  = self.service.files().list(
                q=" and ".join(q), pageSize=10,
                fields="files(id,name,mimeType)",
                corpora="allDrives", supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            m = find_matching_item(resp.get("files", []), name, mime_type)
            if m:
                return m["id"]

            # Fallback: liệt kê hết và so tên mờ
            q2 = [f"'{sp}' in parents", "trashed = false"]
            if mime_type:
                q2.append(f"mimeType = '{drive_query_literal(mime_type)}'")
            all_items, pt = [], None
            while True:
                r2 = self.service.files().list(
                    q=" and ".join(q2), pageSize=1000,
                    fields="files(id,name,mimeType),nextPageToken",
                    pageToken=pt, corpora="allDrives",
                    supportsAllDrives=True, includeItemsFromAllDrives=True,
                ).execute()
                all_items.extend(r2.get("files", []))
                pt = r2.get("nextPageToken")
                if not pt:
                    break
            m = find_matching_item(all_items, name, mime_type)
            return m["id"] if m else None
        except HttpError:
            return None

    def find_folder_in_parent(self, parent_id: str, name: str) -> str | None:
        return self.find_in_parent(parent_id, name, FOLDER_MIME_TYPE)

    def get_metadata(self, file_id: str) -> dict | None:
        try:
            return self.service.files().get(
                fileId=file_id,
                fields="id,name,mimeType,size,modifiedTime,parents,trashed,shortcutDetails",
                supportsAllDrives=True,
            ).execute()
        except HttpError:
            return None

    def ensure_folder(self, parent_id: str, name: str) -> str | None:
        existing = self.find_folder_in_parent(parent_id, name)
        if existing:
            return existing
        try:
            body   = {"name": name, "mimeType": FOLDER_MIME_TYPE, "parents": [parent_id]}
            folder = self.service.files().create(
                body=body, fields="id", supportsAllDrives=True
            ).execute()
            return folder["id"]
        except HttpError as e:
            UI.error(f"ensure_folder({name}): {e}", indent=2)
            return None

    def copy_file(self, file_id: str, dest_parent_id: str, name: str | None = None) -> str:
        body = {"parents": [dest_parent_id]}
        if name is not None:
            body["name"] = name
        copied = self.service.files().copy(
            fileId=file_id,
            body=body,
            fields="id", supportsAllDrives=True,
        ).execute()
        return copied["id"]

    def rename_file(self, file_id: str, name: str) -> str:
        updated = self.service.files().update(
            fileId=file_id,
            body={"name": name},
            fields="id,name",
            supportsAllDrives=True,
        ).execute()
        return updated["id"]

    def trash_file(self, file_id: str) -> str:
        updated = self.service.files().update(
            fileId=file_id,
            body={"trashed": True},
            fields="id,trashed",
            supportsAllDrives=True,
        ).execute()
        return updated["id"]

    def upload_local_file(
        self, local_path: str, name: str, parent_id: str, mime_type: str = "video/mp4"
    ) -> str:
        total_size = os.path.getsize(local_path)
        progress_task = UI.start_stream_task(f"Upload fallback: {name}", total_size, phase="upload")
        media = MediaFileUpload(
            local_path, mimetype=mime_type,
            chunksize=UPLOAD_CHUNK_SIZE, resumable=True,
        )
        req  = self.service.files().create(
            body={"name": name, "parents": [parent_id]},
            media_body=media, fields="id", supportsAllDrives=True,
        )
        resp = None
        bar  = tqdm(total=total_size, unit="B",
                    unit_scale=True, desc=f"    ↑ {name[:40]}",
                    leave=False, dynamic_ncols=True) if TQDM_AVAILABLE and not progress_task else None
        prev = 0
        success = False
        try:
            while resp is None:
                status, resp = req.next_chunk(num_retries=3)
                if status:
                    cur = int(status.resumable_progress)
                    if bar:
                        bar.update(cur - prev)
                    UI.update_stream_task(progress_task, cur, total_size)
                    prev = cur
            success = True
            return resp["id"]
        finally:
            if bar:
                bar.close()
            UI.finish_stream_task(progress_task, "OK" if success else "Lỗi")

    def upload_from_http_stream(
        self,
        source: VideoStreamSource,
        session: req_lib.Session,
        name: str,
        parent_id: str,
        mime_type: str = "video/mp4",
        file_id_for_refresh: str | None = None,
        streamer: StreamDownloader | None = None,
    ) -> str:
        """
        Upload video stream trực tiếp, không qua file tạm.
        Tự động refresh URL nếu hết hạn trong lúc upload.
        """
        def _refresh():
            if file_id_for_refresh and streamer:
                return streamer.refresh_source(file_id_for_refresh)
            return None

        progress_task = UI.start_stream_task(f"Stream upload: {name}", source.size, phase="stream")
        bar = None
        if TQDM_AVAILABLE and not progress_task:
            bar = tqdm(
                total=source.size, unit="B", unit_scale=True,
                desc=f"    ↑ {name[:40]}", leave=False, dynamic_ncols=True,
            )
        prev_read = [0]

        def _progress(total_read: int, total: int):
            if bar:
                bar.update(total_read - prev_read[0])
            UI.update_stream_task(progress_task, total_read, total)
            prev_read[0] = total_read

        stream = SeekableHttpRangeStream(
            session, source,
            progress_callback=_progress,
            refresh_callback=_refresh,
        )
        media = MediaIoBaseUpload(
            stream, mimetype=mime_type,
            chunksize=UPLOAD_CHUNK_SIZE, resumable=True,
        )
        req  = self.service.files().create(
            body={"name": name, "parents": [parent_id]},
            media_body=media, fields="id", supportsAllDrives=True,
        )
        resp = None
        success = False
        try:
            while resp is None:
                _, resp = req.next_chunk(num_retries=3)
            success = True
            return resp["id"]
        finally:
            stream.close()
            if bar:
                bar.close()
            UI.finish_stream_task(progress_task, "OK" if success else "Lỗi")

    def export_workspace_file(
        self, file_id: str, mime_type: str, dest_parent_id: str, name: str
    ) -> str | None:
        """Xuất Google Workspace file (Doc/Slide/Drawing) sang PDF/PNG rồi upload."""
        spec = GOOGLE_WORKSPACE_EXPORT.get(mime_type)
        if not spec:
            return None
        ext, export_mime = spec

        export_url = (
            f"https://www.googleapis.com/drive/v3/files/{file_id}"
            f"/export?mimeType={export_mime}"
        )
        # Dùng requests với auth token; refresh nếu token đã hết hạn
        creds = self.service._http.credentials
        if not getattr(creds, "valid", True) and getattr(creds, "refresh_token", None):
            try:
                creds.refresh(Request())
            except Exception as e:
                UI.warn(f"Không refresh được token khi export {name}: {e}", indent=4)
        token = creds.token
        headers = {"Authorization": f"Bearer {token}"}
        resp = req_lib.get(export_url, headers=headers, stream=True, timeout=120)
        if resp.status_code >= 400:
            UI.warn(f"HTTP {resp.status_code} khi export {name}", indent=4)
            return None

        with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
            for chunk in resp.iter_content(chunk_size=64 * 1024 * 1024):
                if chunk:
                    tmp.write(chunk)
            tmp_path = tmp.name

        try:
            return self.upload_local_file(
                tmp_path, f"{name}.{ext}", dest_parent_id, export_mime
            )
        finally:
            Path(tmp_path).unlink(missing_ok=True)


# ─── ConfigManager ────────────────────────────────────────────────────────────

DEFAULT_SOURCE_FOLDER_IDS = ["107CZgk5F7iRX_b9Esa1avdOEkwTIMKIh"]
DEFAULT_DEST_FOLDER_ID = "1jzw1q9Chb9CQRdWXgu0qMP3jmMgmq8As"
DRIVE_FOLDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")
DRIVE_FOLDER_PATH_RE = re.compile(r"(?:^|/)folders/([A-Za-z0-9_-]+)")


def _clean_folder_input(value: object) -> str:
    return unquote(str(value)).strip().strip('"').strip("'").strip()


def extract_drive_folder_id(value: str) -> str:
    raw = _clean_folder_input(value)
    if not raw:
        raise ValueError("folder ID/link dang trong")

    candidates: list[str] = []
    parsed = urlparse(raw)
    if parsed.query:
        candidates.extend(parse_qs(parsed.query).get("id", []))

    path_match = DRIVE_FOLDER_PATH_RE.search(parsed.path or raw)
    if path_match:
        candidates.append(path_match.group(1))

    if not candidates:
        candidates.append(raw)

    for candidate in candidates:
        folder_id = _clean_folder_input(candidate)
        if DRIVE_FOLDER_ID_RE.fullmatch(folder_id):
            return folder_id

    raise ValueError(f"folder ID/link khong hop le: {raw}")


def extract_drive_folder_ids(value: str) -> list[str]:
    """Tach chuoi phan cach boi dau phay thanh danh sach folder ID."""
    ids = []
    for part in value.split(","):
        part = part.strip()
        if part:
            try:
                ids.append(extract_drive_folder_id(part))
            except ValueError:
                pass
    if not ids:
        raise ValueError(f"khong tim thay folder ID/link hop le nao trong: {value}")
    return ids


def _stdin_is_interactive() -> bool:
    return bool(getattr(sys.stdin, "isatty", lambda: False)())


def _normalize_configured_folder_id(label: str, source_name: str, value: object) -> str:
    try:
        return extract_drive_folder_id(str(value))
    except ValueError as exc:
        raise ValueError(f"{label} tu {source_name} khong hop le: {exc}") from exc


def resolve_folder_id(
    label: str,
    cli_value: str | None,
    positional_value: str | None,
    env_name: str,
    default: str,
    prompt_enabled: bool,
) -> str:
    direct_inputs = (
        ("tham so CLI", cli_value),
        ("tham so vi tri", positional_value),
    )
    for source_name, value in direct_inputs:
        if value is not None and _clean_folder_input(value):
            return _normalize_configured_folder_id(label, source_name, value)

    normalized_default = extract_drive_folder_id(default)
    if prompt_enabled and _stdin_is_interactive():
        while True:
            try:
                answer = input(f"{label} folder ID or URL [default: {normalized_default}]: ")
            except EOFError:
                return normalized_default

            if not _clean_folder_input(answer):
                return normalized_default
            try:
                return extract_drive_folder_id(answer)
            except ValueError as exc:
                UI.warn(f"{label} khong hop le: {exc}")

    env_value = os.environ.get(env_name)
    if env_value is not None and _clean_folder_input(env_value):
        return _normalize_configured_folder_id(
            label, f"bien moi truong {env_name}", env_value
        )

    return normalized_default


def resolve_folder_ids(
    label: str,
    cli_value: str | None,
    positional_value: str | None,
    env_name: str,
    default: list[str],
    prompt_enabled: bool,
) -> list[str]:
    """Giong resolve_folder_id nhung ho tro nhieu folder ID phan cach boi dau phay."""
    direct_inputs = (
        ("tham so CLI", cli_value),
        ("tham so vi tri", positional_value),
    )
    for source_name, value in direct_inputs:
        if value is not None and _clean_folder_input(value):
            try:
                return extract_drive_folder_ids(str(value))
            except ValueError as exc:
                raise ValueError(f"{label} tu {source_name} khong hop le: {exc}") from exc

    normalized_defaults = [extract_drive_folder_id(d) for d in default]
    default_display = ", ".join(normalized_defaults)
    if prompt_enabled and _stdin_is_interactive():
        while True:
            try:
                answer = input(
                    f"{label} folder ID/URL (nhieu link phan cach boi dau phay)"
                    f" [default: {default_display}]: "
                )
            except EOFError:
                return normalized_defaults

            if not _clean_folder_input(answer):
                return normalized_defaults
            try:
                return extract_drive_folder_ids(answer)
            except ValueError as exc:
                UI.warn(f"{label} khong hop le: {exc}")

    env_value = os.environ.get(env_name)
    if env_value is not None and _clean_folder_input(env_value):
        try:
            return extract_drive_folder_ids(env_value)
        except ValueError as exc:
            raise ValueError(
                f"{label} tu bien moi truong {env_name} khong hop le: {exc}"
            ) from exc

    return normalized_defaults


class ConfigManager:
    def __init__(self):
        self.SOURCE_FOLDER_IDS = DEFAULT_SOURCE_FOLDER_IDS
        self.DEST_FOLDER_ID   = DEFAULT_DEST_FOLDER_ID
        self.MAX_WORKERS         = int(os.environ.get("MAX_WORKERS", "64"))
        self.MAX_STREAM_WORKERS  = int(os.environ.get("MAX_STREAM_WORKERS", "32")) # Đã tăng lên 32 cho mạng siêu tốc
        # Mỗi PDF bị block được cứu bằng một Chromium headless riêng (~400MB RAM).
        # Giới hạn số trình duyệt chạy song song để tránh cạn RAM/CPU khi nhiều
        # worker cùng gặp PDF view-only một lúc (nguyên nhân các "Đường dẫn lỗi").
        self.MAX_PDF_WORKERS     = int(os.environ.get("MAX_PDF_WORKERS", "3"))
        self.MAX_DEPTH           = int(os.environ.get("MAX_DEPTH", "15"))
        self.RETRY_TIMES         = int(os.environ.get("RETRY_TIMES", "3"))
        self.TOKEN_FILE          = os.environ.get("TOKEN_FILE", "token.json")
        self.CHECKPOINT_FILE     = os.environ.get("CHECKPOINT_FILE", "sync_checkpoint.json")
        self.SYNC_STATE_FILE     = os.environ.get("SYNC_STATE_FILE", "sync_state.json")
        self.SMART_SCAN_ENABLED  = os.environ.get("SMART_SCAN_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
        self.FORCE_FULL_SCAN     = os.environ.get("FORCE_FULL_SCAN", "0").strip().lower() in {"1", "true", "yes", "on"}
        self.COOKIE_FILE         = os.environ.get("COOKIE_FILE", "cookie.txt")
        self.AUTO_COOKIE_REFRESH = os.environ.get(
            "AUTO_COOKIE_REFRESH", "1"
        ).strip().lower() not in {"0", "false", "no", "off"}
        self.COOKIE_REFRESH_TIMEOUT = float(os.environ.get("COOKIE_REFRESH_TIMEOUT", "0"))
        self.COOKIE_REFRESH_POLL_SECONDS = float(
            os.environ.get("COOKIE_REFRESH_POLL_SECONDS", "2")
        )
        self.BLOCKED_PDF_LOG_FILE = os.environ.get(
            "BLOCKED_PDF_LOG_FILE", "blocked_pdfs_log.md"
        )
        self.BLOCKED_PDF_LIST = os.environ.get("BLOCKED_PDF_LIST")
        self.BLOCKED_PDF_CHECKPOINT_FILE = os.environ.get(
            "BLOCKED_PDF_CHECKPOINT_FILE", "blocked_pdf_upload_checkpoint.json"
        )
        self.BLOCKED_PDF_CACHE_DIR = os.environ.get(
            "BLOCKED_PDF_CACHE_DIR", "downloaded_pdfs"
        )

    def apply_args(self, args: argparse.Namespace):
        self.SOURCE_FOLDER_IDS = resolve_folder_ids(
            "Source",
            getattr(args, "source_folder_id", None),
            getattr(args, "source_folder", None),
            "SOURCE_FOLDER_IDS",
            DEFAULT_SOURCE_FOLDER_IDS,
            not getattr(args, "no_input_prompt", False),
        )
        self.DEST_FOLDER_ID = resolve_folder_id(
            "Dest",
            getattr(args, "dest_folder_id", None),
            getattr(args, "dest_folder", None),
            "DEST_FOLDER_ID",
            DEFAULT_DEST_FOLDER_ID,
            not getattr(args, "no_input_prompt", False),
        )

        source_dest_args = {
            "source_folder_id",
            "dest_folder_id",
            "source_folder",
            "dest_folder",
            "no_input_prompt",
        }
        for attr, value in vars(args).items():
            if attr not in source_dest_args and value is not None:
                setattr(self, attr.upper(), value)


# ─── Authenticator ────────────────────────────────────────────────────────────

class CookieRefreshManager:
    def __init__(self, config: ConfigManager):
        self.config = config
        self.lock = threading.Lock()
        self.version = 0
        self.fingerprint = self._fingerprint(Path(self.config.COOKIE_FILE))

    def load_into_session(self, session: req_lib.Session, announce: bool = False) -> bool:
        cookie_path = Path(self.config.COOKIE_FILE)
        if not cookie_path.exists():
            if announce:
                UI.warn(f"Không tìm thấy {self.config.COOKIE_FILE}.")
            return False

        try:
            session.cookies.clear()
            _load_cookie_file_into_session(session, cookie_path)
            self.fingerprint = self._fingerprint(cookie_path)
            if announce:
                UI.status(
                    "COOKIE",
                    f"Đã nạp Cookie từ {self.config.COOKIE_FILE} (version {self.version}).",
                )
            return True
        except Exception as e:
            if announce:
                UI.warn(f"Lỗi nạp Cookie: {e}")
            return False

    def refresh_after_auth_error(self, reason: Exception | str, observed_version: int) -> bool:
        if not self.config.AUTO_COOKIE_REFRESH:
            return False

        with self.lock:
            if self.version != observed_version:
                return True

            cookie_path = Path(self.config.COOKIE_FILE)
            old_fingerprint = self.fingerprint or self._fingerprint(cookie_path)
            UI.section("Cookie Google Drive có thể đã hết hạn")
            UI.warn(f"Lý do: {reason}", indent=4)
            UI.info(f"File cookie hiện tại: {cookie_path.resolve(strict=False)}", indent=4)

            if self._stdin_is_interactive():
                return self._refresh_interactive(old_fingerprint)
            return self._refresh_by_polling(old_fingerprint)

    def _refresh_interactive(self, old_fingerprint: str | None) -> bool:
        while True:
            answer = input(
                "    Ghi đè file cookie rồi Enter, hoặc nhập đường dẫn cookie mới "
                "(q để bỏ qua): "
            ).strip().strip('"')
            if answer.lower() in {"q", "quit", "skip"}:
                return False
            if answer:
                self.config.COOKIE_FILE = answer

            cookie_path = Path(self.config.COOKIE_FILE)
            if not cookie_path.exists():
                UI.warn(f"Không tìm thấy file cookie: {cookie_path}", indent=4)
                continue

            new_fingerprint = self._fingerprint(cookie_path)
            if old_fingerprint and new_fingerprint == old_fingerprint and not answer:
                UI.warn("File cookie chưa thay đổi; vẫn nạp lại để thử tiếp.", indent=4)
            self.fingerprint = new_fingerprint
            self.version += 1
            UI.status("COOKIE", f"Đã nhận cookie mới: {cookie_path.resolve(strict=False)}", indent=4)
            return True

    def _refresh_by_polling(self, old_fingerprint: str | None) -> bool:
        timeout = float(self.config.COOKIE_REFRESH_TIMEOUT or 0)
        if timeout <= 0:
            UI.warn(
                "Không có console để nhập cookie mới. "
                "Đặt COOKIE_REFRESH_TIMEOUT>0 nếu muốn chờ file cookie thay đổi.",
                indent=4,
            )
            return False

        deadline = time.monotonic() + timeout
        poll = max(float(self.config.COOKIE_REFRESH_POLL_SECONDS or 2), 0.5)
        UI.status("COOKIE", f"Đang chờ cookie file thay đổi trong {timeout:.0f}s...", indent=4)
        while time.monotonic() < deadline:
            cookie_path = Path(self.config.COOKIE_FILE)
            new_fingerprint = self._fingerprint(cookie_path)
            if new_fingerprint and new_fingerprint != old_fingerprint:
                self.fingerprint = new_fingerprint
                self.version += 1
                UI.status("COOKIE", f"Đã phát hiện cookie mới: {cookie_path.resolve(strict=False)}", indent=4)
                return True
            time.sleep(poll)

        UI.warn("Hết thời gian chờ cookie mới.", indent=4)
        return False

    @staticmethod
    def _stdin_is_interactive() -> bool:
        try:
            return bool(sys.stdin and sys.stdin.isatty())
        except Exception:
            return False

    @staticmethod
    def _fingerprint(path: Path) -> str | None:
        try:
            data = path.read_bytes()
            return hashlib.sha256(data).hexdigest()
        except OSError:
            return None


class Authenticator:
    def __init__(self, config: ConfigManager):
        self.config     = config
        self.api_creds: Credentials | None = None
        self.cookie_manager = CookieRefreshManager(config)

    def authenticate_api(self):
        if not os.path.exists(self.config.TOKEN_FILE):
            raise FileNotFoundError(f"Không tìm thấy {self.config.TOKEN_FILE}")
        with open(self.config.TOKEN_FILE) as f:
            info = json.load(f)
        self.api_creds = Credentials.from_authorized_user_info(info)
        if not self.api_creds.valid:
            if self.api_creds.expired and self.api_creds.refresh_token:
                self.api_creds.refresh(Request())
        UI.success("Xác thực Google Drive API thành công.")

    def build_web_session(self, announce: bool = False) -> req_lib.Session:
        """
        Cải tiến: reuse session adapter với retry built-in.
        """
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        session = req_lib.Session()
        session.headers.update({
            "User-Agent": stream_dl.DEFAULT_USER_AGENT,
            "Accept":     "*/*",
            "Referer":    "https://drive.google.com/",
        })

        # Retry tự động cho 5xx và connection error
        retry = Retry(
            total=3, backoff_factor=1.0,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods={"GET"},
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://",  adapter)

        self.cookie_manager.load_into_session(session, announce=announce)
        return session

    def authenticate_web(self):
        self.web_session = self.build_web_session(announce=True)


# ─── SyncEngine ───────────────────────────────────────────────────────────────

class SyncEngine:
    """
    Cải tiến:
    - Session pool: mỗi thread tái sử dụng một session riêng (không tạo mới mỗi lần)
    - Fallback chain rõ ràng cho file bị block:
        1. copy API
        2. stream upload trực tiếp (nếu là video và có cookie)
        3. tải về file tạm rồi upload (nếu stream upload thất bại)
        4. export workspace sang PDF (nếu là Google Workspace type)
    - Progress bar cho bước 2 và 3
    """

    def __init__(self, config: ConfigManager, auth: Authenticator):
        self.config = config
        self.auth   = auth

        self.lock              = threading.Lock()
        self.folder_lock       = threading.Lock()
        self.parent_index_lock = threading.Lock()
        self.folder_cache:       dict[tuple[str, str], str]    = {}
        self.dest_parent_items: dict[str, list[dict]]          = {}
        self.stream_semaphore = threading.BoundedSemaphore(config.MAX_STREAM_WORKERS)
        self.pdf_semaphore = threading.BoundedSemaphore(config.MAX_PDF_WORKERS)
        self.sync_state = SyncState(
            config.SYNC_STATE_FILE,
            ",".join(config.SOURCE_FOLDER_IDS),
            config.DEST_FOLDER_ID,
            enabled=config.SMART_SCAN_ENABLED,
            force_full_scan=config.FORCE_FULL_SCAN,
        )
        self.bad_paths: list[str] = []
        self.stats_lock = threading.Lock()
        self.stats: Counter[str] = Counter()

        # Blocked PDF log: list of {source_path, source_file_id, dest_path}
        self.blocked_pdf_log: list[dict] = []
        self.blocked_pdf_lock = threading.Lock()
        self.blocked_pdf_upload_lock = threading.Lock()
        self.blocked_pdf_upload_checkpoint = self._load_checkpoint(
            config.BLOCKED_PDF_CHECKPOINT_FILE
        )

        # Thread-local storage: mỗi worker có DriveAPI, StreamDownloader, Session riêng
        self._tlocal = threading.local()

        # Checkpoint
        self.checkpoint: dict = self._load_checkpoint(config.CHECKPOINT_FILE)
        self._save_counter = 0

    # ── Thread-local services ─────────────────────────────────────────────────

    def _drive_api(self) -> DriveAPI:
        """Lấy DriveAPI của thread hiện tại, tạo mới nếu chưa có."""
        api = getattr(self._tlocal, "drive_api", None)
        if api is None:
            api = DriveAPI(self.auth.api_creds)
            self._tlocal.drive_api = api
        return api

    def _streamer(self) -> StreamDownloader:
        """Lấy StreamDownloader (cùng session) của thread hiện tại."""
        sd = getattr(self._tlocal, "streamer", None)
        current_cookie_version = self.auth.cookie_manager.version
        if sd is None or getattr(sd, "cookie_version", -1) != current_cookie_version:
            sd = StreamDownloader(self.auth.build_web_session())
            sd.cookie_version = current_cookie_version
            self._tlocal.streamer = sd
        return sd

    def _refresh_cookie_for_streamer(
        self, streamer: StreamDownloader, reason: Exception | str
    ) -> bool:
        observed_version = getattr(streamer, "cookie_version", self.auth.cookie_manager.version)
        refreshed = self.auth.cookie_manager.refresh_after_auth_error(
            reason, observed_version
        )
        if not refreshed:
            return False

        streamer.session = self.auth.build_web_session(announce=True)
        streamer.cookie_version = self.auth.cookie_manager.version
        self._tlocal.streamer = streamer
        return True

    # ── Checkpoint ────────────────────────────────────────────────────────────

    @staticmethod
    def _load_checkpoint(path: str) -> dict:
        """Nạp checkpoint, ưu tiên file .tmp còn sót nếu file chính hỏng.

        Trả về dict rỗng nếu cả hai đều không đọc được, nhưng cảnh báo rõ ràng
        để người dùng biết tiến trình cũ có thể đã mất."""
        for candidate in (path, f"{path}.tmp"):
            if not os.path.exists(candidate):
                continue
            try:
                with open(candidate, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    if candidate != path:
                        UI.warn(f"Checkpoint chính hỏng, khôi phục từ {candidate}.")
                    return data
                UI.warn(f"Checkpoint {candidate} sai định dạng (không phải dict).")
            except (OSError, json.JSONDecodeError) as e:
                UI.warn(f"Không đọc được checkpoint {candidate}: {e}")
        return {}

    def _save_checkpoint(self, src_id: str, dest_id: str):
        snapshot = None
        with self.lock:
            self.checkpoint[src_id] = dest_id
            self._save_counter += 1
            if self._save_counter % 20 == 0:
                snapshot = dict(self.checkpoint)
        # Ghi ra đĩa NGOÀI lock để không chặn các worker khác trong lúc I/O.
        if snapshot is not None:
            self._flush_checkpoint(snapshot)

    def _flush_checkpoint(self, snapshot: dict | None = None):
        """Ghi checkpoint nguyên tử: ghi file tạm rồi os.replace để tránh
        hỏng file nếu tiến trình bị kill giữa chừng."""
        path = self.config.CHECKPOINT_FILE
        tmp_path = f"{path}.tmp"
        if snapshot is None:
            with self.lock:
                snapshot = dict(self.checkpoint)
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except OSError as e:
            UI.warn(f"Không ghi được checkpoint: {e}")
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def _flush_blocked_pdf_log(self):
        """Append blocked PDF entries to the Markdown log file (append-safe).

        Each run appends a new section with a timestamp header and one row per
        blocked PDF in the format:
            - [tên file](link file) - [folder đích](link folder đích)

        Writes atomically via a .tmp file.
        """
        with self.blocked_pdf_lock:
            new_entries = list(self.blocked_pdf_log)

        if not new_entries:
            return

        log_path = self.config.BLOCKED_PDF_LOG_FILE
        tmp_path = f"{log_path}.tmp"

        # Read existing content (if any) to append to it
        existing_content = ""
        if os.path.exists(log_path):
            try:
                with open(log_path, encoding="utf-8") as f:
                    existing_content = f.read()
            except OSError as e:
                UI.warn(f"Không đọc được log PDF bị block cũ ({log_path}): {e}")

        # Build new section for this run
        run_time = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        lines = [
            f"\n## Run: {run_time} — {len(new_entries)} file bị block\n",
            "| File bị block | Folder đích |\n",
            "|---|---|\n",
        ]
        for entry in new_entries:
            name             = entry.get("name", "")
            file_link        = entry.get("file_link", "")
            dest_folder_link = entry.get("dest_folder_link", "")
            lines.append(f"| [{name}]({file_link}) | [folder đích]({dest_folder_link}) |\n")

        new_section = "".join(lines)

        # If file is brand new, prepend a top-level heading
        if not existing_content.strip():
            header = "# PDF bị block — không thể download\n"
            full_content = header + new_section
        else:
            full_content = existing_content.rstrip("\n") + "\n" + new_section

        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(full_content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, log_path)
            UI.info(
                f"Đã lưu {len(new_entries)} PDF bị block vào {log_path}."
            )
        except OSError as e:
            UI.warn(f"Không ghi được log PDF bị block: {e}")
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def _mark_bad_path(self, path: str):
        with self.lock:
            self.bad_paths.append(path)

    def _record_blocked_pdf(
        self,
        file_id: str,
        source_path: str,
        dest_folder_id: str,
        name: str,
    ):
        """Record a PDF that was blocked and could not be copied.

        source_path   — full Drive path of the file in the source folder tree
                        (e.g. "SOURCE_1/Khóa học A/Bài 1/lecture.pdf")
        file_link     — https://drive.google.com/file/d/<file_id>/view
        dest_folder_link — https://drive.google.com/drive/folders/<dest_folder_id>
        """
        file_link        = f"https://drive.google.com/file/d/{file_id}/view"
        dest_folder_link = f"https://drive.google.com/drive/folders/{dest_folder_id}"
        entry = {
            "name":             name,
            "file_id":          file_id,
            "file_link":        file_link,
            "dest_folder_id":   dest_folder_id,
            "dest_folder_link": dest_folder_link,
            "source_path":      source_path,
            "recorded_at":      datetime.utcnow().isoformat() + "Z",
        }
        with self.blocked_pdf_lock:
            self.blocked_pdf_log.append(entry)
        UI.status(
            "BLOCKED",
            f"PDF bị block: {file_link}  →  folder đích: {dest_folder_link}",
            indent=4,
        )

    def _blocked_pdf_checkpoint_key(self, file_id: str, dest_folder_id: str) -> str:
        return f"{file_id}:{dest_folder_id}"

    def _save_blocked_pdf_upload_checkpoint(
        self, entry: BlockedPdfEntry, dest_file_id: str
    ):
        key = self._blocked_pdf_checkpoint_key(entry.file_id, entry.dest_folder_id)
        with self.blocked_pdf_upload_lock:
            self.blocked_pdf_upload_checkpoint[key] = {
                "source_file_id": entry.file_id,
                "dest_folder_id": entry.dest_folder_id,
                "dest_file_id": dest_file_id,
                "name": entry.name,
                "saved_at": datetime.utcnow().isoformat() + "Z",
            }
            snapshot = dict(self.blocked_pdf_upload_checkpoint)
        self._flush_json_atomic(self.config.BLOCKED_PDF_CHECKPOINT_FILE, snapshot)

    def _checkpoint_dest_file_id(self, checkpoint_hit: object) -> str | None:
        if isinstance(checkpoint_hit, dict):
            dest_id = checkpoint_hit.get("dest_file_id")
            return str(dest_id) if dest_id else None
        if isinstance(checkpoint_hit, str):
            return checkpoint_hit
        return None

    def _legacy_truncated_pdf_name(self, name: str) -> str | None:
        last_bracket = name.rfind("[")
        if last_bracket <= 0:
            return None
        truncated = name[last_bracket + 1:].strip()
        if not truncated or truncated == name:
            return None
        return truncated

    def _trash_legacy_name_duplicates(
        self, api: DriveAPI, entry: BlockedPdfEntry, keep_file_id: str | None
    ):
        legacy_name = self._legacy_truncated_pdf_name(entry.name)
        if not legacy_name:
            return
        items = api.list_children(entry.dest_folder_id)
        for item in items:
            item_id = item.get("id")
            if not item_id or item_id == keep_file_id:
                continue
            if item.get("mimeType") != "application/pdf":
                continue
            if not same_drive_name(str(item.get("name", "")), legacy_name):
                continue
            try:
                api.trash_file(item_id)
                self._count("deduped")
                UI.status(
                    "DEDUP",
                    f"Dua ban ten cu vao thung rac: {item.get('name')}",
                    indent=4,
                )
            except Exception as e:
                self._count("errors")
                UI.warn(f"Khong trash duoc PDF duplicate {item_id}: {e}", indent=4)

    def _trash_same_name_duplicates(
        self, api: DriveAPI, entry: BlockedPdfEntry, keep_file_id: str | None
    ):
        if not keep_file_id:
            return
        items = api.list_children(entry.dest_folder_id)
        for item in items:
            item_id = item.get("id")
            if not item_id or item_id == keep_file_id:
                continue
            if item.get("mimeType") != "application/pdf":
                continue
            if not same_drive_name(str(item.get("name", "")), entry.name):
                continue
            try:
                api.trash_file(item_id)
                self._count("deduped")
                UI.status(
                    "DEDUP",
                    f"Dua ban trung ten vao thung rac: {item.get('name')}",
                    indent=4,
                )
            except Exception as e:
                self._count("errors")
                UI.warn(f"Khong trash duoc PDF trung ten {item_id}: {e}", indent=4)

    def _ensure_blocked_pdf_dest_name(
        self, api: DriveAPI, entry: BlockedPdfEntry, dest_file_id: str | None
    ):
        if not dest_file_id:
            return
        meta = api.get_metadata(dest_file_id) or {}
        current_name = meta.get("name")
        if current_name == entry.name:
            self._trash_legacy_name_duplicates(api, entry, dest_file_id)
            self._trash_same_name_duplicates(api, entry, dest_file_id)
            return
        existing_correct = self._find_dest_item_id(
            api, entry.dest_folder_id, entry.name, "application/pdf", force_refresh=True
        )
        if existing_correct and existing_correct != dest_file_id:
            try:
                api.trash_file(dest_file_id)
                self._count("deduped")
                UI.status(
                    "DEDUP",
                    f"Da co file dung ten; dua ban sai ten vao thung rac: {current_name or dest_file_id}",
                    indent=4,
                )
                self._save_blocked_pdf_upload_checkpoint(entry, existing_correct)
                self._trash_same_name_duplicates(api, entry, existing_correct)
                return
            except Exception as e:
                self._count("errors")
                UI.warn(f"Khong trash duoc PDF duplicate {dest_file_id}: {e}", indent=4)
                return
        try:
            api.rename_file(dest_file_id, entry.name)
            self._count("renamed")
            UI.status("RENAME", f"{current_name or dest_file_id} -> {entry.name}", indent=4)
            self._save_blocked_pdf_upload_checkpoint(entry, dest_file_id)
            self._trash_same_name_duplicates(api, entry, dest_file_id)
        except Exception as e:
            self._count("errors")
            UI.warn(f"Khong rename duoc PDF {dest_file_id} thanh {entry.name}: {e}", indent=4)

    @staticmethod
    def _flush_json_atomic(path: str, data: dict):
        tmp_path = f"{path}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except OSError as e:
            UI.warn(f"Khong ghi duoc {path}: {e}")
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def _load_blocked_pdf_cookie_dict(self) -> dict:
        cookie_path = Path(self.config.COOKIE_FILE)
        if not cookie_path.exists():
            return {}

        try:
            cookie_dict = _load_cookie_file_as_dict(cookie_path)
            if cookie_dict:
                return cookie_dict
        except Exception:
            pass

        if not PLAYWRIGHT_AVAILABLE:
            return {}

        try:
            return load_cookies(str(cookie_path))
        except Exception as e:
            UI.warn(f"Khong nap duoc cookie PDF fallback tu {cookie_path}: {e}", indent=4)
            return {}

    def _pdf_rescue_session(self) -> req_lib.Session:
        auth = getattr(self, "auth", None)
        if auth and hasattr(auth, "build_web_session"):
            session = auth.build_web_session()
        else:
            session = req_lib.Session()
        token_file = getattr(self.config, "TOKEN_FILE", "token.json")
        token = load_oauth_token(token_file) if PLAYWRIGHT_AVAILABLE else None
        if token:
            session.headers.update({"Authorization": f"Bearer {token}"})
        return session

    def _blocked_pdf_cache_path(self, entry: BlockedPdfEntry) -> Path:
        cache_dir = Path(getattr(self.config, "BLOCKED_PDF_CACHE_DIR", "downloaded_pdfs"))
        safe_name = sanitize_local_filename(entry.name)
        path = cache_dir / safe_name
        if path.suffix.lower() != ".pdf":
            path = path.with_suffix(".pdf")
        return path

    def _download_blocked_pdf_to_cache(
        self,
        entry: BlockedPdfEntry,
        expected_size: int = 0,
    ) -> Path | None:
        cache_path = self._blocked_pdf_cache_path(entry)
        if cache_path.exists() and _validate_pdf(cache_path, expected_size):
            UI.status("SKIP", f"Da co PDF cache hop le: {cache_path}", indent=4)
            return cache_path

        if not PLAYWRIGHT_AVAILABLE:
            UI.status("BLOCKED", "Thieu batch_cookie_downloader.py de cuu PDF bi block", indent=4)
            return None

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        session = self._pdf_rescue_session()

        UI.status("FALLBACK", f"{entry.name} - thu direct /uc...", indent=4)
        try:
            if download_direct_uc(session, entry.file_id, cache_path):
                pdf_path = cache_path.with_suffix(".pdf")
                if _validate_pdf(pdf_path, expected_size):
                    return pdf_path
                pdf_path.unlink(missing_ok=True)
        except Exception as e:
            UI.warn(f"Direct /uc fail {entry.name}: {e}", indent=4)

        cookie_dict = self._load_blocked_pdf_cookie_dict()
        access_token = load_oauth_token(getattr(self.config, "TOKEN_FILE", "token.json"))

        for attempt in range(1, self.config.RETRY_TIMES + 1):
            try:
                ok = download_playwright_viewer(
                    file_id=entry.file_id,
                    mime_type="application/pdf",
                    output_path=cache_path,
                    cookie_dict=cookie_dict,
                    headless=True,
                    access_token=access_token,
                    expected_size=expected_size,
                )
            except Exception as e:
                ok = False
                UI.warn(f"Playwright capture loi (lan {attempt}) {entry.name}: {e}", indent=4)

            pdf_path = cache_path.with_suffix(".pdf")
            if ok and _validate_pdf(pdf_path, expected_size):
                return pdf_path

            pdf_path.unlink(missing_ok=True)
            if attempt < self.config.RETRY_TIMES:
                delay = exponential_backoff(attempt)
                UI.status(
                    "RETRY",
                    f"Playwright {entry.name} - doi {delay:.0f}s roi thu lai ({attempt}/{self.config.RETRY_TIMES})",
                    indent=4,
                )
                time.sleep(delay)

        self._count("blocked")
        UI.status("BLOCKED", f"{entry.name} - khong cuu duoc PDF", indent=4)
        return None

    def _process_blocked_pdf_entry(self, api: DriveAPI, entry: BlockedPdfEntry) -> bool:
        key = self._blocked_pdf_checkpoint_key(entry.file_id, entry.dest_folder_id)
        with self.blocked_pdf_upload_lock:
            checkpoint_hit = self.blocked_pdf_upload_checkpoint.get(key)
        if checkpoint_hit:
            self._count("checkpoint")
            UI.status("SKIP", f"Checkpoint PDF: {entry.name}", indent=4)
            self._ensure_blocked_pdf_dest_name(
                api, entry, self._checkpoint_dest_file_id(checkpoint_hit)
            )
            return True

        existing = self._find_dest_item_id(
            api, entry.dest_folder_id, entry.name, "application/pdf", force_refresh=True
        )
        if existing:
            self._count("skipped")
            UI.status("SKIP", f"Da ton tai o dich: {entry.name}", indent=4)
            self._save_blocked_pdf_upload_checkpoint(entry, existing)
            return True

        meta = api.get_metadata(entry.file_id) or {}
        expected_size = int(meta.get("size") or 0)
        pdf_path = self._download_blocked_pdf_to_cache(entry, expected_size)
        if not pdf_path:
            self._record_blocked_pdf(
                entry.file_id,
                source_path=entry.name,
                dest_folder_id=entry.dest_folder_id,
                name=entry.name,
            )
            return False

        upload_name = entry.name if entry.name.lower().endswith(".pdf") else entry.name + ".pdf"
        try:
            dest_id = api.upload_local_file(
                str(pdf_path),
                name=upload_name,
                parent_id=entry.dest_folder_id,
                mime_type="application/pdf",
            )
            self._count("fallback_uploaded")
            UI.status("FALLBACK", f"OK PDF: {entry.name}", indent=4)
            self._remember_dest_item(
                entry.dest_folder_id,
                {"id": dest_id, "name": upload_name, "mimeType": "application/pdf"},
            )
            self._save_blocked_pdf_upload_checkpoint(entry, dest_id)
            return True
        except Exception as e:
            self._count("errors")
            UI.error(f"Upload PDF fail {entry.name}: {e}", indent=4)
            return False

    def replay_blocked_pdf_list(self, list_path: str) -> bool:
        started_at = time.monotonic()
        entries = parse_blocked_pdf_markdown(list_path)
        UI.banner(
            "REPLAY PDF BI BLOCK",
            [
                ("List", str(list_path)),
                ("PDF", str(len(entries))),
                ("Cache", self.config.BLOCKED_PDF_CACHE_DIR),
                ("Checkpoint", self.config.BLOCKED_PDF_CHECKPOINT_FILE),
            ],
        )
        if not entries:
            UI.warn(f"Khong tim thay PDF nao trong {list_path}")
            return False

        api = self._drive_api()
        ok = 0
        failed = 0
        for idx, entry in enumerate(entries, start=1):
            UI.status("PDF", f"[{idx}/{len(entries)}] {entry.name}")
            if self._process_blocked_pdf_entry(api, entry):
                ok += 1
            else:
                failed += 1

        self._flush_blocked_pdf_log()
        stats = self._stats_snapshot()
        UI.summary(
            "Tong ket replay PDF",
            [
                ("Thoi gian chay", format_duration(time.monotonic() - started_at)),
                ("Thanh cong/skip", ok),
                ("That bai", failed),
                ("Upload PDF", stats.get("fallback_uploaded", 0)),
                ("Da ton tai", stats.get("skipped", 0)),
                ("Checkpoint", stats.get("checkpoint", 0)),
                ("Rename", stats.get("renamed", 0)),
                ("Dedup", stats.get("deduped", 0)),
                ("Blocked", stats.get("blocked", 0)),
                ("Loi", stats.get("errors", 0)),
            ],
        )
        return failed == 0

    def _count(self, key: str, amount: int = 1):
        with self.stats_lock:
            self.stats[key] += amount
            value = self.stats[key]
        UI.update_counter(key, value)

    def _stats_value(self, key: str) -> int:
        with self.stats_lock:
            return self.stats.get(key, 0)

    def _stats_snapshot(self) -> Counter[str]:
        with self.stats_lock:
            return Counter(self.stats)

    # ── Dest folder cache ─────────────────────────────────────────────────────

    def _get_dest_parent_items(
        self, api: DriveAPI, parent_id: str, force_refresh: bool = False
    ) -> list[dict]:
        if not force_refresh:
            with self.parent_index_lock:
                cached = self.dest_parent_items.get(parent_id)
                if cached is not None:
                    return cached
        items = api.list_children(parent_id)
        with self.parent_index_lock:
            if force_refresh:
                self.dest_parent_items[parent_id] = items
            else:
                self.dest_parent_items.setdefault(parent_id, items)
        return items

    def _remember_dest_item(self, parent_id: str, item: dict):
        item_id = item.get("id")
        if not item_id:
            return
        with self.parent_index_lock:
            cached = self.dest_parent_items.get(parent_id)
            if cached is None:
                return
            if not any(e.get("id") == item_id for e in cached):
                cached.append(item)

    def _find_dest_item_id(
        self, api: DriveAPI, parent_id: str, name: str,
        mime_type: str | None = None, force_refresh: bool = False,
    ) -> str | None:
        items = self._get_dest_parent_items(api, parent_id, force_refresh)
        m     = find_matching_item(items, name, mime_type)
        return m["id"] if m else None

    def ensure_dest_folder(
        self, api: DriveAPI, src_id: str, dest_parent: str, name: str
    ) -> str | None:
        key = (dest_parent, normalize_drive_name(name))
        with self.folder_lock:
            if key in self.folder_cache:
                return self.folder_cache[key]
            existing = self._find_dest_item_id(api, dest_parent, name, FOLDER_MIME_TYPE)
            if existing:
                self.folder_cache[key] = existing
                self._save_checkpoint(src_id, existing)
                return existing

            # Kiểm tra checkpoint
            with self.lock:
                cp_id = self.checkpoint.get(src_id)
            if cp_id:
                meta = api.get_metadata(cp_id)
                if (meta and not meta.get("trashed")
                        and same_drive_name(meta.get("name", ""), name, loose_folder=True)
                        and meta.get("mimeType") == FOLDER_MIME_TYPE
                        and dest_parent in meta.get("parents", [])):
                    self.folder_cache[key] = cp_id
                    return cp_id

            created = api.ensure_folder(dest_parent, name)
            if created:
                self.folder_cache[key] = created
                self._remember_dest_item(
                    dest_parent, {"id": created, "name": name, "mimeType": FOLDER_MIME_TYPE}
                )
                self._save_checkpoint(src_id, created)
            return created

    # ── Core file processing ──────────────────────────────────────────────────

    def process_single_file(
        self, api: DriveAPI, streamer: StreamDownloader,
        src_file: dict, dest_parent_id: str, path: str,
    ) -> bool:
        file_id   = src_file["id"]
        name      = src_file["name"]
        mime_type = src_file.get("mimeType", "")
        copy_file_id = src_file.get("_copy_file_id", file_id)
        checkpoint_key = src_file.get("_checkpoint_key", file_id)
        self._count("files_seen")

        # 0. Skip mime types
        skip_reason = skipped_file_reason(src_file)
        if skip_reason:
            self._count("skipped")
            UI.status("SKIP", f"{skip_reason}: {name}", indent=4)
            return True

        # 1. Checkpoint
        if checkpoint_key in self.checkpoint:
            self._count("checkpoint")
            return True

        # 2. Kiểm tra đã tồn tại ở đích
        existing = self._find_dest_item_id(api, dest_parent_id, name, mime_type)
        if existing:
            self._count("skipped")
            UI.status("SKIP", f"Đã tồn tại: {name}", indent=4)
            self._save_checkpoint(checkpoint_key, existing)
            return True

        # 3. Thử copy API (có retry + exponential backoff)
        for attempt in range(1, self.config.RETRY_TIMES + 1):
            try:
                dest_id = api.copy_file(copy_file_id, dest_parent_id, name=name)
                self._count("copied")
                UI.status("COPY", name, indent=4)
                self._remember_dest_item(
                    dest_parent_id, {"id": dest_id, "name": name, "mimeType": mime_type}
                )
                self._save_checkpoint(checkpoint_key, dest_id)
                return True

            except HttpError as e:
                if is_retryable_error(e):
                    if attempt < self.config.RETRY_TIMES:
                        delay = exponential_backoff(attempt)
                        UI.status("RETRY", f"{name} — đợi {delay:.0f}s: {e}", indent=4)
                        time.sleep(delay)
                        continue
                    self._count("errors")
                    UI.error(f"{name} — hết retry: {e}", indent=4)
                    return False

                if is_blocked_error(e):
                    # Kiểm tra lại thực tế ở đích (đôi khi file đã được copy bởi worker khác)
                    actual = self._find_dest_item_id(
                        api, dest_parent_id, name, mime_type, force_refresh=True
                    )
                    if actual:
                        self._count("skipped")
                        UI.status("SKIP", f"Bị block nhưng đã có ở đích: {name}", indent=4)
                        self._save_checkpoint(checkpoint_key, actual)
                        return True

                    # Xử lý file bị block
                    return self._handle_blocked_file(
                        api, streamer, src_file, dest_parent_id, name,
                        copy_file_id, mime_type, path, checkpoint_key,
                    )

                if attempt < self.config.RETRY_TIMES:
                    delay = exponential_backoff(attempt)
                    UI.status("RETRY", f"{name} — đợi {delay:.0f}s: {e}", indent=4)
                    time.sleep(delay)
                else:
                    self._count("errors")
                    UI.error(f"{name}: {e}", indent=4)
                    return False

            except Exception as e:
                self._count("errors")
                UI.error(f"{name}: {e}", indent=4)
                return False

        return False

    def _is_video_file(self, mime_type: str, name: str) -> bool:
        return (
            "video" in mime_type.lower()
            or name.lower().endswith((".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"))
        )

    def _handle_blocked_file(
        self, api: DriveAPI, streamer: StreamDownloader,
        src_file: dict, dest_parent_id: str, name: str,
        file_id: str, mime_type: str, path: str, checkpoint_key: str | None = None,
    ) -> bool:
        """
        Fallback chain cho file bị block:
          A. Video → stream upload trực tiếp
          B. Video → nếu A thất bại → tải về tạm rồi upload
          C. Google Workspace → export PDF/PNG rồi upload
          D. Còn lại → ghi nhận BLOCKED
        """
        # C. Google Workspace
        if mime_type in GOOGLE_WORKSPACE_EXPORT:
            UI.status("EXPORT", f"{name} — xuất sang PDF/PNG...", indent=4)
            try:
                dest_id = api.export_workspace_file(
                    file_id, mime_type, dest_parent_id, name
                )
                if dest_id:
                    self._count("exported")
                    UI.status("EXPORT", f"OK: {name}", indent=4)
                    self._save_checkpoint(checkpoint_key or file_id, dest_id)
                    return True
            except Exception as e:
                UI.error(f"Export fail {name}: {e}", indent=4)
            self._count("errors")
            return False

        # A+B. Video
        if self._is_video_file(mime_type, name):
            with self.stream_semaphore:
                return self._handle_blocked_video(
                        api, streamer, dest_parent_id, name,
                        file_id, mime_type, checkpoint_key or file_id,
                )

        # D. PDF/non-video bị block → Playwright viewer capture
        if mime_type == "application/pdf":
            if PLAYWRIGHT_AVAILABLE:
                # Giới hạn số Chromium chạy song song để không cạn RAM/CPU.
                with self.pdf_semaphore:
                    ok = self._handle_blocked_pdf_playwright(
                        api, src_file, dest_parent_id, name, file_id, path,
                        checkpoint_key or file_id,
                    )
                if not ok:
                    # Playwright failed — record source + intended dest path
                    self._record_blocked_pdf(
                        file_id,
                        source_path=f"{path}/{name}",
                        dest_folder_id=dest_parent_id,
                        name=name,
                    )
                return ok
            else:
                # Playwright not available — record immediately
                self._count("blocked")
                self._record_blocked_pdf(
                    file_id,
                    source_path=f"{path}/{name}",
                    dest_folder_id=dest_parent_id,
                    name=name,
                )
                return False

        # E. Không thể xử lý
        self._count("blocked")
        UI.status("BLOCKED", f"{name} — không phải video và không có export", indent=4)

        return False

    def _handle_blocked_pdf_playwright(
        self, api: "DriveAPI", src_file: dict, dest_parent_id: str,
        name: str, file_id: str, source_path: str = "", checkpoint_key: str | None = None,
    ) -> bool:
        """Download blocked PDF via Playwright viewer capture, then upload to Drive.

        Chromise capture thi thoang fail tam thoi (page.goto timeout, browser
        launch fail khi nhieu instance chay cung luc). Retry voi backoff de
        tranh dua file vao bad_paths chi vi mot loi tam thoi.

        Returns True on success. On failure the caller (_handle_blocked_file)
        is responsible for calling _record_blocked_pdf.
        """
        UI.status("PLAYWRIGHT", f"{name} — tải qua viewer...", indent=4)

        expected_size = int(src_file.get("size", 0))
        entry = BlockedPdfEntry(file_id=file_id, name=name, dest_folder_id=dest_parent_id)
        pdf_path = self._download_blocked_pdf_to_cache(entry, expected_size)
        if not pdf_path:
            return False

        upload_name = name if name.lower().endswith(".pdf") else name + ".pdf"
        try:
            dest_id = api.upload_local_file(
                str(pdf_path),
                name=upload_name,
                parent_id=dest_parent_id,
                mime_type="application/pdf",
            )
            self._count("fallback_uploaded")
            UI.status("PLAYWRIGHT", f"OK: {name} ({pdf_path.stat().st_size // 1024}KB)", indent=4)
            self._remember_dest_item(
                dest_parent_id,
                {"id": dest_id, "name": upload_name, "mimeType": "application/pdf"},
            )
            self._save_checkpoint(checkpoint_key or file_id, dest_id)
            return True
        except Exception as e:
            UI.error(f"Upload sau PDF fallback that bai {name}: {e}", indent=4)
            self._count("errors")
            return False

    def _handle_blocked_video(
        self, api: DriveAPI, streamer: StreamDownloader,
        dest_parent_id: str, name: str,
        file_id: str, mime_type: str, checkpoint_key: str | None = None,
    ) -> bool:
        # Áp dụng kiến trúc IDM: Ưu tiên tải đa luồng về đĩa tạm để tối đa tốc độ
        UI.status("FALLBACK", f"{name} — tải file tạm (IDM 8 luồng) rồi upload...", indent=4)
        suffix = Path(name).suffix or ".mp4"
        tmp_path = None
        for attempt in range(1, self.config.RETRY_TIMES + 1):
            tmp_path = streamer.download_to_temp(file_id, suffix=suffix, display_name=name)
            if tmp_path is not None:
                break
            if looks_like_cookie_auth_error(streamer.last_error or ""):
                if self._refresh_cookie_for_streamer(streamer, streamer.last_error or "cookie error"):
                    UI.status("COOKIE", f"Đã thay cookie, retry fallback: {name}", indent=4)
                    continue
            UI.warn(f"IDM download fail attempt={attempt} {name}", indent=4)
            if attempt < self.config.RETRY_TIMES:
                time.sleep(exponential_backoff(attempt))

        if tmp_path is None:
            self._count("errors")
            UI.error(f"Fallback fail {name} — không tải được stream bằng IDM", indent=4)
            return False
            
        try:
            dest_id = api.upload_local_file(
                str(tmp_path), name, dest_parent_id,
                mime_type if "video" in mime_type else "video/mp4",
            )
            self._count("fallback_uploaded")
            UI.status("FALLBACK", f"OK: {name}", indent=4)
            self._remember_dest_item(
                dest_parent_id, {"id": dest_id, "name": name, "mimeType": mime_type}
            )
            self._save_checkpoint(checkpoint_key or file_id, dest_id)
            return True
        except Exception as e:
            self._count("errors")
            UI.error(f"Upload fail {name}: {e}", indent=4)
            return False
        finally:
            if tmp_path:
                tmp_path.unlink(missing_ok=True)

    # ── Recursive traversal ───────────────────────────────────────────────────

    def resolve_shortcut_item(
        self, api: DriveAPI, item: dict
    ) -> tuple[dict | None, str | None]:
        if not is_shortcut(item):
            return item, None
        return None, skipped_shortcut_reason(item)

    def process_tree_item(
        self, api: DriveAPI, streamer: StreamDownloader,
        item: dict, dest_folder_id: str, path: str, depth: int,
    ) -> bool:
        if is_shortcut(item):
            self._count("skipped")
            UI.status("SKIP", skipped_shortcut_reason(item), indent=4)
            return True

        resolved, error = self.resolve_shortcut_item(api, item)
        if error or not resolved:
            self._count("errors")
            UI.error(error or "Khong resolve duoc shortcut", indent=4)
            return False
        name = resolved["name"]
        if resolved["mimeType"] == FOLDER_MIME_TYPE:
            folder_id = resolved.get("_list_folder_id", resolved["id"])
            state_folder_id = resolved.get("_checkpoint_key", folder_id)
            sub_dest = self.ensure_dest_folder(api, state_folder_id, dest_folder_id, name)
            if not sub_dest:
                self._count("errors")
                UI.error(f"Khong tao duoc folder dich: {name}", indent=4)
                return False
            return self.recursive_copy(
                folder_id, sub_dest, f"{path}/{name}", depth + 1,
                state_folder_id=state_folder_id,
            )
        return self.process_single_file(api, streamer, resolved, dest_folder_id, path)

    def recursive_copy(
        self, src_folder_id: str, dest_folder_id: str,
        path: str, depth: int = 0, state_folder_id: str | None = None,
    ) -> bool:
        if depth > self.config.MAX_DEPTH:
            self._count("errors")
            UI.warn(f"Vượt MAX_DEPTH tại: {path}", indent=2)
            self._mark_bad_path(path)
            return False

        state_folder_id = state_folder_id or src_folder_id
        if self.sync_state.can_skip_folder(state_folder_id):
            self._count("smart_skipped")
            UI.status("SMART", f"{path} không có thay đổi, bỏ qua cả nhánh.", indent=2)
            return True

        api      = self._drive_api()
        streamer = self._streamer()
        children = api.list_children(src_folder_id)
        self._count("folders_scanned")
        self.sync_state.record_scanned_folder(
            state_folder_id, dest_folder_id, path, children,
            real_folder_id=src_folder_id,
        )
        UI.status("SCAN", f"{path} | {len(children)} items | depth={depth}", indent=2)

        ok = True
        for item in children:
            if not self.process_tree_item(api, streamer, item, dest_folder_id, path, depth):
                self._mark_bad_path(f"{path}/{item.get('name', '')}")
                ok = False
        return ok

        ok = True
        for item in children:
            name = item["name"]
            if item["mimeType"] == FOLDER_MIME_TYPE:
                # Use the exact source name — never rename or sanitize.
                # Google Drive allows '/' in folder names on the source side;
                # the destination folder is created via the API with the same
                # name, which Drive also accepts on write.
                sub_dest = self.ensure_dest_folder(api, item["id"], dest_folder_id, name)
                if sub_dest:
                    sub_ok = self.recursive_copy(
                        item["id"], sub_dest, f"{path}/{name}", depth + 1
                    )
                    if not sub_ok:
                        ok = False
                else:
                    self._count("errors")
                    UI.error(f"Không tạo được folder đích: {name}", indent=4)
            else:
                if not self.process_single_file(api, streamer, item, dest_folder_id, path):
                    self._mark_bad_path(f"{path}/{name}")
                    ok = False
        return ok

    def run_worker(self, src_folder_id: str, dest_folder_id: str, label: str):
        api  = self._drive_api()
        name = label

        try:
            info = api.service.files().get(
                fileId=src_folder_id, fields="name", supportsAllDrives=True
            ).execute()
            name = info.get("name", src_folder_id)
            UI.status("WORKER", f"[{label}] {name}")

            sub_dest = self.ensure_dest_folder(api, src_folder_id, dest_folder_id, name)
            if not sub_dest:
                self._count("errors")
                UI.error(f"Không tạo được folder đích: {name}", indent=2)
                return

            self.recursive_copy(src_folder_id, sub_dest, name)
            UI.status("DONE", f"[{label}] {name}", indent=2)
        except Exception as e:
            self._count("errors")
            UI.error(f"[{label}] Lỗi: {e}", indent=2)
            traceback.print_exc()

    def _process_source_folder(self, main_api, source_idx: int, source_id: str):
        """Quet va xu ly mot source folder."""
        source_label = f"SOURCE_{source_idx}"
        if self.sync_state.should_skip_everything():
            UI.status("SMART", f"[{source_label}] Khong co thay doi, bo qua.")
            return

        top_children = main_api.list_children(source_id)
        self._count("folders_scanned")
        self.sync_state.record_scanned_folder(
            source_id,
            self.config.DEST_FOLDER_ID,
            source_label,
            top_children,
        )
        folders = [
            c for c in top_children
            if not is_shortcut(c) and c["mimeType"] == FOLDER_MIME_TYPE
        ]
        loose_files = [
            c for c in top_children
            if not is_shortcut(c) and c["mimeType"] != FOLDER_MIME_TYPE
        ]

        UI.section(f"[{source_label}] {len(folders)} folder + {len(loose_files)} file roi")

        # File rời ở root
        if loose_files:
            streamer = self._streamer()
            UI.status("SCAN", f"[{source_label}] {len(loose_files)} file roi...")
            for f in loose_files:
                if not self.process_tree_item(
                    main_api, streamer, f, self.config.DEST_FOLDER_ID, source_label, 0
                ):
                    self._mark_bad_path(f"{source_label}/{f['name']}")

        # Folder con song song
        if folders:
            UI.section(
                f"[{source_label}] Chay {self.config.MAX_WORKERS} worker"
                f" cho {len(folders)} folder"
            )
            with ThreadPoolExecutor(max_workers=self.config.MAX_WORKERS) as pool:
                futures = {
                    pool.submit(
                        self.run_worker, f["id"], self.config.DEST_FOLDER_ID,
                        f"{source_label}/{i + 1}"
                    ): f
                    for i, f in enumerate(folders)
                }
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as e:
                        self._count("errors")
                        UI.error(f"Worker lỗi: {e}")

    def start(self):
        started_at = time.monotonic()
        source_display = ", ".join(self.config.SOURCE_FOLDER_IDS)
        UI.banner(
            "BẮT ĐẦU ĐỒNG BỘ DRIVE",
            [
                ("Source", source_display),
                ("Dest", self.config.DEST_FOLDER_ID),
                ("Workers", str(self.config.MAX_WORKERS)),
                ("Stream workers", str(self.config.MAX_STREAM_WORKERS)),
                ("PDF workers", str(self.config.MAX_PDF_WORKERS)),
                ("Smart scan", "bật" if self.config.SMART_SCAN_ENABLED else "tắt"),
                ("Force full scan", "có" if self.config.FORCE_FULL_SCAN else "không"),
                ("Cookie file", self.config.COOKIE_FILE),
            ],
        )

        UI.start_dashboard()
        main_api = self._drive_api()
        self.sync_state.prepare(main_api.service)

        for idx, source_id in enumerate(self.config.SOURCE_FOLDER_IDS, start=1):
            UI.status("SOURCE", f"Dang xu ly source {idx}/{len(self.config.SOURCE_FOLDER_IDS)}: {source_id}")
            self._process_source_folder(main_api, idx, source_id)

        sync_results = [
            {"status": "ERROR", "path": path}
            for path in self.bad_paths
        ]
        self.sync_state.commit(sync_results, main_api.service)
        self._flush_checkpoint()
        self._flush_blocked_pdf_log()
        UI.stop_dashboard()
        elapsed = time.monotonic() - started_at
        stats = self._stats_snapshot()
        UI.summary(
            "Tổng kết",
            [
                ("Thời gian chạy", format_duration(elapsed)),
                ("File đã xử lý", stats.get("files_seen", 0)),
                ("Folder đã quét", stats.get("folders_scanned", 0)),
                ("Copy API", stats.get("copied", 0)),
                ("Stream upload", stats.get("streamed", 0)),
                ("Fallback upload", stats.get("fallback_uploaded", 0)),
                ("Export Workspace", stats.get("exported", 0)),
                ("Bỏ qua", stats.get("skipped", 0)),
                ("Bỏ qua từ checkpoint", stats.get("checkpoint", 0)),
                ("Smart skip folder", stats.get("smart_skipped", 0)),
                ("Blocked chưa xử lý", stats.get("blocked", 0)),
                ("PDF bị block (đã ghi log)", len(self.blocked_pdf_log)),
                ("Lỗi", stats.get("errors", 0)),
                ("Checkpoint", f"{len(self.checkpoint)} mục"),
            ],
        )
        if self.bad_paths:
            UI.section("Đường dẫn lỗi")
            for path in self.bad_paths[:10]:
                UI.error(path, indent=2)
            if len(self.bad_paths) > 10:
                UI.warn(f"... và {len(self.bad_paths) - 10} đường dẫn khác.", indent=2)
        if self.blocked_pdf_log:
            UI.section(f"PDF bị block — đã lưu vào {self.config.BLOCKED_PDF_LOG_FILE}")
            for entry in self.blocked_pdf_log[:10]:
                UI.warn(
                    f"  {entry['file_link']}  →  {entry['dest_folder_link']}",
                    indent=2,
                )
            if len(self.blocked_pdf_log) > 10:
                UI.warn(f"... và {len(self.blocked_pdf_log) - 10} file khác.", indent=2)
        UI.status("DONE", "HOÀN THÀNH")


# ─── CLI ──────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("source_folder", nargs="?", help="Source Google Drive folder ID/URL (nhieu link phan cach boi dau phay)")
    p.add_argument("dest_folder", nargs="?", help="Destination Google Drive folder ID or URL")
    p.add_argument("--source-folder-id",   dest="source_folder_id")
    p.add_argument("--dest-folder-id",     dest="dest_folder_id")
    p.add_argument("--max-workers",        dest="max_workers",       type=int)
    p.add_argument("--max-stream-workers", dest="max_stream_workers", type=int)
    p.add_argument("--max-depth",          dest="max_depth",         type=int)
    p.add_argument("--retry-times",        dest="retry_times",       type=int)
    p.add_argument("--token-file",         dest="token_file")
    p.add_argument("--checkpoint-file",    dest="checkpoint_file")
    p.add_argument("--sync-state-file",    dest="sync_state_file")
    p.add_argument("--force-full-scan",    dest="force_full_scan",
                   action="store_true", default=None)
    p.add_argument("--no-smart-scan",      dest="smart_scan_enabled",
                   action="store_false", default=None)
    p.add_argument(
        "--cookie-file",
        dest="cookie_file",
        help="Cookie file path: supports cookies.json, cookie.txt/Netscape, or raw Cookie header",
    )
    p.add_argument("--auto-cookie-refresh", dest="auto_cookie_refresh",
                   action="store_true", default=None)
    p.add_argument("--no-auto-cookie-refresh", dest="auto_cookie_refresh",
                   action="store_false")
    p.add_argument("--cookie-refresh-timeout", dest="cookie_refresh_timeout",
                   type=float)
    p.add_argument("--cookie-refresh-poll-seconds",
                   dest="cookie_refresh_poll_seconds", type=float)
    p.add_argument("--blocked-pdf-list", dest="blocked_pdf_list",
                   help="Markdown log/list of blocked PDFs to recover and upload")
    p.add_argument("--blocked-pdf-checkpoint-file",
                   dest="blocked_pdf_checkpoint_file")
    p.add_argument("--blocked-pdf-cache-dir", dest="blocked_pdf_cache_dir")
    p.add_argument("--no-input-prompt",    dest="no_input_prompt",
                   action="store_true", default=False)
    return p.parse_args()


def main() -> int:
    try:
        args = parse_args()
        config = ConfigManager()
        config.apply_args(args)
    except ValueError as e:
        UI.error(f"Cau hinh khong hop le: {e}")
        return 2

    auth = Authenticator(config)

    try:
        auth.authenticate_api()
        auth.authenticate_web()
    except Exception as e:
        UI.error(f"Khởi tạo thất bại: {e}")
        return 1

    engine = SyncEngine(config, auth)
    try:
        if config.BLOCKED_PDF_LIST:
            engine.replay_blocked_pdf_list(config.BLOCKED_PDF_LIST)
        else:
            engine.start()
    except KeyboardInterrupt:
        UI.stop_dashboard()
        UI.warn("Đã nhận Ctrl+C — đang lưu checkpoint trước khi thoát...")
        engine._flush_checkpoint()
        engine._flush_blocked_pdf_log()
        UI.status("DONE", "Đã lưu checkpoint. Chạy lại để tiếp tục từ điểm dừng.")
    finally:
        UI.stop_dashboard()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
