#!/usr/bin/env python3
"""
Download Google Drive files or direct stream URLs with cookie support.

Use this only for files you own or are authorized to access. Do not paste
cookies into chat or commit them to source control.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import html as html_module
import json
import mimetypes
import os
import queue
import re
import shutil
import sys
import threading
import time
import warnings
from dataclasses import dataclass
from email.message import Message
from html.parser import HTMLParser
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from datetime import datetime

import contextlib
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from requests.cookies import get_cookie_header

try:
    from rich.progress import (
        Progress,
        TextColumn,
        BarColumn,
        DownloadColumn,
        TransferSpeedColumn,
        TimeRemainingColumn,
    )
    from rich.console import Console
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False
    Progress = None

class DummyProgress:
    def add_task(self, *args, **kwargs): return 0
    def update(self, *args, **kwargs): pass
    def remove_task(self, *args, **kwargs): pass
    @property
    def tasks(self):
        class DummyTask:
            total = None
        return {0: DummyTask()}

GLOBAL_PROGRESS = None

def get_progress():
    global GLOBAL_PROGRESS
    if not RICH_AVAILABLE:
        return DummyProgress()
    if GLOBAL_PROGRESS is None:
        GLOBAL_PROGRESS = Progress(
            TextColumn("[bold blue]{task.fields[filename]}", justify="right"),
            BarColumn(bar_width=None),
            "[progress.percentage]{task.percentage:>3.1f}%",
            "•",
            DownloadColumn(),
            "•",
            TransferSpeedColumn(),
            "•",
            TimeRemainingColumn(),
            console=Console(file=sys.stderr),
        )
    return GLOBAL_PROGRESS

def print_msg(msg: str) -> None:
    if RICH_AVAILABLE and GLOBAL_PROGRESS:
        GLOBAL_PROGRESS.console.print(msg)
    else:
        print(msg, file=sys.stderr)

from idm_downloader import IDMDownloadError, IDMDownloader


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
DEFAULT_CHUNK_SIZE = 1024 * 1024
DRIVE_UC_URL = "https://drive.google.com/uc"
DRIVE_VIDEO_INFO_URL = "https://drive.google.com/get_video_info"
GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
GOOGLE_APPS_MIME_PREFIX = "application/vnd.google-apps."
MAX_EMBEDDED_FOLDER_ITEMS = 50
DEFAULT_SEGMENT_THRESHOLD = 8 * 1024 * 1024
VIDEO_ITAG_RANK = {
    "38": 9,
    "37": 8,
    "22": 7,
    "59": 6,
    "18": 5,
    "35": 4,
    "34": 3,
    "43": 2,
    "5": 1,
}
COOKIE_AUTH_ERROR_KEYWORDS = (
    "cookie",
    "http 401",
    "http 403",
    "unauthorized",
    "not authorized",
    "login",
    "sign in",
    "servicelogin",
    "accounts.google",
    "while reading video stream metadata",
    "check that the cookie has access",
)


@dataclass(frozen=True)
class ExportSpec:
    extension: str
    url_template: str


OFFICE_EXPORTS = {
    "application/vnd.google-apps.document": ExportSpec(
        "docx",
        "https://docs.google.com/document/d/{id}/export?format=docx",
    ),
    "application/vnd.google-apps.spreadsheet": ExportSpec(
        "xlsx",
        "https://docs.google.com/spreadsheets/d/{id}/export?format=xlsx",
    ),
    "application/vnd.google-apps.presentation": ExportSpec(
        "pptx",
        "https://docs.google.com/presentation/d/{id}/export/pptx",
    ),
    "application/vnd.google-apps.drawing": ExportSpec(
        "png",
        "https://docs.google.com/drawings/d/{id}/export/png",
    ),
    "application/vnd.google-apps.script": ExportSpec(
        "json",
        "https://script.google.com/feeds/download/export?id={id}&format=json",
    ),
}


API_EXPORT_MIME_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "pdf": "application/pdf",
    "png": "image/png",
    "json": "application/json"
}

PDF_EXPORTS = {
    "application/vnd.google-apps.document": ExportSpec(
        "pdf",
        "https://docs.google.com/document/d/{id}/export?format=pdf",
    ),
    "application/vnd.google-apps.spreadsheet": ExportSpec(
        "pdf",
        "https://docs.google.com/spreadsheets/d/{id}/export?format=pdf",
    ),
    "application/vnd.google-apps.presentation": ExportSpec(
        "pdf",
        "https://docs.google.com/presentation/d/{id}/export/pdf",
    ),
    "application/vnd.google-apps.drawing": ExportSpec(
        "pdf",
        "https://docs.google.com/drawings/d/{id}/export/pdf",
    ),
}



class TokenAuth(requests.auth.AuthBase):
    def __init__(self, token_file: str):
        self.token_file = Path(token_file)
        self.token_data = self._load_data()

    def _load_data(self):
        with self.token_file.open("r", encoding="utf-8") as f:
            return json.load(f)

    def _save_data(self):
        with self.token_file.open("w", encoding="utf-8") as f:
            json.dump(self.token_data, f, indent=4)

    def _is_expired(self):
        expiry_str = self.token_data.get("expiry")
        if not expiry_str:
            return False
        try:
            expiry_str = expiry_str.replace("Z", "+00:00")
            expiry = datetime.fromisoformat(expiry_str)
            return expiry.timestamp() - time.time() < 60
        except Exception:
            return True

    def _refresh(self):
        if "refresh_token" not in self.token_data:
            return
        response = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": self.token_data["client_id"],
                "client_secret": self.token_data["client_secret"],
                "refresh_token": self.token_data["refresh_token"],
                "grant_type": "refresh_token",
            }
        )
        if response.status_code == 200:
            new_data = response.json()
            self.token_data["token"] = new_data["access_token"]
            self._save_data()
        else:
            raise DownloadError(f"Token refresh failed: {response.text}")

    def get_token(self):
        if self._is_expired():
            self._refresh()
        return self.token_data.get("token")

    def __call__(self, r):
        token = self.get_token()
        if token:
            r.headers["Authorization"] = f"Bearer {token}"
        return r

class DownloadError(RuntimeError):
    pass


class CookieRefreshState:
    def __init__(self, cookie_file: Optional[str]):
        self.lock = threading.Lock()
        self.version = 0
        self.fingerprint = cookie_file_fingerprint(cookie_file)


@dataclass
class ParsedForm:
    action: str
    method: str
    inputs: dict[str, str]


@dataclass
class DriveItem:
    id: str
    name: str
    mime_type: str
    children: list["DriveItem"]

    @property
    def is_folder(self) -> bool:
        return self.mime_type == GOOGLE_FOLDER_MIME


@dataclass
class VideoStream:
    itag: str
    url: str
    quality: str = ""


class DriveHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[ParsedForm] = []
        self.links: list[str] = []
        self.title_parts: list[str] = []
        self._form: Optional[dict[str, object]] = None
        self._in_title = False

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
            return

        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "form" and self._form is not None:
            inputs = self._form["inputs"]
            assert isinstance(inputs, dict)
            self.forms.append(
                ParsedForm(
                    action=str(self._form["action"]),
                    method=str(self._form["method"]),
                    inputs={str(k): str(v) for k, v in inputs.items()},
                )
            )
            self._form = None
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data.strip())

    @property
    def title(self) -> str:
        return " ".join(part for part in self.title_parts if part)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download Google Drive videos/files or direct stream URLs with cookies."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--url", nargs="+", help="Google Drive share URL or direct stream URL.")
    source.add_argument("--file-id", nargs="+", help="Google Drive file id.")
    source.add_argument("--folder-url", nargs="+", help="Google Drive folder URL.")
    source.add_argument("--folder-id", nargs="+", help="Google Drive folder id.")

    parser.add_argument(
        "-o",
        "--output",
        help=(
            "Output file path for a single file, or output directory for a folder. "
            "Folder downloads are saved under this directory using the remote folder name."
        ),
    )
    parser.add_argument(
        "--token-file",
        help=(
            "Path to a token.json file containing Google OAuth 2.0 credentials. "
            "If provided, the script uses the official Google Drive API."
        ),
    )
    parser.add_argument(
        "--cookie-file",
        help=(
            "Cookie file path. Supports Netscape cookies.txt or a plain Cookie header "
            "stored in a text file."
        ),
    )
    parser.add_argument(
        "--cookie-env",
        default="GDRIVE_COOKIE",
        help="Environment variable containing a Cookie header. Default: GDRIVE_COOKIE.",
    )
    parser.add_argument(
        "--cookie-header",
        help="Cookie header string. Prefer --cookie-env to avoid shell history leaks.",
    )
    parser.add_argument(
        "--cookie-domain",
        default=".google.com",
        help="Domain used when parsing Cookie header into a cookie jar. Default: .google.com.",
    )
    parser.add_argument(
        "--force-cookie-header",
        action="store_true",
        help=(
            "Send the raw Cookie header on every request. This can expose cookies to "
            "redirected hosts, so use only when you understand the risk."
        ),
    )
    parser.add_argument(
        "--auto-cookie-refresh",
        dest="auto_cookie_refresh",
        action="store_true",
        default=True,
        help=(
            "When cookie auth looks expired, wait for a replacement cookie file, "
            "reload it, and retry the failed request. Enabled by default."
        ),
    )
    parser.add_argument(
        "--no-auto-cookie-refresh",
        dest="auto_cookie_refresh",
        action="store_false",
        help="Disable waiting/reloading when the cookie appears expired.",
    )
    parser.add_argument(
        "--cookie-refresh-timeout",
        type=float,
        default=0.0,
        help=(
            "Seconds to wait for --cookie-file to change when stdin is not interactive. "
            "Default: 0 (fail fast outside a console)."
        ),
    )
    parser.add_argument(
        "--cookie-refresh-poll-seconds",
        type=float,
        default=2.0,
        help="Polling interval while waiting for a replacement cookie file. Default: 2.",
    )
    parser.add_argument(
        "--referer",
        default="https://drive.google.com/",
        help="Referer header. Default: https://drive.google.com/.",
    )
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="User-Agent header.")
    parser.add_argument(
        "--chunk-size",
        type=parse_size,
        default=DEFAULT_CHUNK_SIZE,
        help="Download chunk size, e.g. 256K, 1M, 8M. Default: 1M.",
    )
    parser.add_argument(
        "--segments",
        type=int,
        default=1,
        help=(
            "Number of parallel ranged connections for one large file. "
            "Use 4-8 when Google Drive throttles a single stream. Default: 1."
        ),
    )
    parser.add_argument(
        "--segment-threshold",
        type=parse_size,
        default=DEFAULT_SEGMENT_THRESHOLD,
        help="Minimum remaining file size before --segments is used. Default: 8M.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume if the output file already exists and the server supports Range.",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="For folder downloads, print the folder tree without downloading files.",
    )
    parser.add_argument(
        "--workspace-format",
        choices=("office", "pdf"),
        default="office",
        help=(
            "Export format for Google Docs/Sheets/Slides/Drawings inside folders. "
            "Default: office."
        ),
    )
    parser.add_argument(
        "--allow-partial-folder",
        action="store_true",
        help=(
            "Continue when a folder page exposes the Google Drive embedded listing "
            "limit. Without this, the tool stops to avoid silently missing files."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Per-request timeout in seconds. Default: 30.",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS verification. Avoid this unless you are debugging a local proxy.",
    )
    args = parser.parse_args()
    if args.segments < 1:
        parser.error("--segments must be at least 1")
    return args


def parse_size(value: str) -> int:
    match = re.fullmatch(r"(\d+)([KkMmGg]?)", value.strip())
    if not match:
        raise argparse.ArgumentTypeError("size must look like 512K, 1M, or 8M")

    amount = int(match.group(1))
    suffix = match.group(2).lower()
    multiplier = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}[suffix]
    size = amount * multiplier
    if size <= 0:
        raise argparse.ArgumentTypeError("size must be greater than zero")
    return size


def looks_like_cookie_auth_error(error: Exception | str) -> bool:
    text = str(error).lower()
    return any(keyword in text for keyword in COOKIE_AUTH_ERROR_KEYWORDS)


def cookie_file_fingerprint(cookie_file: Optional[str]) -> Optional[str]:
    if not cookie_file:
        return None
    try:
        path = Path(cookie_file).expanduser()
        data = path.read_bytes()
        return f"{path.resolve(strict=False)}:{len(data)}:{hashlib.sha256(data).hexdigest()}"
    except OSError:
        return None


def cookie_refresh_state(args: argparse.Namespace) -> CookieRefreshState:
    state = getattr(args, "_cookie_refresh_state", None)
    if state is None:
        state = CookieRefreshState(getattr(args, "cookie_file", None))
        setattr(args, "_cookie_refresh_state", state)
    return state


def apply_cookie_sources(session: requests.Session, args: argparse.Namespace) -> None:
    session.cookies.clear()
    session.headers.pop("Cookie", None)
    cookie_header = args.cookie_header or os.environ.get(args.cookie_env)
    if args.cookie_file:
        load_cookie_file(session, Path(args.cookie_file), args.cookie_domain)
    if cookie_header:
        apply_cookie_header(session, cookie_header, args.cookie_domain)
        if args.force_cookie_header:
            session.headers["Cookie"] = cookie_header


def refresh_cookies_after_auth_error(
    args: argparse.Namespace,
    session: requests.Session,
    reason: Exception | str,
) -> bool:
    if getattr(args, "token_file", None) or not getattr(args, "auto_cookie_refresh", True):
        return False
    if not getattr(args, "cookie_file", None):
        return False
    if not looks_like_cookie_auth_error(reason):
        return False

    state = cookie_refresh_state(args)
    observed_version = getattr(session, "_gdrive_cookie_version", state.version)
    with state.lock:
        if observed_version != state.version:
            try:
                apply_cookie_sources(session, args)
                session._gdrive_cookie_version = state.version
                return True
            except DownloadError as exc:
                print_msg(f"Could not reload updated cookie file: {exc}")
                return False

        old_fingerprint = state.fingerprint or cookie_file_fingerprint(args.cookie_file)
        print_msg("")
        print_msg("Google Drive cookie looks expired or unauthorized.")
        print_msg(f"Reason: {reason}")
        print_msg(f"Current cookie file: {Path(args.cookie_file).resolve(strict=False)}")

        if stdin_is_interactive():
            refreshed = wait_for_cookie_interactive(args, state, old_fingerprint)
        else:
            refreshed = wait_for_cookie_file_change(args, state, old_fingerprint)
        if not refreshed:
            return False

        try:
            apply_cookie_sources(session, args)
            session._gdrive_cookie_version = state.version
            print_msg("Reloaded replacement cookie; retrying request.")
            return True
        except DownloadError as exc:
            print_msg(f"Replacement cookie could not be loaded: {exc}")
            return False


def wait_for_cookie_interactive(
    args: argparse.Namespace,
    state: CookieRefreshState,
    old_fingerprint: Optional[str],
) -> bool:
    while True:
        answer = input(
            "Overwrite the cookie file and press Enter, or enter a new cookie path "
            "(q to skip): "
        ).strip().strip('"')
        if answer.lower() in {"q", "quit", "skip"}:
            return False
        if answer:
            args.cookie_file = answer

        path = Path(args.cookie_file).expanduser()
        if not path.exists():
            print_msg(f"Cookie file not found: {path}")
            continue

        new_fingerprint = cookie_file_fingerprint(str(path))
        if old_fingerprint and new_fingerprint == old_fingerprint and not answer:
            print_msg("Cookie file has not changed; reloading it anyway.")
        state.fingerprint = new_fingerprint
        state.version += 1
        return True


def wait_for_cookie_file_change(
    args: argparse.Namespace,
    state: CookieRefreshState,
    old_fingerprint: Optional[str],
) -> bool:
    timeout = float(getattr(args, "cookie_refresh_timeout", 0.0) or 0.0)
    if timeout <= 0:
        print_msg(
            "No interactive stdin is available. Set --cookie-refresh-timeout to "
            "wait for the cookie file to be replaced."
        )
        return False

    deadline = time.monotonic() + timeout
    poll = max(float(getattr(args, "cookie_refresh_poll_seconds", 2.0) or 2.0), 0.5)
    print_msg(f"Waiting up to {timeout:.0f}s for the cookie file to change...")
    while time.monotonic() < deadline:
        new_fingerprint = cookie_file_fingerprint(args.cookie_file)
        if new_fingerprint and new_fingerprint != old_fingerprint:
            state.fingerprint = new_fingerprint
            state.version += 1
            return True
        time.sleep(poll)

    print_msg("Timed out waiting for a replacement cookie file.")
    return False


def stdin_is_interactive() -> bool:
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except Exception:
        return False


def build_session(args: argparse.Namespace) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": args.user_agent,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": args.referer,
        }
    )

    if getattr(args, "token_file", None):
        session.auth = TokenAuth(args.token_file)
        return session

    apply_cookie_sources(session, args)
    session._gdrive_cookie_version = cookie_refresh_state(args).version

    return session


def load_cookie_file(session: requests.Session, path: Path, cookie_domain: str) -> None:
    if not path.exists():
        raise DownloadError(f"Cookie file not found: {path}")

    text = path.read_text(encoding="utf-8-sig", errors="ignore").strip()
    if not text:
        return

    if text.startswith("# Netscape") or "\t" in text:
        jar = MozillaCookieJar(str(path))
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
            session.cookies.update(jar)
        except Exception:
            load_netscape_cookie_lines(session, text)
        return

    apply_cookie_header(session, text, cookie_domain)


def load_netscape_cookie_lines(session: requests.Session, text: str) -> None:
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


def apply_cookie_header(session: requests.Session, header: str, domain: str) -> None:
    for part in header.replace("\n", ";").split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if not name:
            continue
        session.cookies.set(name, value.strip(), domain=domain)


def extract_file_id(value: str) -> Optional[str]:
    value = value.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", value):
        return value

    parsed = urlparse(value)
    query = parse_qs(parsed.query)
    for key in ("id", "file_id"):
        if query.get(key):
            return query[key][0]

    patterns = [
        r"/file/d/([^/?#]+)",
        r"/uc/([^/?#]+)",
        r"/d/([^/?#]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, parsed.path)
        if match:
            return unquote(match.group(1))

    return None


def extract_folder_id(value: str) -> Optional[str]:
    value = value.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", value):
        return value

    parsed = urlparse(value)
    query = parse_qs(parsed.query)
    for key in ("id", "folder_id"):
        if query.get(key):
            return query[key][0]

    match = re.search(r"/folders/([^/?#]+)", parsed.path)
    if match:
        return unquote(match.group(1))

    return None


def is_probably_folder_url(url: str) -> bool:
    parsed = urlparse(url)
    return "/folders/" in parsed.path and is_probably_drive_url(url)


def is_probably_drive_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host.endswith("drive.google.com") or host.endswith("drive.usercontent.google.com")


def open_response(args: argparse.Namespace, session: requests.Session) -> requests.Response:
    headers = build_range_headers(args)
    if args.file_id:
        if getattr(args, "token_file", None):
            return open_api_file_response(args, session, args.file_id, headers, getattr(args, "mime_type", None))
        return open_drive_response(
            args,
            session,
            args.file_id,
            headers,
            mime_type=getattr(args, "mime_type", None),
        )

    assert args.url
    file_id = extract_file_id(args.url)
    if file_id and is_probably_drive_url(args.url):
        if getattr(args, "token_file", None):
            return open_api_file_response(args, session, file_id, headers)
        return open_drive_response(args, session, file_id, headers)

    response = session.get(
        args.url,
        headers=headers,
        stream=True,
        allow_redirects=True,
        timeout=args.timeout,
        verify=not args.insecure,
    )
    return validate_file_response(response, session, args, headers)


def open_response_with_cookie_refresh(
    args: argparse.Namespace,
    session: requests.Session,
) -> requests.Response:
    for attempt in range(2):
        try:
            return open_response(args, session)
        except DownloadError as exc:
            if attempt == 0 and looks_like_cookie_auth_error(exc):
                if refresh_cookies_after_auth_error(args, session, exc):
                    continue
            raise
    raise DownloadError("Could not open response after cookie refresh")


def build_range_headers(args: argparse.Namespace) -> dict[str, str]:
    if not args.resume or not args.output:
        return {}

    output = Path(args.output)
    if output.exists() and output.stat().st_size > 0:
        return {"Range": f"bytes={output.stat().st_size}-"}
    return {}



def get_api_file_metadata(session: requests.Session, file_id: str, timeout: float) -> dict:
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}?fields=id,name,mimeType"
    response = session.get(url, timeout=timeout)
    if response.status_code >= 400:
        raise DownloadError(f"Failed to fetch file metadata via API: {response.text}")
    return response.json()

def open_api_file_response(
    args: argparse.Namespace,
    session: requests.Session,
    file_id: str,
    headers: dict[str, str],
    mime_type: Optional[str] = None,
) -> requests.Response:
    if mime_type is None:
        meta = get_api_file_metadata(session, file_id, args.timeout)
        mime_type = meta.get("mimeType")
        if not getattr(args, "output", None):
            args._api_filename = meta.get("name")

    if mime_type and is_google_workspace_mime(mime_type):
        spec = workspace_export_spec(mime_type, args.workspace_format)
        if spec is None:
            raise DownloadError(f"Unsupported Google Workspace file type: {mime_type}")
        export_mime = API_EXPORT_MIME_TYPES.get(spec.extension, "application/pdf")
        url = f"https://www.googleapis.com/drive/v3/files/{file_id}/export?mimeType={export_mime}"
        response = session.get(url, headers=headers, stream=True, allow_redirects=True, timeout=args.timeout, verify=not args.insecure)
        if response.status_code >= 400:
            raise DownloadError(f"API Export HTTP {response.status_code}: {response.text}")
        return response

    url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
    response = session.get(url, headers=headers, stream=True, allow_redirects=True, timeout=args.timeout, verify=not args.insecure)
    if response.status_code >= 400:
        raise DownloadError(f"API Download HTTP {response.status_code}: {response.text}")
    return response

def fetch_api_drive_folder(
    args: argparse.Namespace,
    session: requests.Session,
    folder_id: str,
    seen: Optional[set[str]] = None,
) -> DriveItem:
    if seen is None:
        seen = set()
    if folder_id in seen:
        raise DownloadError(f"Detected recursive folder reference: {folder_id}")
    seen.add(folder_id)

    meta = get_api_file_metadata(session, folder_id, args.timeout)
    folder = DriveItem(
        id=meta.get("id", folder_id),
        name=sanitize_filename(meta.get("name", "Google Drive folder")),
        mime_type=meta.get("mimeType", GOOGLE_FOLDER_MIME),
        children=[]
    )

    print_msg(f"Reading folder via API: {folder.name}")
    page_token = None
    while True:
        query = f"'{folder_id}' in parents and trashed = false"
        params = {"q": query, "fields": "nextPageToken,files(id,name,mimeType)", "pageSize": 1000}
        if page_token:
            params["pageToken"] = page_token
            
        list_resp = session.get("https://www.googleapis.com/drive/v3/files", params=params, timeout=args.timeout)
        if list_resp.status_code >= 400:
            raise DownloadError(f"Failed to list folder contents via API: {list_resp.text}")
            
        data = list_resp.json()
        for item in data.get("files", []):
            if item.get("mimeType") == GOOGLE_SHORTCUT_MIME:
                continue
            if item.get("mimeType") == GOOGLE_FOLDER_MIME:
                child = fetch_api_drive_folder(args, session, item["id"], seen=seen.copy())
                folder.children.append(child)
            else:
                folder.children.append(
                    DriveItem(id=item["id"], name=item.get("name", ""), mime_type=item.get("mimeType", ""), children=[])
                )
                
        page_token = data.get("nextPageToken")
        if not page_token:
            break
            
    return folder

def open_drive_response(
    args: argparse.Namespace,
    session: requests.Session,
    file_id: str,
    headers: dict[str, str],
    mime_type: Optional[str] = None,
) -> requests.Response:
    if mime_type and is_google_workspace_mime(mime_type):
        return open_workspace_export_response(args, session, file_id, headers, mime_type)

    params = {"export": "download", "id": file_id}
    response = session.get(
        DRIVE_UC_URL,
        params=params,
        headers=headers,
        stream=True,
        allow_redirects=True,
        timeout=args.timeout,
        verify=not args.insecure,
    )
    try:
        return validate_file_response(response, session, args, headers, file_id=file_id)
    except DownloadError as exc:
        if should_try_video_stream_fallback(str(exc), mime_type, getattr(args, "output", None)):
            print(
                "Direct download was blocked by Google Drive; trying video stream fallback",
                file=sys.stderr,
            )
            try:
                return open_drive_video_stream_response(args, session, file_id, headers)
            except DownloadError as stream_exc:
                raise DownloadError(
                    f"{exc}. Video stream fallback also failed: {stream_exc}"
                ) from stream_exc
        raise


def open_workspace_export_response(
    args: argparse.Namespace,
    session: requests.Session,
    file_id: str,
    headers: dict[str, str],
    mime_type: str,
) -> requests.Response:
    spec = workspace_export_spec(mime_type, args.workspace_format)
    if spec is None:
        raise DownloadError(f"Unsupported Google Workspace file type: {mime_type}")

    response = session.get(
        spec.url_template.format(id=file_id),
        headers=headers,
        stream=True,
        allow_redirects=True,
        timeout=args.timeout,
        verify=not args.insecure,
    )
    return validate_file_response(response, session, args, headers, file_id=file_id)


def open_drive_video_stream_response(
    args: argparse.Namespace,
    session: requests.Session,
    file_id: str,
    headers: dict[str, str],
) -> requests.Response:
    info_response = session.get(
        DRIVE_VIDEO_INFO_URL,
        params={"docid": file_id, "authuser": "0"},
        timeout=args.timeout,
        verify=not args.insecure,
    )
    if info_response.status_code >= 400:
        raise DownloadError(
            f"HTTP {info_response.status_code} while reading video stream metadata"
        )

    info = parse_qs(info_response.text, keep_blank_values=True)
    status = first_value(info, "status")
    if status and status.lower() != "ok":
        reason = first_value(info, "reason") or "Google Drive did not return video streams"
        raise DownloadError(f"Video stream fallback failed: {reason}")

    streams = parse_video_streams(info)
    if not streams:
        reason = first_value(info, "reason") or "No video stream URL found"
        raise DownloadError(f"Video stream fallback failed: {reason}")

    stream = choose_video_stream(streams)
    stream_headers = dict(headers)
    stream_headers.setdefault("Referer", f"https://drive.google.com/file/d/{file_id}/preview")
    stream_headers.setdefault("Accept", "*/*")

    response = session.get(
        stream.url,
        headers=stream_headers,
        stream=True,
        allow_redirects=True,
        timeout=args.timeout,
        verify=not args.insecure,
    )
    if response.status_code == 416:
        return response
    if response.status_code >= 400:
        raise DownloadError(
            f"Video stream fallback returned HTTP {response.status_code}"
        )
    if is_html_response(response):
        html = response.text
        response.close()
        raise make_html_error(html, response.url)

    print(
        f"Using video stream itag={stream.itag or 'unknown'}"
        + (f" quality={stream.quality}" if stream.quality else ""),
        file=sys.stderr,
    )
    return response


def should_try_video_stream_fallback(
    error: str,
    mime_type: Optional[str],
    output: Optional[str],
) -> bool:
    is_video = bool(mime_type and mime_type.lower().startswith("video/"))
    has_video_extension = bool(
        output and Path(output).suffix.lower() in {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
    )
    unknown_type = mime_type is None
    looks_like_drive_block = "can't download file" in error.lower()
    return looks_like_drive_block and (is_video or has_video_extension or unknown_type)


def parse_video_streams(info: dict[str, list[str]]) -> list[VideoStream]:
    streams: list[VideoStream] = []

    for stream_map_name in ("fmt_stream_map", "url_encoded_fmt_stream_map"):
        stream_map = first_value(info, stream_map_name)
        if not stream_map:
            continue
        streams.extend(parse_stream_map(stream_map, stream_map_name))

    unique: dict[str, VideoStream] = {}
    for stream in streams:
        if stream.url and stream.url not in unique:
            unique[stream.url] = stream
    return list(unique.values())


def parse_stream_map(stream_map: str, stream_map_name: str) -> list[VideoStream]:
    streams: list[VideoStream] = []
    for entry in split_stream_map(stream_map):
        if not entry:
            continue

        if stream_map_name == "fmt_stream_map" and "|" in entry:
            itag, url = entry.split("|", 1)
            streams.append(VideoStream(itag=itag.strip(), url=clean_stream_url(url)))
            continue

        data = parse_qs(entry, keep_blank_values=True)
        url = first_value(data, "url")
        if not url:
            continue
        streams.append(
            VideoStream(
                itag=first_value(data, "itag"),
                url=clean_stream_url(url),
                quality=first_value(data, "quality"),
            )
        )

    return streams


def split_stream_map(stream_map: str) -> list[str]:
    entries: list[str] = []
    current: list[str] = []
    for char in stream_map:
        if char == "," and current and current[-1] != "\\":
            entries.append("".join(current))
            current = []
            continue
        current.append(char)
    if current:
        entries.append("".join(current))
    return entries


def choose_video_stream(streams: list[VideoStream]) -> VideoStream:
    return max(
        streams,
        key=lambda stream: (
            VIDEO_ITAG_RANK.get(stream.itag, 0),
            quality_rank(stream.quality),
            stream.itag,
        ),
    )


def quality_rank(quality: str) -> int:
    ranks = {
        "highres": 9,
        "hd2160": 8,
        "hd1440": 7,
        "hd1080": 6,
        "hd720": 5,
        "large": 4,
        "medium": 3,
        "small": 2,
        "tiny": 1,
    }
    return ranks.get(quality.lower(), 0)


def first_value(values: dict[str, list[str]], key: str) -> str:
    items = values.get(key)
    if not items:
        return ""
    return items[0]


def clean_stream_url(url: str) -> str:
    return (
        html_module.unescape(url.strip())
        .replace("\\u0026", "&")
        .replace("\\/", "/")
    )


def validate_file_response(
    response: requests.Response,
    session: requests.Session,
    args: argparse.Namespace,
    headers: dict[str, str],
    file_id: Optional[str] = None,
) -> requests.Response:
    if response.status_code == 416:
        return response
    if response.status_code >= 400:
        raise DownloadError(f"HTTP {response.status_code} from {response.url}")

    if not is_html_response(response):
        return response

    html = response.text
    response.close()

    next_response = follow_drive_confirmation(html, response.url, session, args, headers, file_id)
    if next_response is not None:
        if next_response.status_code >= 400:
            raise DownloadError(f"HTTP {next_response.status_code} from {next_response.url}")
        if not is_html_response(next_response):
            return next_response
        html = next_response.text
        next_response.close()

    raise make_html_error(html, response.url)


def is_html_response(response: requests.Response) -> bool:
    content_type = response.headers.get("Content-Type", "").lower()
    content_disposition = response.headers.get("Content-Disposition", "").lower()
    return "text/html" in content_type and "filename=" not in content_disposition


def follow_drive_confirmation(
    html: str,
    base_url: str,
    session: requests.Session,
    args: argparse.Namespace,
    headers: dict[str, str],
    file_id: Optional[str],
) -> Optional[requests.Response]:
    parser = DriveHtmlParser()
    parser.feed(html)

    form = choose_download_form(parser.forms)
    if form is not None:
        url = urljoin(base_url, form.action or base_url)
        if form.method == "post":
            return session.post(
                url,
                data=form.inputs,
                headers=headers,
                stream=True,
                allow_redirects=True,
                timeout=args.timeout,
                verify=not args.insecure,
            )

        return session.get(
            url,
            params=form.inputs,
            headers=headers,
            stream=True,
            allow_redirects=True,
            timeout=args.timeout,
            verify=not args.insecure,
        )

    confirm_link = choose_confirm_link(parser.links)
    if confirm_link:
        return session.get(
            urljoin(base_url, confirm_link),
            headers=headers,
            stream=True,
            allow_redirects=True,
            timeout=args.timeout,
            verify=not args.insecure,
        )

    if file_id:
        token = find_download_warning_token(session)
        if token:
            return session.get(
                DRIVE_UC_URL,
                params={"export": "download", "id": file_id, "confirm": token},
                headers=headers,
                stream=True,
                allow_redirects=True,
                timeout=args.timeout,
                verify=not args.insecure,
            )

    return None


def choose_download_form(forms: list[ParsedForm]) -> Optional[ParsedForm]:
    for form in forms:
        action = form.action.lower()
        input_keys = set(form.inputs)
        looks_like_download = (
            "download" in action
            or "/uc" in action
            or {"id", "confirm"}.issubset(input_keys)
            or {"uuid", "confirm"}.issubset(input_keys)
        )
        if looks_like_download:
            return form
    return None


def choose_confirm_link(links: list[str]) -> Optional[str]:
    for link in links:
        lower = link.lower()
        if "confirm=" in lower and ("download" in lower or "/uc?" in lower):
            return link
    return None


def find_download_warning_token(session: requests.Session) -> Optional[str]:
    for cookie in session.cookies:
        if cookie.name.startswith("download_warning"):
            return cookie.value
    return None


def make_html_error(html: str, url: str) -> DownloadError:
    parser = DriveHtmlParser()
    parser.feed(html[:500_000])
    title = parser.title or "Google Drive returned an HTML page instead of a file"
    compact = re.sub(r"\s+", " ", html)
    compact = re.sub(r"<[^>]+>", " ", compact)
    compact = re.sub(r"\s+", " ", compact).strip()
    snippet = compact[:400]
    return DownloadError(f"{title}. URL: {url}. Snippet: {snippet}")


def fetch_drive_folder(
    args: argparse.Namespace,
    session: requests.Session,
    folder_id: str,
    seen: Optional[set[str]] = None,
) -> DriveItem:
    if seen is None:
        seen = set()
    if folder_id in seen:
        raise DownloadError(f"Detected recursive folder reference: {folder_id}")
    seen.add(folder_id)

    url = f"https://drive.google.com/drive/folders/{folder_id}?hl=en"
    print_msg(f"Reading folder metadata: {folder_id}")
    response = session.get(
        url,
        stream=False,
        allow_redirects=True,
        timeout=args.timeout,
        verify=not args.insecure,
    )
    if response.status_code >= 400:
        error = DownloadError(f"HTTP {response.status_code} while reading folder {folder_id}")
        if refresh_cookies_after_auth_error(args, session, error):
            response = session.get(
                url,
                stream=False,
                allow_redirects=True,
                timeout=args.timeout,
                verify=not args.insecure,
            )
        if response.status_code >= 400:
            raise DownloadError(f"HTTP {response.status_code} while reading folder {folder_id}")

    folder = parse_drive_folder_page(response.url, response.text)
    folder.children = []

    try:
        children = parse_drive_folder_children(response.text)
    except DownloadError as exc:
        if not refresh_cookies_after_auth_error(args, session, exc):
            raise
        response = session.get(
            url,
            stream=False,
            allow_redirects=True,
            timeout=args.timeout,
            verify=not args.insecure,
        )
        if response.status_code >= 400:
            raise DownloadError(f"HTTP {response.status_code} while reading folder {folder_id}")
        folder = parse_drive_folder_page(response.url, response.text)
        folder.children = []
        children = parse_drive_folder_children(response.text)

    for child_id, child_name, child_mime in children:
        if child_mime == GOOGLE_SHORTCUT_MIME:
            continue
        if child_mime == GOOGLE_FOLDER_MIME:
            child = fetch_drive_folder(args, session, child_id, seen=seen.copy())
            child.name = child_name
            folder.children.append(child)
        else:
            folder.children.append(
                DriveItem(id=child_id, name=child_name, mime_type=child_mime, children=[])
            )

    if (
        len(folder.children) >= MAX_EMBEDDED_FOLDER_ITEMS
        and not args.allow_partial_folder
    ):
        raise DownloadError(
            "Google Drive embedded folder listing exposed "
            f"{MAX_EMBEDDED_FOLDER_ITEMS} items for folder '{folder.name}'. "
            "This may mean the folder has more items than the page provides. "
            "Re-run with --allow-partial-folder only if you accept that risk."
        )

    return folder


def parse_drive_folder_page(url: str, content: str) -> DriveItem:
    parser = DriveHtmlParser()
    parser.feed(content[:500_000])
    name = folder_name_from_title(parser.title) or extract_folder_id(url) or "Google Drive folder"
    folder_id = extract_folder_id(url)
    if not folder_id:
        raise DownloadError(f"Could not extract folder id from URL: {url}")

    return DriveItem(
        id=folder_id,
        name=sanitize_filename(name),
        mime_type=GOOGLE_FOLDER_MIME,
        children=[],
    )


def parse_drive_folder_children(content: str) -> list[tuple[str, str, str]]:
    encoded_data = extract_drive_ivd(content)
    if encoded_data is None:
        raise DownloadError(
            "Could not read Google Drive folder data from the page. "
            "Check that the cookie has access to this folder, or that the folder is shared "
            "with anyone who has the link."
        )

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        decoded = encoded_data.encode("utf-8").decode("unicode_escape")

    try:
        folder_arr = json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise DownloadError(f"Could not decode Google Drive folder data: {exc}") from exc

    folder_contents = [] if not folder_arr or folder_arr[0] is None else folder_arr[0]
    children: list[tuple[str, str, str]] = []
    for entry in folder_contents:
        if not isinstance(entry, list) or len(entry) < 4:
            continue
        child_id = str(entry[0])
        child_name = decode_drive_name(str(entry[2]))
        child_mime = str(entry[3])
        if child_mime == GOOGLE_SHORTCUT_MIME:
            continue
        if child_id and child_name and child_mime:
            children.append((child_id, child_name, child_mime))

    return children


def extract_drive_ivd(content: str) -> Optional[str]:
    script_pattern = re.compile(r"<script\b[^>]*>(.*?)</script>", re.I | re.S)
    string_pattern = re.compile(r"'((?:[^'\\]|\\.)*)'")

    for script_match in script_pattern.finditer(content):
        inner_html = script_match.group(1)
        if "_DRIVE_ivd" not in inner_html:
            continue

        try:
            return next(
                match.group(1)
                for index, match in enumerate(string_pattern.finditer(inner_html))
                if index == 1
            )
        except StopIteration:
            raise DownloadError("Found _DRIVE_ivd but could not extract its data string")

    return None


def folder_name_from_title(title: str) -> Optional[str]:
    title = html_module.unescape(title).strip()
    if not title:
        return None

    for suffix in (" - Google Drive", " - My Drive", " - Shared with me"):
        if title.endswith(suffix):
            return title[: -len(suffix)].strip()

    return title


def decode_drive_name(name: str) -> str:
    try:
        name = name.encode("raw_unicode_escape").decode("utf-8")
    except UnicodeDecodeError:
        pass
    return html_module.unescape(name)


def get_output_path(args: argparse.Namespace, response: requests.Response) -> Path:
    out_dir = None
    if args.output:
        out_path = Path(args.output)
        if getattr(args, "is_multiple", False) or out_path.is_dir():
            out_dir = out_path
        else:
            return out_path

    filename = filename_from_headers(response)
    if not filename:
        filename = filename_from_content_type(args, response)
    if not filename:
        parsed = urlparse(response.url)
        basename = Path(unquote(parsed.path)).name
        if basename and basename not in {"download", "videoplayback"}:
            filename = sanitize_filename(basename)
    if not filename:
        if args.file_id:
            filename = f"{args.file_id}.bin"
        elif args.url:
            file_id = extract_file_id(args.url)
            if file_id:
                filename = f"{file_id}.bin"
    if not filename:
        filename = "download.bin"

    if out_dir:
        return out_dir / filename
    return Path(filename)


def filename_from_content_type(
    args: argparse.Namespace,
    response: requests.Response,
) -> Optional[str]:
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if not content_type:
        return None

    extension = mimetypes.guess_extension(content_type)
    if content_type == "video/mp4":
        extension = ".mp4"
    if not extension:
        return None

    if args.file_id:
        return sanitize_filename(f"{args.file_id}{extension}")
    if args.url:
        file_id = extract_file_id(args.url)
        if file_id:
            return sanitize_filename(f"{file_id}{extension}")
    return None


def filename_from_headers(response: requests.Response) -> Optional[str]:
    content_disposition = response.headers.get("Content-Disposition")
    if not content_disposition:
        return None

    message = Message()
    message["Content-Disposition"] = content_disposition
    filename = message.get_filename()
    if filename:
        return sanitize_filename(filename)

    match = re.search(r"filename\*=UTF-8''([^;]+)", content_disposition, re.I)
    if match:
        return sanitize_filename(unquote(match.group(1).strip('"')))

    match = re.search(r'filename="?([^";]+)"?', content_disposition, re.I)
    if match:
        return sanitize_filename(match.group(1))

    return None


def sanitize_filename(filename: str) -> str:
    filename = filename.strip().replace("\\", "_").replace("/", "_")
    filename = re.sub(r'[\x00-\x1f<>:"|?*]', "_", filename)
    return filename or "download.bin"


def is_google_workspace_mime(mime_type: str) -> bool:
    return mime_type.startswith(GOOGLE_APPS_MIME_PREFIX) and mime_type != GOOGLE_FOLDER_MIME


def workspace_export_spec(mime_type: str, export_format: str) -> Optional[ExportSpec]:
    if export_format == "pdf":
        return PDF_EXPORTS.get(mime_type) or OFFICE_EXPORTS.get(mime_type)
    return OFFICE_EXPORTS.get(mime_type)


def output_name_for_item(item: DriveItem, export_format: str) -> str:
    name = sanitize_filename(item.name)
    if is_google_workspace_mime(item.mime_type):
        spec = workspace_export_spec(item.mime_type, export_format)
        if spec and not name.lower().endswith("." + spec.extension.lower()):
            name = f"{name}.{spec.extension}"
    return name


def unique_path(parent: Path, name: str, used_names: set[str]) -> Path:
    candidate = sanitize_filename(name)
    stem = Path(candidate).stem
    suffix = Path(candidate).suffix
    index = 2
    while candidate.lower() in used_names:
        candidate = f"{stem} ({index}){suffix}"
        index += 1
    used_names.add(candidate.lower())
    return parent / candidate


def content_total(response: requests.Response, resumed_from: int) -> Optional[int]:
    content_range = response.headers.get("Content-Range")
    if content_range:
        match = re.search(r"/(\d+)$", content_range)
        if match:
            return int(match.group(1))

    content_length = response.headers.get("Content-Length")
    if content_length and content_length.isdigit():
        return int(content_length) + resumed_from

    return None


def content_range_start(response: requests.Response) -> int:
    content_range = response.headers.get("Content-Range", "")
    match = re.search(r"bytes\s+(\d+)-\d+/", content_range)
    if match:
        return int(match.group(1))
    return 0


def reusable_request_headers(response: requests.Response) -> dict[str, str]:
    blocked = {"host", "content-length", "connection", "range"}
    return {
        key: value
        for key, value in response.request.headers.items()
        if key.lower() not in blocked
    }


def should_segment_download(
    args: argparse.Namespace,
    response: requests.Response,
    output: Path,
    start_offset: int,
    total: Optional[int],
) -> bool:
    segments = getattr(args, "segments", 1)
    threshold = getattr(args, "segment_threshold", DEFAULT_SEGMENT_THRESHOLD)
    if segments <= 1 or total is None:
        return False
    if total <= start_offset:
        return False
    if total - start_offset < threshold:
        return False
    if response.status_code not in (200, 206):
        return False

    # A partial final file from the old single-stream path is resumed with the
    # single stream path. Segmented downloads keep their own .parts directory.
    if output.exists() and output.stat().st_size > 0 and response.status_code != 206:
        return False
    return True


def range_probe_supported(
    args: argparse.Namespace,
    response: requests.Response,
    start_offset: int,
) -> bool:
    if response.status_code == 206:
        return True

    headers = reusable_request_headers(response)
    headers["Range"] = f"bytes={start_offset}-{start_offset}"
    probe_session = build_session(args)
    probe = probe_session.get(
        response.url,
        headers=headers,
        stream=True,
        allow_redirects=True,
        timeout=args.timeout,
        verify=not args.insecure,
    )
    try:
        return probe.status_code == 206
    finally:
        probe.close()


def build_segment_ranges(start_offset: int, total: int, segments: int) -> list[tuple[int, int]]:
    remaining = total - start_offset
    count = min(max(segments, 1), remaining)
    segment_size = (remaining + count - 1) // count
    ranges = []
    start = start_offset
    while start < total:
        end = min(start + segment_size - 1, total - 1)
        ranges.append((start, end))
        start = end + 1
    return ranges


def segmented_download(
    args: argparse.Namespace,
    response: requests.Response,
    output: Path,
    start_offset: int,
    total: int,
    progress_callback = None,
) -> Path:
    if not range_probe_supported(args, response, start_offset):
        raise DownloadError("Server did not honor Range requests for segmented download")

    url = response.url
    headers = reusable_request_headers(response)
    cookie_header = cookie_header_for_url(build_session(args), url)
    if cookie_header and "Cookie" not in headers:
        headers["Cookie"] = cookie_header
    response.close()

    initial_completed = [(0, start_offset - 1)] if start_offset > 0 else []
    downloader = IDMDownloader(
        url=url,
        final_url=url,
        total_size=total,
        output_path=output,
        headers=headers,
        concurrency=args.segments,
        chunk_size=args.chunk_size,
        checkpoint_path=output.with_name(output.name + ".idm.json"),
        timeout=args.timeout,
        insecure=args.insecure,
        resume=args.resume,
        seed_path=output if initial_completed else None,
        initial_completed_ranges=initial_completed,
        progress_callback=progress_callback,
    )

    try:
        return downloader.download_sync()
    except IDMDownloadError as exc:
        raise DownloadError(str(exc)) from exc


def cookie_header_for_url(session: requests.Session, url: str) -> Optional[str]:
    request = requests.Request("GET", url)
    prepared = session.prepare_request(request)
    return get_cookie_header(session.cookies, prepared)


def download_segment_part(
    args: argparse.Namespace,
    url: str,
    base_headers: dict[str, str],
    start: int,
    end: int,
    part_path: Path,
    existing_size: int,
    progress_events: "queue.Queue[int]",
) -> None:
    if existing_size >= end - start + 1:
        return

    range_start = start + existing_size
    headers = dict(base_headers)
    headers["Range"] = f"bytes={range_start}-{end}"
    session = build_session(args)
    response = session.get(
        url,
        headers=headers,
        stream=True,
        allow_redirects=True,
        timeout=args.timeout,
        verify=not args.insecure,
    )
    try:
        if response.status_code != 206:
            raise DownloadError(
                f"Segment {range_start}-{end} returned HTTP {response.status_code}"
            )
        with part_path.open("ab") as file:
            for chunk in response.iter_content(chunk_size=args.chunk_size):
                if not chunk:
                    continue
                file.write(chunk)
                progress_events.put(len(chunk))
    finally:
        response.close()


def merge_segment_parts(
    output: Path,
    parts_dir: Path,
    part_specs: list[tuple[int, int, int, Path, int]],
    prefix_size: int,
) -> None:
    temp_output = output.with_name(output.name + ".merge.tmp")
    with temp_output.open("wb") as target:
        if prefix_size > 0:
            if not output.exists() or output.stat().st_size < prefix_size:
                raise DownloadError(
                    f"Existing partial file is smaller than expected prefix: {output}"
                )
            with output.open("rb") as prefix:
                remaining = prefix_size
                while remaining > 0:
                    chunk = prefix.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise DownloadError(
                            f"Could not read expected prefix from partial file: {output}"
                        )
                    target.write(chunk)
                    remaining -= len(chunk)

        for _, start, end, part_path, _ in part_specs:
            expected_size = end - start + 1
            actual_size = part_path.stat().st_size if part_path.exists() else 0
            if actual_size != expected_size:
                raise DownloadError(
                    f"Incomplete segment {part_path.name}: "
                    f"{actual_size} of {expected_size} bytes"
                )
            with part_path.open("rb") as source:
                shutil.copyfileobj(source, target, length=1024 * 1024)
    temp_output.replace(output)
    shutil.rmtree(parts_dir, ignore_errors=True)


def download_folder(args: argparse.Namespace) -> list[Path]:
    session = build_session(args)
    folder_id = args.folder_id
    if args.folder_url:
        folder_id = extract_folder_id(args.folder_url)
    elif args.url and is_probably_folder_url(args.url):
        folder_id = extract_folder_id(args.url)

    if not folder_id:
        raise DownloadError("Could not extract Google Drive folder id")

    root = fetch_drive_folder(args, session, folder_id)
    if args.list_only:
        print_folder_tree(root, args)
        return []

    root_path = folder_output_root(args.output, root.name)
    if root_path.exists() and not root_path.is_dir():
        raise DownloadError(f"Folder output path is not a directory: {root_path}")
    root_path.mkdir(parents=True, exist_ok=True)

    downloaded: list[Path] = []
    failed: list[tuple[DriveItem, Path, str]] = []
    download_folder_children(args, session, root, root_path, downloaded, failed)

    if failed:
        for item, path, error in failed:
            print(f"Failed: {item.name} -> {path}: {error}", file=sys.stderr)
        raise DownloadError(
            f"Folder download finished with {len(failed)} failed file(s) "
            f"and {len(downloaded)} downloaded file(s)."
        )

    print(f"Folder download completed: {root_path.resolve()}", file=sys.stderr)
    return downloaded


def folder_output_root(output: Optional[str], root_name: str) -> Path:
    root_dir_name = sanitize_filename(root_name)
    if not output:
        return Path(root_dir_name)

    parent = Path(output)
    if parent.name.lower() == root_dir_name.lower():
        return parent
    return parent / root_dir_name


def download_folder_children(
    args: argparse.Namespace,
    session: requests.Session,
    folder: DriveItem,
    local_dir: Path,
    downloaded: list[Path],
    failed: list[tuple[DriveItem, Path, str]],
) -> None:
    used_names: set[str] = set()
    subdirs = []
    files_to_download = []
    
    for child in folder.children:
        if child.is_folder:
            child_dir = unique_path(local_dir, child.name, used_names)
            child_dir.mkdir(parents=True, exist_ok=True)
            subdirs.append((child, child_dir))
            continue

        local_path = unique_path(local_dir, output_name_for_item(child, args.workspace_format), used_names)
        item_args = argparse.Namespace(**vars(args))
        item_args.url = None
        item_args.file_id = child.id
        item_args.output = str(local_path)
        item_args.mime_type = child.mime_type
        files_to_download.append((child, local_path, item_args))
        
    for child, child_dir in subdirs:
        download_folder_children(args, session, child, child_dir, downloaded, failed)

    if not files_to_download:
        return

    def _download_task(task_info):
        child, local_path, item_args = task_info
        print_msg(f"Downloading: {child.name} -> {local_path}")
        try:
            return child, local_path, download(item_args, session=session), None
        except (requests.RequestException, OSError, DownloadError) as exc:
            return child, local_path, None, str(exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(files_to_download), 4)) as executor:
        for child, local_path, path, error in executor.map(_download_task, files_to_download):
            if error:
                failed.append((child, local_path, error))
            elif path:
                downloaded.append(path)


def print_folder_tree(folder: DriveItem, args: argparse.Namespace, indent: str = "") -> None:
    print(f"{indent}{folder.name}/")
    child_indent = indent + "  "
    for child in folder.children:
        if child.is_folder:
            print_folder_tree(child, args, child_indent)
        else:
            print(f"{child_indent}{output_name_for_item(child, args.workspace_format)}")


def download(args: argparse.Namespace, session: Optional[requests.Session] = None) -> Path:
    if session is None:
        session = build_session(args)
    response = open_response_with_cookie_refresh(args, session)

    output = get_output_path(args, response)
    output.parent.mkdir(parents=True, exist_ok=True)

    existing_size = output.stat().st_size if output.exists() else 0
    resumed_from = 0
    mode = "wb"

    if response.status_code == 416 and args.resume and existing_size > 0:
        print_msg(f"Already complete or server rejected range: {output}")
        return output

    if args.resume and existing_size > 0:
        if response.status_code == 206:
            resumed_from = existing_size
            mode = "ab"
            print_msg(f"Resuming from {format_bytes(resumed_from)}")
        else:
            print_msg("Server ignored Range; restarting from byte 0")

    total = content_total(response, resumed_from)
    start_offset = content_range_start(response) if response.status_code == 206 else 0
    if args.resume and response.status_code == 206 and existing_size > 0:
        start_offset = existing_size

    progress = get_progress()
    task_id = progress.add_task("download", filename=output.name, total=total, completed=resumed_from)
    
    def _progress_callback(written: int, total_size: int, speed: float) -> None:
        try:
            if total_size:
                progress.update(task_id, total=total_size, completed=written)
            else:
                progress.update(task_id, completed=written)
        except Exception:
            pass

    if should_segment_download(args, response, output, start_offset, total):
        try:
            res = segmented_download(args, response, output, start_offset, total, _progress_callback)
            progress.remove_task(task_id)
            return res
        except DownloadError as exc:
            print_msg(f"Segmented download unavailable: {exc}")
            response = open_response_with_cookie_refresh(args, session)
            total = content_total(response, resumed_from)

    written = resumed_from
    started = time.monotonic()
    last_report = 0.0

    with output.open(mode) as file:
        for chunk in response.iter_content(chunk_size=args.chunk_size):
            if not chunk:
                continue
            file.write(chunk)
            written += len(chunk)
            if not RICH_AVAILABLE:
                last_report = print_progress(written, total, started, last_report)
            else:
                if total:
                    progress.update(task_id, total=total, completed=written)
                else:
                    progress.update(task_id, completed=written)

    response.close()
    if not RICH_AVAILABLE:
        print_progress(written, total, started, 0.0, final=True)
    else:
        progress.update(task_id, completed=written)
        progress.remove_task(task_id)
        
    print_msg(f"Saved to: {output.resolve()}")
    return output


def print_progress(
    written: int,
    total: Optional[int],
    started: float,
    last_report: float,
    final: bool = False,
) -> float:
    now = time.monotonic()
    if not final and now - last_report < 0.5:
        return last_report

    elapsed = max(now - started, 0.001)
    speed = written / elapsed
    if total:
        percent = min(written / total * 100, 100)
        message = (
            f"{format_bytes(written)} / {format_bytes(total)} "
            f"({percent:5.1f}%) at {format_bytes(speed)}/s"
        )
    else:
        message = f"{format_bytes(written)} at {format_bytes(speed)}/s"

    print("\r" + message.ljust(90), end="", file=sys.stderr, flush=True)
    return now


def format_bytes(value: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TB"


def main() -> int:
    args = parse_args()
    try:
        items = []
        if args.folder_id:
            items = [("folder_id", val) for val in args.folder_id]
        elif args.folder_url:
            items = [("folder_url", val) for val in args.folder_url]
        elif args.file_id:
            items = [("file_id", val) for val in args.file_id]
        elif args.url:
            items = [("url", val) for val in args.url]

        is_multiple = len(items) > 1
        
        progress_ctx = get_progress() if RICH_AVAILABLE else contextlib.nullcontext()
        with progress_ctx:
            for item_type, item_val in items:
                item_args = argparse.Namespace(**vars(args))
                item_args.folder_id = None
                item_args.folder_url = None
                item_args.file_id = None
                item_args.url = None
                item_args.is_multiple = is_multiple
                setattr(item_args, item_type, item_val)
                
                if item_type in ("folder_id", "folder_url") or (item_type == "url" and is_probably_folder_url(item_val)):
                    download_folder(item_args)
                elif item_args.list_only:
                    raise DownloadError("--list-only is only valid for folder downloads")
                else:
                    download(item_args)
        return 0
    except KeyboardInterrupt:
        print("\nCancelled", file=sys.stderr)
        return 130
    except (requests.exceptions.RetryError, requests.RequestException, OSError, DownloadError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
