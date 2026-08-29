"""Download Sangsang course HLS videos and upload them to Google Drive.

The course JSON is public, while the storage HLS URLs are intentionally
resolved through the same ``/api/playlist`` proxy used by the web player.
Each item is converted to MP4 by ffmpeg, uploaded resumably, and removed from
the runner after a successful upload.  Folder paths follow the course UI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import threading
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, quote, urljoin, urlsplit

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


DEFAULT_BASE_URL = "https://sangsang-thuvien.vercel.app/"
DEFAULT_COURSE_SLUG = "dgnl-bca"
DEFAULT_DOWNLOAD_TIMEOUT = 7200
VIDEO_MIME = "video/mp4"
FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
UPLOAD_CHUNK_SIZE = 64 * 1024 * 1024
URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.I)
INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_error(error: BaseException | str) -> str:
    return URL_RE.sub("<url>", str(error))[:1000]


def atomic_write_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def drive_query_literal(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


def safe_filename(title: str, order: int, lesson_id: str = "") -> str:
    value = INVALID_FILENAME.sub(" ", str(title or "Bai hoc"))
    value = re.sub(r"\s+", " ", value).strip(" .") or "Bai hoc"
    suffix = f" [bai-{lesson_id}]" if lesson_id else ""
    return f"{order:03d} - {value[: max(1, 170 - len(suffix))]}{suffix}.mp4"


def course_json_url(slug: str, base_url: str = DEFAULT_BASE_URL) -> str:
    return base_url.rstrip("/") + "/data/c/" + quote(slug, safe="") + ".json"


def resolve_course_url(value: str, default_slug: str, base_url: str = DEFAULT_BASE_URL) -> tuple[str, str]:
    """Accept either a course slug, the course page URL, or its JSON URL."""
    raw = str(value or "").strip()
    if not raw:
        return course_json_url(default_slug, base_url), default_slug
    if raw.startswith("http"):
        parsed = urlsplit(raw)
        match = re.search(r"/data/c/([^/]+)\.json$", parsed.path)
        if match:
            return raw, match.group(1)
        slug = (parse_qs(parsed.query).get("khoa") or [default_slug])[0]
        return course_json_url(slug, base_url), slug
    return course_json_url(raw, base_url), raw


def playback_hls_url(source_hls: str, file_id: Any, base_url: str = DEFAULT_BASE_URL) -> str:
    if not source_hls or not file_id:
        return ""
    return (
        base_url.rstrip("/")
        + "/api/playlist?src="
        + quote(str(source_hls), safe="")
        + "&id="
        + quote(str(file_id), safe="")
        + "&seg=1"
    )


def fetch_course(url: str, session: requests.Session) -> dict[str, Any]:
    response = session.get(url, timeout=45)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("sections"):
        raise ValueError("Course response has no sections")
    return payload


@dataclass(frozen=True)
class Lesson:
    key: str
    lesson_id: str
    file_id: str
    title: str
    path: tuple[str, ...]
    drive_path: tuple[str, ...]
    order: int
    source_hls: str
    playback_hls: str
    file_name: str


def flatten_course(course: dict[str, Any], base_url: str = DEFAULT_BASE_URL) -> list[Lesson]:
    lessons: list[Lesson] = []

    def walk(
        sections: list[dict[str, Any]],
        path: tuple[str, ...] = (),
        drive_path: tuple[str, ...] = (),
    ) -> None:
        for section_index, section in enumerate(sections or [], start=1):
            current = path + (str(section.get("title") or "Chua dat ten"),)
            current_drive_path = drive_path + (
                f"{section_index:02d} - {current[-1]}",
            )
            local_order = 0
            for item in section.get("items", []) or []:
                if item.get("type") != "video":
                    continue
                local_order += 1
                lesson_id = str(item.get("lessonId") or "")
                file_id = str(item.get("fileId") or "")
                source_hls = str(item.get("hls") or "")
                key = lesson_id or file_id or hashlib.sha256(
                    source_hls.encode("utf-8")
                ).hexdigest()[:24]
                lessons.append(
                    Lesson(
                        key=key,
                        lesson_id=lesson_id,
                        file_id=file_id,
                        title=str(item.get("title") or "Bai hoc"),
                        path=current,
                        drive_path=current_drive_path,
                        order=local_order,
                        source_hls=source_hls,
                        playback_hls=playback_hls_url(source_hls, file_id, base_url),
                        file_name=safe_filename(
                            str(item.get("title") or "Bai hoc"),
                            local_order,
                            lesson_id,
                        ),
                    )
                )
            walk(section.get("children", []) or [], current, current_drive_path)

    walk(course.get("sections", []) or [])
    if len({lesson.key for lesson in lessons}) != len(lessons):
        raise ValueError("Course contains duplicate lesson keys")
    return lessons


def manifest_signature(course: dict[str, Any], slug: str) -> str:
    raw = json.dumps({"slug": slug, "course": course}, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def load_checkpoint(path: pathlib.Path, signature: str) -> dict[str, Any]:
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            if payload.get("signature") == signature:
                payload.setdefault("lessons", {})
                return payload
        except Exception:
            pass
    return {"version": 1, "signature": signature, "lessons": {}}


def build_credentials(token_file: pathlib.Path) -> Credentials:
    info = json.loads(token_file.read_text(encoding="utf-8-sig"))
    credentials = Credentials.from_authorized_user_info(info)
    if not credentials.valid and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    if not credentials.valid:
        raise RuntimeError("Drive OAuth token is not valid or refreshable")
    return credentials


class DriveClient:
    def __init__(self, credentials: Credentials):
        self.service = build("drive", "v3", credentials=credentials, cache_discovery=False)

    def find_named(self, parent_id: str, name: str, mime_type: str | None = None) -> dict | None:
        query = [
            f"'{drive_query_literal(parent_id)}' in parents",
            f"name = '{drive_query_literal(name)}'",
            "trashed = false",
        ]
        if mime_type:
            query.append(f"mimeType = '{drive_query_literal(mime_type)}'")
        response = self.service.files().list(
            q=" and ".join(query),
            pageSize=20,
            fields="files(id,name,mimeType,size,parents,appProperties,shortcutDetails)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        return next(iter(response.get("files", [])), None)

    def find_lesson(self, lesson: Lesson, dest_root: str) -> dict | None:
        query = (
            "appProperties has { key='sangsang_lesson_id' and value='"
            + drive_query_literal(lesson.key)
            + "' } and appProperties has { key='sangsang_dest_root' and value='"
            + drive_query_literal(dest_root)
            + "' } and trashed = false"
        )
        response = self.service.files().list(
            q=query,
            pageSize=20,
            fields="files(id,name,mimeType,size,parents,appProperties,trashed)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        return next(iter(response.get("files", [])), None)

    def ensure_folder(self, parent_id: str, name: str) -> str:
        existing = self.find_named(parent_id, name, FOLDER_MIME)
        if existing:
            return str(existing["id"])
        created = self.service.files().create(
            body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
            fields="id",
            supportsAllDrives=True,
        ).execute()
        return str(created["id"])

    def upload_mp4(self, path: pathlib.Path, lesson: Lesson, parent_id: str, dest_root: str) -> dict[str, Any]:
        media = MediaFileUpload(str(path), mimetype=VIDEO_MIME, chunksize=UPLOAD_CHUNK_SIZE, resumable=True)
        body = {
            "name": lesson.file_name,
            "parents": [parent_id],
            "appProperties": {
                "sangsang_lesson_id": lesson.key,
                "sangsang_file_id": lesson.file_id[:64],
                "sangsang_dest_root": dest_root,
                "sangsang_source": "sangsang-thuvien",
            },
        }
        request = self.service.files().create(
            body=body,
            media_body=media,
            fields="id,name,size,mimeType",
            supportsAllDrives=True,
        )
        response = None
        while response is None:
            _, response = request.next_chunk(num_retries=3)
        return dict(response)


def _variant_urls(master_url: str, master_text: str) -> list[tuple[int, str]]:
    variants: list[tuple[int, str]] = []
    pending_bandwidth = 0
    for line in master_text.splitlines():
        if line.startswith("#EXT-X-STREAM-INF:"):
            match = re.search(r"\bBANDWIDTH=(\d+)", line)
            pending_bandwidth = int(match.group(1)) if match else 0
        elif pending_bandwidth and line.strip() and not line.startswith("#"):
            variants.append((pending_bandwidth, urljoin(master_url, line.strip())))
            pending_bandwidth = 0
    return variants


def prepare_local_media_playlist(
    source_hls: str,
    file_id: str,
    base_url: str,
    work_dir: pathlib.Path,
    session: requests.Session,
) -> pathlib.Path:
    """Resolve a Sangsang master playlist to a local, ffmpeg-readable playlist.

    The storage playlist uses ``URI=\"ENCRYPTED\"`` for its AES key.  The web
    player replaces that URI with ``/api/key?id=...``; do the same here and
    make every media segment absolute before invoking ffmpeg.
    """
    master_response = session.get(source_hls, timeout=45)
    master_response.raise_for_status()
    master_text = master_response.text
    variants = _variant_urls(source_hls, master_text)
    selected_url = max(variants, key=lambda value: value[0])[1] if variants else source_hls
    media_response = session.get(selected_url, timeout=45) if variants else master_response
    if variants:
        media_response.raise_for_status()
    media_text = media_response.text
    key_url = base_url.rstrip("/") + "/api/key?id=" + quote(str(file_id), safe="")
    rewritten: list[str] = []
    for line in media_text.splitlines():
        if line.startswith("#EXT-X-KEY:") and "URI=" in line:
            line = re.sub(r'URI=(?:"[^"]*"|\'[^\']*\')', f'URI="{key_url}"', line, count=1)
        elif line.startswith("#EXT-X-MAP:") and "URI=" in line:
            line = re.sub(
                r'URI="([^"]+)"',
                lambda match: f'URI="{urljoin(selected_url, match.group(1))}"',
                line,
                count=1,
            )
        elif line.strip() and not line.startswith("#"):
            line = urljoin(selected_url, line.strip())
        rewritten.append(line)
    output = work_dir / "media.m3u8"
    output.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    return output


def download_hls_to_mp4(
    playlist: pathlib.Path,
    output: pathlib.Path,
    timeout: int = 7200,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg was not found on the runner")
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-protocol_whitelist",
        "file,http,https,tcp,tls,crypto",
        "-rw_timeout",
        "30000000",
        "-i",
        str(playlist),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c",
        "copy",
        "-bsf:a",
        "aac_adtstoasc",
        "-movflags",
        "+faststart",
        str(output),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg exit {result.returncode}: {sanitize_error(result.stderr)}")
    if not output.exists() or output.stat().st_size < 1024:
        raise RuntimeError("ffmpeg produced an empty or invalid MP4")


class SangsangReupload:
    def __init__(
        self,
        course: dict[str, Any],
        slug: str,
        credentials: Credentials,
        dest_root: str,
        checkpoint_file: pathlib.Path,
        report_file: pathlib.Path,
        max_workers: int,
        force_retry: bool,
        base_url: str,
        download_timeout: int,
    ):
        self.course = course
        self.slug = slug
        self.credentials = credentials
        self.dest_root = dest_root
        self.checkpoint_file = checkpoint_file
        self.report_file = report_file
        self.max_workers = max(1, max_workers)
        self.force_retry = force_retry
        self.base_url = base_url
        self.download_timeout = max(60, int(download_timeout))
        self.lessons = flatten_course(course, base_url)
        self.signature = manifest_signature(course, slug)
        self.checkpoint = load_checkpoint(checkpoint_file, self.signature)
        self.folder_ids: dict[tuple[str, ...], str] = {(): dest_root}
        self.lock = threading.RLock()
        self.stats: Counter[str] = Counter()
        self.results: list[dict[str, Any]] = []

    def prepare_folders(self) -> None:
        client = DriveClient(self.credentials)
        unique_paths: OrderedDict[tuple[str, ...], None] = OrderedDict()
        for lesson in self.lessons:
            for depth in range(1, len(lesson.path) + 1):
                unique_paths.setdefault(lesson.drive_path[:depth], None)
        for path in unique_paths:
            parent = self.folder_ids[path[:-1]]
            self.folder_ids[path] = client.ensure_folder(parent, path[-1])

    @staticmethod
    def valid_existing(metadata: dict | None) -> bool:
        return bool(
            metadata
            and metadata.get("mimeType") == VIDEO_MIME
            and not metadata.get("trashed")
            and int(metadata.get("size") or 0) > 1024
        )

    def run(self) -> dict[str, Any]:
        if not self.lessons:
            raise RuntimeError("No video lessons found")
        self.prepare_folders()
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {pool.submit(self.process_lesson, lesson): lesson.key for lesson in self.lessons}
            for future in as_completed(futures):
                key = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {"lesson_id": key, "status": "failed", "error": sanitize_error(exc)}
                    with self.lock:
                        self.stats["failed"] += 1
                with self.lock:
                    self.results.append(result)
                    print(json.dumps(result, ensure_ascii=False))
        report = {
            "version": 1,
            "source": course_json_url(self.slug, self.base_url),
            "course_slug": self.slug,
            "course_title": self.course.get("title", ""),
            "signature": self.signature,
            "stats": {
                "lessons_total": len(self.lessons),
                "hls_available": sum(bool(item.playback_hls) for item in self.lessons),
                "uploaded": self.stats.get("uploaded", 0),
                "existing": self.stats.get("existing", 0),
                "checkpoint": self.stats.get("checkpoint", 0),
                "skipped_missing_hls": self.stats.get("skipped_missing_hls", 0),
                "failed": self.stats.get("failed", 0),
            },
            "items": sorted(self.results, key=lambda item: str(item.get("lesson_id", ""))),
        }
        atomic_write_json(self.report_file, report)
        return report

    def process_lesson(self, lesson: Lesson) -> dict[str, Any]:
        if not lesson.playback_hls:
            with self.lock:
                self.stats["skipped_missing_hls"] += 1
            return {
                "lesson_id": lesson.key,
                "title": lesson.title,
                "group": " / ".join(lesson.path),
                "drive_group": " / ".join(lesson.drive_path),
                "status": "skipped_missing_hls",
            }

        client = DriveClient(self.credentials)
        if not self.force_retry:
            checkpoint_item = self.checkpoint.get("lessons", {}).get(lesson.key)
            if checkpoint_item and self.valid_existing(client.find_lesson(lesson, self.dest_root)):
                with self.lock:
                    self.stats["checkpoint"] += 1
                return self._result(lesson, "checkpoint", checkpoint_item.get("file_id"), checkpoint_item.get("size", 0))
            existing = client.find_lesson(lesson, self.dest_root)
            if self.valid_existing(existing):
                with self.lock:
                    self.stats["existing"] += 1
                self._save_checkpoint(lesson, existing["id"], int(existing.get("size") or 0))
                return self._result(lesson, "existing", existing["id"], int(existing.get("size") or 0))

        parent_id = self.folder_ids[lesson.drive_path]
        with tempfile.TemporaryDirectory(prefix="sangsang-") as tmp:
            output = pathlib.Path(tmp) / lesson.file_name
            with requests.Session() as hls_session:
                hls_session.headers.update({"User-Agent": "drive-k9-sangsang/1.0"})
                playlist = prepare_local_media_playlist(
                    lesson.source_hls,
                    lesson.file_id,
                    self.base_url,
                    pathlib.Path(tmp),
                    hls_session,
                )
            download_hls_to_mp4(playlist, output, timeout=self.download_timeout)
            uploaded = client.upload_mp4(output, lesson, parent_id, self.dest_root)
        file_id = str(uploaded["id"])
        size = int(uploaded.get("size") or 0)
        with self.lock:
            self.stats["uploaded"] += 1
            self._save_checkpoint(lesson, file_id, size)
        return self._result(lesson, "uploaded", file_id, size)

    def _save_checkpoint(self, lesson: Lesson, file_id: str, size: int) -> None:
        with self.lock:
            self.checkpoint.setdefault("lessons", {})[lesson.key] = {
                "file_id": file_id,
                "size": size,
                "name": lesson.file_name,
                "path": list(lesson.drive_path),
                "display_path": list(lesson.path),
            }
            atomic_write_json(self.checkpoint_file, self.checkpoint)

    @staticmethod
    def _result(lesson: Lesson, status: str, file_id: Any, size: Any) -> dict[str, Any]:
        return {
            "lesson_id": lesson.key,
            "title": lesson.title,
            "group": " / ".join(lesson.path),
            "drive_group": " / ".join(lesson.drive_path),
            "status": status,
            "file_id": file_id,
            "size": int(size or 0),
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upload Sangsang HLS course videos to Google Drive")
    parser.add_argument("--course-slug", default=DEFAULT_COURSE_SLUG)
    parser.add_argument("--course-url", default="")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--token-file", type=pathlib.Path, required=True)
    parser.add_argument("--dest-folder-id", required=True)
    parser.add_argument("--checkpoint-file", type=pathlib.Path, default=pathlib.Path("sangsang_reupload_checkpoint.json"))
    parser.add_argument("--report-file", type=pathlib.Path, default=pathlib.Path("sangsang_reupload_report.json"))
    parser.add_argument("--max-workers", type=int, default=1)
    parser.add_argument(
        "--download-timeout",
        type=int,
        default=int(os.environ.get("SANGSANG_DOWNLOAD_TIMEOUT", DEFAULT_DOWNLOAD_TIMEOUT)),
        help="Maximum seconds allowed for one HLS-to-MP4 conversion",
    )
    parser.add_argument("--force-retry", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="Process only the first N lessons; 0 means all")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    with requests.Session() as session:
        session.headers.update({"User-Agent": "drive-k9-sangsang/1.0"})
        url, resolved_slug = resolve_course_url(args.course_url, args.course_slug, args.base_url)
        course = fetch_course(url, session)
    if args.limit > 0:
        course = json.loads(json.dumps(course, ensure_ascii=False))
        # Keep the source tree intact for grouping, but limit the flattened work set below.
    credentials = build_credentials(args.token_file)
    engine = SangsangReupload(
        course,
        resolved_slug,
        credentials,
        args.dest_folder_id,
        args.checkpoint_file,
        args.report_file,
        args.max_workers,
        args.force_retry,
        args.base_url,
        args.download_timeout,
    )
    if args.limit > 0:
        engine.lessons = engine.lessons[: args.limit]
    report = engine.run()
    print(json.dumps(report["stats"], ensure_ascii=False))
    return 1 if report["stats"].get("failed", 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
