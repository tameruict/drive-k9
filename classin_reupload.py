from __future__ import annotations

import argparse
import io
import json
import os
import pathlib
import re
import threading
import time
from collections import OrderedDict, Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload


FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
VIDEO_MIME = "video/mp4"
UPLOAD_CHUNK_SIZE = 64 * 1024 * 1024
RANGE_BUFFER_SIZE = 8 * 1024 * 1024
URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.I)


def sanitize_error(error: BaseException | str) -> str:
    value = str(error)
    value = URL_RE.sub("<url>", value)
    return value[:500]


def drive_query_literal(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


def atomic_write_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def load_payloads(media_path: pathlib.Path, mapping_path: pathlib.Path) -> tuple[dict, dict]:
    media = json.loads(media_path.read_text(encoding="utf-8-sig"))
    mapping = json.loads(mapping_path.read_text(encoding="utf-8-sig"))
    if media.get("version") != 1 or mapping.get("version") != 1:
        raise ValueError("Unsupported ClassIn payload version")
    if not media.get("manifest_id") or media.get("manifest_id") != mapping.get("manifest_id"):
        raise ValueError("Media and mapping payload manifest IDs do not match")
    media_ids: set[str] = set()
    for item in media.get("media", []):
        media_id = str(item.get("id", ""))
        if not media_id or media_id in media_ids:
            raise ValueError(f"Invalid or duplicate media id: {media_id}")
        if item.get("format") != "mp4":
            raise ValueError(f"Drive uploader v1 only supports mp4: {media_id}")
        sources = item.get("sources", [])
        if not sources or any(not str(url).startswith("https://") for url in sources):
            raise ValueError(f"Missing HTTPS sources: {media_id}")
        if len(set(sources)) != len(sources):
            raise ValueError(f"Duplicate sources in payload: {media_id}")
        media_ids.add(media_id)
    activity_keys: set[str] = set()
    for activity in mapping.get("activities", []):
        key = str(activity.get("key", ""))
        if not key or key in activity_keys:
            raise ValueError(f"Invalid or duplicate activity key: {key}")
        activity_keys.add(key)
        if not activity.get("path") or not activity.get("refs"):
            raise ValueError(f"Activity is missing path or refs: {key}")
        for ref in activity["refs"]:
            if ref.get("id") not in media_ids or not str(ref.get("name", "")).strip():
                raise ValueError(f"Invalid media ref in activity: {key}")
    return media, mapping


def reference_index(mapping: dict[str, Any]) -> OrderedDict[str, list[dict[str, Any]]]:
    result: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for activity in mapping.get("activities", []):
        for ref in activity.get("refs", []):
            result.setdefault(ref["id"], []).append(
                {
                    "activity_key": activity["key"],
                    "activity_id": str(activity.get("activity_id", "")),
                    "path": list(activity["path"]),
                    "file_name": str(ref["name"]),
                }
            )
    return result


@dataclass
class HttpSource:
    url: str
    size: int
    headers: dict[str, str]


def probe_source(session: requests.Session, url: str, timeout: float = 30.0) -> HttpSource:
    response = session.get(
        url,
        headers={"Range": "bytes=0-0", "Accept": "*/*", "Accept-Encoding": "identity"},
        timeout=timeout,
        allow_redirects=True,
        stream=True,
    )
    try:
        content_range = response.headers.get("Content-Range", "")
        match = re.search(r"/(\d+)$", content_range)
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if response.status_code != 206 or content_type != VIDEO_MIME or not match:
            raise RuntimeError(f"Unexpected source response: HTTP {response.status_code}, {content_type or 'unknown'}")
        return HttpSource(
            url=url,
            size=int(match.group(1)),
            headers={"Accept": "*/*", "Accept-Encoding": "identity"},
        )
    finally:
        response.close()


class HttpRangeStream(io.RawIOBase):
    def __init__(self, session: requests.Session, source: HttpSource, buffer_size: int = RANGE_BUFFER_SIZE):
        super().__init__()
        self.session = session
        self.source = source
        self.buffer_size = buffer_size
        self.position = 0
        self.buffer = b""
        self.buffer_start = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.position

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if whence == os.SEEK_SET:
            target = offset
        elif whence == os.SEEK_CUR:
            target = self.position + offset
        elif whence == os.SEEK_END:
            target = self.source.size + offset
        else:
            raise ValueError("Invalid seek mode")
        self.position = max(0, min(int(target), self.source.size))
        return self.position

    def read(self, size: int = -1) -> bytes:
        if self.closed:
            raise ValueError("I/O operation on closed stream")
        if self.position >= self.source.size:
            return b""
        if size is None or size < 0:
            size = self.source.size - self.position
        size = min(int(size), self.source.size - self.position)
        chunks: list[bytes] = []
        remaining = size
        while remaining > 0:
            if not self._buffer_contains(self.position):
                self._fetch_buffer()
            offset = self.position - self.buffer_start
            available = len(self.buffer) - offset
            if available <= 0:
                raise RuntimeError("HTTP range source returned an empty buffer")
            take = min(available, remaining)
            chunks.append(self.buffer[offset : offset + take])
            self.position += take
            remaining -= take
        return b"".join(chunks)

    def close(self) -> None:
        self.buffer = b""
        super().close()

    def _buffer_contains(self, position: int) -> bool:
        return self.buffer_start <= position < self.buffer_start + len(self.buffer)

    def _fetch_buffer(self) -> None:
        start = self.position
        end = min(start + self.buffer_size, self.source.size) - 1
        headers = dict(self.source.headers)
        headers["Range"] = f"bytes={start}-{end}"
        last_error: Exception | None = None
        for attempt in range(1, 4):
            response = None
            try:
                response = self.session.get(
                    self.source.url,
                    headers=headers,
                    timeout=90,
                    stream=True,
                )
                if response.status_code != 206:
                    raise RuntimeError(f"HTTP range failed with status {response.status_code}")
                data = b"".join(chunk for chunk in response.iter_content(1024 * 1024) if chunk)
                if not data:
                    raise RuntimeError("HTTP range returned no data")
                self.buffer = data
                self.buffer_start = start
                return
            except Exception as exc:
                last_error = exc
                if attempt < 3:
                    time.sleep(2**attempt)
            finally:
                if response is not None:
                    response.close()
        raise RuntimeError(sanitize_error(last_error or "range request failed"))


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
            fields="files(id,name,mimeType,size,appProperties,shortcutDetails)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        return next(iter(response.get("files", [])), None)

    def find_media(self, media_id: str, dest_root: str) -> dict | None:
        query = (
            "appProperties has { key='classin_media_id' and value='"
            + drive_query_literal(media_id)
            + "' } and appProperties has { key='classin_dest_root' and value='"
            + drive_query_literal(dest_root)
            + "' } and trashed = false"
        )
        response = self.service.files().list(
            q=query,
            pageSize=20,
            fields="files(id,name,mimeType,size,parents,appProperties)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        return next(iter(response.get("files", [])), None)

    def get_file(self, file_id: str) -> dict | None:
        try:
            return self.service.files().get(
                fileId=file_id,
                fields="id,name,mimeType,size,parents,trashed,appProperties,shortcutDetails",
                supportsAllDrives=True,
            ).execute()
        except Exception:
            return None

    def ensure_folder(self, parent_id: str, name: str) -> str:
        existing = self.find_named(parent_id, name, FOLDER_MIME)
        if existing:
            return existing["id"]
        created = self.service.files().create(
            body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
            fields="id",
            supportsAllDrives=True,
        ).execute()
        return created["id"]

    def upload_http_source(
        self,
        session: requests.Session,
        source: HttpSource,
        name: str,
        parent_id: str,
        media_id: str,
        manifest_id: str,
        dest_root: str,
    ) -> str:
        stream = HttpRangeStream(session, source)
        media = MediaIoBaseUpload(stream, mimetype=VIDEO_MIME, chunksize=UPLOAD_CHUNK_SIZE, resumable=True)
        request = self.service.files().create(
            body={
                "name": name,
                "parents": [parent_id],
                "appProperties": {
                    "classin_media_id": media_id,
                    "classin_manifest": manifest_id[:64],
                    "classin_dest_root": dest_root,
                },
            },
            media_body=media,
            fields="id,size,mimeType",
            supportsAllDrives=True,
        )
        response = None
        try:
            while response is None:
                _, response = request.next_chunk(num_retries=3)
            return response["id"]
        finally:
            stream.close()

    def ensure_shortcut(
        self,
        parent_id: str,
        name: str,
        target_id: str,
        media_id: str,
        activity_id: str,
    ) -> tuple[str, bool]:
        existing = self.find_named(parent_id, name, SHORTCUT_MIME)
        if existing and existing.get("shortcutDetails", {}).get("targetId") == target_id:
            return existing["id"], False
        if existing:
            stem, suffix = os.path.splitext(name)
            name = f"{stem} [ClassIn {media_id[-8:]}]{suffix}"
            second = self.find_named(parent_id, name, SHORTCUT_MIME)
            if second and second.get("shortcutDetails", {}).get("targetId") == target_id:
                return second["id"], False
        created = self.service.files().create(
            body={
                "name": name,
                "mimeType": SHORTCUT_MIME,
                "parents": [parent_id],
                "shortcutDetails": {"targetId": target_id},
                "appProperties": {
                    "classin_media_id": media_id,
                    "classin_activity_id": activity_id,
                },
            },
            fields="id,shortcutDetails",
            supportsAllDrives=True,
        ).execute()
        return created["id"], True


class CredentialFactory:
    def __init__(self, token_file: pathlib.Path):
        info = json.loads(token_file.read_text(encoding="utf-8-sig"))
        credentials = Credentials.from_authorized_user_info(info)
        if not credentials.valid and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        self.info = json.loads(credentials.to_json())

    def client(self) -> DriveClient:
        return DriveClient(Credentials.from_authorized_user_info(self.info))


class ReuploadEngine:
    def __init__(
        self,
        media_payload: dict[str, Any],
        mapping_payload: dict[str, Any],
        credential_factory: CredentialFactory,
        dest_folder_id: str,
        checkpoint_file: pathlib.Path,
        report_file: pathlib.Path,
        max_workers: int = 3,
        force_retry: bool = False,
    ):
        self.media_payload = media_payload
        self.mapping_payload = mapping_payload
        self.credentials = credential_factory
        self.dest_folder_id = dest_folder_id
        self.checkpoint_file = checkpoint_file
        self.report_file = report_file
        self.max_workers = max(1, max_workers)
        self.force_retry = force_retry
        self.manifest_id = str(media_payload["manifest_id"])
        self.refs = reference_index(mapping_payload)
        self.media_by_id = OrderedDict((item["id"], item) for item in media_payload["media"])
        self.folder_ids: dict[tuple[str, ...], str] = {}
        self.lock = threading.RLock()
        self.checkpoint = self._load_checkpoint()
        self.stats: Counter[str] = Counter()
        self.results: list[dict[str, Any]] = []

    def _load_checkpoint(self) -> dict[str, Any]:
        if self.checkpoint_file.exists():
            try:
                value = json.loads(self.checkpoint_file.read_text(encoding="utf-8-sig"))
                if value.get("manifest_id") == self.manifest_id:
                    return value
            except Exception:
                pass
        return {"manifest_id": self.manifest_id, "media": {}, "shortcuts": {}}

    def _save_checkpoint(self) -> None:
        with self.lock:
            atomic_write_json(self.checkpoint_file, self.checkpoint)

    def prepare_folders(self) -> None:
        client = self.credentials.client()
        self.folder_ids[tuple()] = self.dest_folder_id
        unique_paths = OrderedDict()
        for activity in self.mapping_payload["activities"]:
            unique_paths.setdefault(tuple(activity["path"]), None)
        for path in unique_paths:
            parent = self.dest_folder_id
            prefix: tuple[str, ...] = tuple()
            for segment in path:
                prefix = prefix + (str(segment),)
                if prefix not in self.folder_ids:
                    self.folder_ids[prefix] = client.ensure_folder(parent, str(segment))
                parent = self.folder_ids[prefix]

    def run(self) -> dict[str, Any]:
        if os.environ.get("GITHUB_ACTIONS"):
            for item in self.media_by_id.values():
                for url in item["sources"]:
                    print(f"::add-mask::{url}")
        self.prepare_folders()
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {pool.submit(self._process_media, media_id): media_id for media_id in self.media_by_id}
            for future in as_completed(futures):
                media_id = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {
                        "media_id": media_id,
                        "status": "failed",
                        "error": sanitize_error(exc),
                    }
                    with self.lock:
                        self.stats["failed"] += 1
                with self.lock:
                    self.results.append(result)
                    print(json.dumps(result, ensure_ascii=False))
        report = {
            "manifest_id": self.manifest_id,
            "stats": {
                "media_total": len(self.media_by_id),
                "mapping_total": sum(len(values) for values in self.refs.values()),
                **dict(self.stats),
            },
            "items": sorted(self.results, key=lambda item: item["media_id"]),
        }
        atomic_write_json(self.report_file, report)
        return report

    def _process_media(self, media_id: str) -> dict[str, Any]:
        item = self.media_by_id[media_id]
        references = self.refs.get(media_id, [])
        if not references:
            raise RuntimeError("Media has no activity references")
        primary_ref = references[0]
        parent_id = self.folder_ids[tuple(primary_ref["path"])]
        client = self.credentials.client()
        expected_size = int(item.get("size") or 0)
        file_id = None
        status = "uploaded"
        if not self.force_retry:
            checkpoint_item = self.checkpoint.get("media", {}).get(media_id)
            if checkpoint_item:
                metadata = client.get_file(str(checkpoint_item.get("file_id", "")))
                if self._valid_existing(metadata, expected_size):
                    file_id = metadata["id"]
                    status = "checkpoint"
            if file_id is None:
                metadata = client.find_media(media_id, self.dest_folder_id)
                if self._valid_existing(metadata, expected_size):
                    file_id = metadata["id"]
                    status = "existing"
        actual_size = expected_size
        if file_id is None:
            file_id, actual_size = self._upload_with_fallbacks(
                client,
                item["sources"],
                primary_ref["file_name"],
                parent_id,
                media_id,
                expected_size,
            )
        with self.lock:
            self.checkpoint.setdefault("media", {})[media_id] = {
                "file_id": file_id,
                "size": actual_size,
                "name": primary_ref["file_name"],
                "parent_id": parent_id,
            }
            self.stats[status] += 1
            self._save_checkpoint()
        shortcuts_created = 0
        shortcuts_existing = 0
        for alias in references[1:]:
            alias_parent = self.folder_ids[tuple(alias["path"])]
            shortcut_key = f"{alias['activity_key']}|{media_id}"
            shortcut_id, created = client.ensure_shortcut(
                alias_parent,
                alias["file_name"],
                file_id,
                media_id,
                alias["activity_id"],
            )
            with self.lock:
                self.checkpoint.setdefault("shortcuts", {})[shortcut_key] = {
                    "shortcut_id": shortcut_id,
                    "target_id": file_id,
                }
                if created:
                    shortcuts_created += 1
                    self.stats["shortcuts_created"] += 1
                else:
                    shortcuts_existing += 1
                    self.stats["shortcuts_existing"] += 1
                self._save_checkpoint()
        return {
            "media_id": media_id,
            "status": status,
            "name": primary_ref["file_name"],
            "size": actual_size,
            "aliases": len(references) - 1,
            "shortcuts_created": shortcuts_created,
            "shortcuts_existing": shortcuts_existing,
        }

    @staticmethod
    def _valid_existing(metadata: dict | None, expected_size: int) -> bool:
        if not metadata or metadata.get("trashed") is True or metadata.get("mimeType") != VIDEO_MIME:
            return False
        size = int(metadata.get("size") or 0)
        return expected_size <= 0 or size == expected_size

    def _upload_with_fallbacks(
        self,
        client: DriveClient,
        urls: list[str],
        name: str,
        parent_id: str,
        media_id: str,
        expected_size: int,
    ) -> tuple[str, int]:
        errors: list[str] = []
        for index, url in enumerate(urls, start=1):
            session = requests.Session()
            session.trust_env = False
            session.headers.update({"User-Agent": "Mozilla/5.0"})
            try:
                source = probe_source(session, url)
                if expected_size > 0 and source.size != expected_size:
                    raise RuntimeError(f"Source size mismatch: {source.size} != {expected_size}")
                file_id = client.upload_http_source(
                    session,
                    source,
                    name,
                    parent_id,
                    media_id,
                    self.manifest_id,
                    self.dest_folder_id,
                )
                return file_id, source.size
            except Exception as exc:
                errors.append(f"source {index}: {type(exc).__name__}: {sanitize_error(exc)}")
            finally:
                session.close()
        raise RuntimeError("; ".join(errors) or "No sources available")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upload a ClassIn video manifest to Google Drive")
    parser.add_argument("--media-payload", type=pathlib.Path, required=True)
    parser.add_argument("--mapping-payload", type=pathlib.Path, required=True)
    parser.add_argument("--token-file", type=pathlib.Path, required=True)
    parser.add_argument("--dest-folder-id", required=True)
    parser.add_argument("--checkpoint-file", type=pathlib.Path, default=pathlib.Path("classin_reupload_checkpoint.json"))
    parser.add_argument("--report-file", type=pathlib.Path, default=pathlib.Path("classin_reupload_report.json"))
    parser.add_argument("--max-workers", type=int, default=3)
    parser.add_argument("--force-retry", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    media, mapping = load_payloads(args.media_payload, args.mapping_payload)
    engine = ReuploadEngine(
        media,
        mapping,
        CredentialFactory(args.token_file),
        args.dest_folder_id,
        args.checkpoint_file,
        args.report_file,
        max_workers=args.max_workers,
        force_retry=args.force_retry,
    )
    report = engine.run()
    print(json.dumps(report["stats"], ensure_ascii=False))
    return 1 if report["stats"].get("failed", 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
