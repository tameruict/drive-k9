"""Cross-account Google Drive sync: read with account A, write with account B.

Mục tiêu
--------
Acc A (ít dung lượng) chỉ cần quyền *xem* folder nguồn. Tool dùng token A để
tải từng file xuống đĩa của máy chạy (runner GitHub Actions hoặc máy local),
rồi dùng token B để upload vào Drive của B. Vì file chỉ đi qua đĩa tạm, **Drive
của A không tốn một byte quota nào**, và B là chủ sở hữu toàn bộ file ngay từ
đầu — không cần "transfer ownership" (vốn bị Google chặn khi A và B khác domain,
ví dụ Gmail cá nhân ↔ tài khoản .edu).

Luồng xử lý mỗi file: A get_media -> đĩa tạm -> B files().create (resumable) ->
xoá file tạm. Cây thư mục được tái tạo trong Drive B. Có checkpoint nên chạy lại
sẽ bỏ qua các file đã xong (hợp với giới hạn 6h mỗi job của Actions).

Ví dụ
-----
  python cross_account_sync.py \
      --source-token token_A.json \
      --dest-token token_B.json \
      --source-folder-id 1AbC... \
      --dest-folder-id 1XyZ... \
      --workers 4

  # Xem trước, không ghi gì:
  python cross_account_sync.py ... --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

# Windows console mặc định cp1252 không in được tên file tiếng Việt.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from drive_common import (
    FOLDER_MIME_TYPE,
    SHORTCUT_MIME_TYPE,
    drive_query_literal,
    is_retryable_drive_error,
    shortcut_target_id,
)

# Full Drive scope: A cần đọc/tải, B cần ghi/tạo file.
SCOPES = ["https://www.googleapis.com/auth/drive"]

# Google Workspace (Docs/Sheets/Slides...) không tải nhị phân trực tiếp được,
# phải export. Map mimeType gốc -> (export mime, đuôi file thêm vào).
EXPORT_MAP = {
    "application/vnd.google-apps.document": (
        "application/pdf",
        ".pdf",
    ),
    "application/vnd.google-apps.presentation": (
        "application/pdf",
        ".pdf",
    ),
    "application/vnd.google-apps.drawing": (
        "image/png",
        ".png",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
}

DOWNLOAD_CHUNK = 64 * 1024 * 1024  # 64MB mỗi lần đọc
UPLOAD_CHUNK = 64 * 1024 * 1024
MAX_RETRIES = 5
LIST_FIELDS = (
    "nextPageToken, files(id, name, mimeType, size, "
    "shortcutDetails(targetId, targetMimeType))"
)


def log(message: str) -> None:
    print(message, flush=True)


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
class DriveClient:
    """Drive client an toàn đa luồng.

    googleapiclient/httplib2 KHÔNG thread-safe: dùng chung 1 service cho nhiều
    luồng gây race trên socket/SSL → core dump ("futex ... unexpected error").
    Lớp này cấp cho mỗi luồng 1 Credentials + 1 service RIÊNG (thread-local),
    dựng từ cùng token info.
    """

    def __init__(self, token_path: str, label: str):
        path = Path(token_path)
        if not path.is_file():
            raise SystemExit(f"[{label}] Không tìm thấy token: {token_path}")
        try:
            # utf-8-sig: chịu được BOM
            self._info = json.loads(path.read_text(encoding="utf-8-sig"))
        except (ValueError, json.JSONDecodeError, OSError) as exc:
            raise SystemExit(f"[{label}] Token JSON không hợp lệ: {exc}")
        if "refresh_token" not in self._info:
            raise SystemExit(
                f"[{label}] Token thiếu refresh_token. Chạy authorize.py lại."
            )
        self.label = label
        self._local = threading.local()
        # Dựng 1 lần ở luồng chính để xác thực + lấy email.
        self.email = account_email(self.service())
        log(f"[{label}] Đăng nhập: {self.email or '(không đọc được email)'}")

    def service(self):
        """Trả về Drive service của luồng hiện tại (tạo mới nếu chưa có)."""
        svc = getattr(self._local, "svc", None)
        if svc is not None:
            return svc
        try:
            creds = Credentials.from_authorized_user_info(self._info, SCOPES)
            if not creds.valid and creds.expired and creds.refresh_token:
                creds.refresh(Request())
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(
                f"[{self.label}] Khởi tạo/refresh token thất bại "
                f"(hãy chạy authorize.py lại): {exc}"
            )
        svc = build("drive", "v3", credentials=creds, cache_discovery=False)
        self._local.svc = svc
        return svc


def account_email(service) -> str:
    try:
        info = service.about().get(fields="user(emailAddress)").execute()
        return str(info.get("user", {}).get("emailAddress", "")).strip()
    except Exception:  # noqa: BLE001
        return ""


# --------------------------------------------------------------------------- #
# Retry helper
# --------------------------------------------------------------------------- #
def with_retry(func, *, what: str):
    delay = 2.0
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return func()
        except HttpError as exc:
            if is_retryable_drive_error(exc) and attempt < MAX_RETRIES:
                log(f"  ! {what}: lỗi tạm thời ({exc}). Thử lại sau {delay:.0f}s "
                    f"({attempt}/{MAX_RETRIES}).")
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            raise


# --------------------------------------------------------------------------- #
# Checkpoint
# --------------------------------------------------------------------------- #
@dataclass
class Checkpoint:
    path: Path
    folders: dict = field(default_factory=dict)  # src_folder_id -> dest_folder_id
    files: dict = field(default_factory=dict)     # src_file_id  -> dest_file_id
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def load(cls, path: Path) -> "Checkpoint":
        cp = cls(path=path)
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                cp.folders = dict(data.get("folders", {}))
                cp.files = dict(data.get("files", {}))
                log(f"Checkpoint: {len(cp.folders)} folder, {len(cp.files)} file đã có.")
            except (OSError, json.JSONDecodeError):
                log("Checkpoint hỏng hoặc trống — bắt đầu mới.")
        return cp

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps({"folders": self.folders, "files": self.files},
                           ensure_ascii=False, indent=0),
                encoding="utf-8",
            )
            tmp.replace(self.path)

    def set_folder(self, src: str, dest: str) -> None:
        with self._lock:
            self.folders[src] = dest

    def set_file(self, src: str, dest: str) -> None:
        with self._lock:
            self.files[src] = dest


# --------------------------------------------------------------------------- #
# Drive helpers
# --------------------------------------------------------------------------- #
def list_children(service, folder_id: str) -> list[dict]:
    items: list[dict] = []
    page_token = None
    query = f"'{drive_query_literal(folder_id)}' in parents and trashed = false"
    while True:
        resp = with_retry(
            lambda: service.files().list(
                q=query,
                spaces="drive",
                fields=LIST_FIELDS,
                pageToken=page_token,
                pageSize=1000,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute(),
            what=f"list {folder_id}",
        )
        items.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return items


def find_dest_folder(service, parent_id: str, name: str) -> str | None:
    safe = drive_query_literal(name)
    query = (
        f"'{drive_query_literal(parent_id)}' in parents and trashed = false and "
        f"mimeType = '{FOLDER_MIME_TYPE}' and name = '{safe}'"
    )
    resp = with_retry(
        lambda: service.files().list(
            q=query, fields="files(id, name)", pageSize=10,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute(),
        what=f"find folder {name}",
    )
    files = resp.get("files", [])
    return files[0]["id"] if files else None


def ensure_dest_folder(service, parent_id: str, name: str, dry_run: bool) -> str:
    if dry_run:
        return f"(dry-run-folder:{name})"
    existing = find_dest_folder(service, parent_id, name)
    if existing:
        return existing
    created = with_retry(
        lambda: service.files().create(
            body={"name": name, "mimeType": FOLDER_MIME_TYPE, "parents": [parent_id]},
            fields="id", supportsAllDrives=True,
        ).execute(),
        what=f"create folder {name}",
    )
    return created["id"]


def dest_file_exists(service, parent_id: str, name: str) -> str | None:
    """Tìm file cùng tên trong folder đích (chống trùng khi mất checkpoint)."""
    safe = drive_query_literal(name)
    query = (
        f"'{drive_query_literal(parent_id)}' in parents and trashed = false and "
        f"name = '{safe}' and mimeType != '{FOLDER_MIME_TYPE}'"
    )
    resp = with_retry(
        lambda: service.files().list(
            q=query, fields="files(id, name)", pageSize=10,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute(),
        what=f"check dup {name}",
    )
    files = resp.get("files", [])
    return files[0]["id"] if files else None


def get_folder_name(src_service, folder_id: str) -> str:
    """Lấy tên folder nguồn (qua A). Lỗi -> thông báo A không truy cập được."""
    try:
        meta = with_retry(
            lambda: src_service.files().get(
                fileId=folder_id, fields="id, name, mimeType",
                supportsAllDrives=True,
            ).execute(),
            what=f"get folder {folder_id}",
        )
    except HttpError as exc:
        raise SystemExit(
            f"[A] Không truy cập được folder nguồn {folder_id}: {exc}. "
            "Kiểm tra acc A (.edu) có quyền xem folder này không."
        )
    if meta.get("mimeType") != FOLDER_MIME_TYPE:
        raise SystemExit(f"[A] {folder_id} không phải folder (mimeType={meta.get('mimeType')}).")
    return str(meta.get("name") or folder_id)


def preflight_dest(dst_service, dest_root: str) -> None:
    """Kiểm tra B truy cập + ghi được vào folder đích trước khi chạy."""
    try:
        meta = with_retry(
            lambda: dst_service.files().get(
                fileId=dest_root,
                fields="id, name, mimeType, driveId, ownedByMe, capabilities(canAddChildren)",
                supportsAllDrives=True,
            ).execute(),
            what=f"preflight dest {dest_root}",
        )
    except HttpError as exc:
        raise SystemExit(
            f"[B] Không truy cập được folder đích {dest_root}: {exc}. "
            "Hãy share folder đích cho acc B với quyền Editor."
        )
    if meta.get("mimeType") != FOLDER_MIME_TYPE:
        raise SystemExit(f"[B] Đích {dest_root} không phải folder.")
    if not meta.get("capabilities", {}).get("canAddChildren", False):
        raise SystemExit(
            f"[B] Acc B không có quyền ghi vào folder đích '{meta.get('name')}'. "
            "Hãy cấp quyền Editor cho acc B."
        )
    where = "Shared Drive (file sẽ thuộc Shared Drive, KHÔNG tính quota B)" if meta.get("driveId") \
        else ("B sở hữu" if meta.get("ownedByMe") else "folder người khác sở hữu, B là Editor "
              "(file B tạo vẫn do B sở hữu, tính quota B)")
    log(f"[B] Đích OK: '{meta.get('name')}' — {where}.")


# --------------------------------------------------------------------------- #
# Transfer de 1 file
# --------------------------------------------------------------------------- #
@dataclass
class FileTask:
    src_id: str
    name: str
    mime_type: str
    size: int
    dest_parent: str
    rel_path: str


def download_file(src_service, task: FileTask, temp_dir: Path) -> tuple[Path, str]:
    """Tải file từ A xuống đĩa tạm. Trả về (đường dẫn, tên file cuối cùng)."""
    export = EXPORT_MAP.get(task.mime_type)
    final_name = task.name
    fd, tmp_name = tempfile.mkstemp(dir=str(temp_dir), suffix=".part")
    os.close(fd)
    tmp_path = Path(tmp_name)

    if export:
        export_mime, suffix = export
        if not final_name.lower().endswith(suffix):
            final_name += suffix
        request = src_service.files().export_media(
            fileId=task.src_id, mimeType=export_mime
        )
    else:
        request = src_service.files().get_media(
            fileId=task.src_id, supportsAllDrives=True
        )

    with tmp_path.open("wb") as handle:
        downloader = MediaIoBaseDownload(handle, request, chunksize=DOWNLOAD_CHUNK)
        done = False
        while not done:
            _, done = with_retry(downloader.next_chunk, what=f"download {task.name}")
    return tmp_path, final_name


def upload_file(dst_service, tmp_path: Path, final_name: str, dest_parent: str) -> str:
    media = MediaFileUpload(str(tmp_path), resumable=True, chunksize=UPLOAD_CHUNK)
    request = dst_service.files().create(
        body={"name": final_name, "parents": [dest_parent]},
        media_body=media,
        fields="id",
        supportsAllDrives=True,
    )
    response = None
    attempt = 0
    while response is None:
        try:
            _, response = request.next_chunk()
        except HttpError as exc:
            attempt += 1
            if is_retryable_drive_error(exc) and attempt < MAX_RETRIES:
                time.sleep(min(2 ** attempt, 60))
                continue
            raise
    return response["id"]


class Stats:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.copied = 0
        self.skipped = 0
        self.failed = 0

    def bump(self, key: str) -> None:
        with self.lock:
            setattr(self, key, getattr(self, key) + 1)


def process_file(task: FileTask, src_client: "DriveClient", dst_client: "DriveClient",
                 cp: Checkpoint, stats: Stats, temp_dir: Path,
                 dry_run: bool, verify_dup: bool) -> None:
    if task.src_id in cp.files:
        stats.bump("skipped")
        return

    if dry_run:
        log(f"  [dry-run] sẽ copy: {task.rel_path}")
        stats.bump("copied")
        return

    # Service riêng cho luồng này (không chia sẻ transport giữa các luồng).
    src_service = src_client.service()
    dst_service = dst_client.service()

    if verify_dup:
        existing = dest_file_exists(dst_service, task.dest_parent, task.name)
        if existing:
            cp.set_file(task.src_id, existing)
            cp.save()
            log(f"  = đã có sẵn, bỏ qua: {task.rel_path}")
            stats.bump("skipped")
            return

    tmp_path = None
    try:
        tmp_path, final_name = download_file(src_service, task, temp_dir)
        dest_id = upload_file(dst_service, tmp_path, final_name, task.dest_parent)
        cp.set_file(task.src_id, dest_id)
        cp.save()
        size_mb = task.size / 1048576 if task.size else 0
        log(f"  ✓ {task.rel_path} ({size_mb:.1f} MB)")
        stats.bump("copied")
    except Exception as exc:  # noqa: BLE001
        log(f"  ✗ LỖI {task.rel_path}: {exc}")
        stats.bump("failed")
    finally:
        if tmp_path and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Walk + điều phối
# --------------------------------------------------------------------------- #
def walk_and_collect(src_service, dst_service, cp: Checkpoint,
                     src_folder: str, dest_folder: str, recursive: bool,
                     dry_run: bool) -> list[FileTask]:
    """Duyệt cây nguồn, tạo folder đích tương ứng, trả về danh sách file cần copy."""
    tasks: list[FileTask] = []
    # (src_folder_id, dest_folder_id, rel_path)
    stack = [(src_folder, dest_folder, "")]
    cp.set_folder(src_folder, dest_folder)

    while stack:
        src_id, dst_id, rel = stack.pop()
        children = list_children(src_service, src_id)
        for item in children:
            name = item.get("name", "")
            mime = item.get("mimeType", "")
            item_id = item["id"]
            child_rel = f"{rel}/{name}" if rel else name

            if mime == SHORTCUT_MIME_TYPE:
                target_id = shortcut_target_id(item)
                if not target_id:
                    continue
                try:
                    target = with_retry(
                        lambda: src_service.files().get(
                            fileId=target_id,
                            fields="id, name, mimeType, size",
                            supportsAllDrives=True,
                        ).execute(),
                        what=f"resolve shortcut {name}",
                    )
                except HttpError:
                    log(f"  (bỏ qua shortcut hỏng: {child_rel})")
                    continue
                item_id = target["id"]
                mime = target.get("mimeType", "")
                name = target.get("name", name)
                item = target

            if mime == FOLDER_MIME_TYPE:
                if not recursive:
                    continue
                mapped = cp.folders.get(item_id)
                if not mapped:
                    mapped = ensure_dest_folder(dst_service, dst_id, name, dry_run)
                    cp.set_folder(item_id, mapped)
                    cp.save()
                stack.append((item_id, mapped, child_rel))
                continue

            try:
                size = int(item.get("size", 0) or 0)
            except (TypeError, ValueError):
                size = 0
            tasks.append(FileTask(
                src_id=item_id, name=name, mime_type=mime, size=size,
                dest_parent=dst_id, rel_path=child_rel,
            ))
    return tasks


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Copy Drive nguồn (token A) sang Drive đích (token B), B làm chủ sở hữu."
    )
    parser.add_argument("--source-token", default="token_A.json",
                        help="Token JSON của acc A (acc xem được nguồn).")
    parser.add_argument("--dest-token", default="token_B.json",
                        help="Token JSON của acc B (kho lưu, sẽ sở hữu file).")
    parser.add_argument("--source-folder-id", required=True,
                        help="ID/URL folder nguồn trên Drive A. Nhiều folder ngăn bằng dấu phẩy; "
                             "mỗi nguồn thành 1 subfolder cùng tên trong folder đích.")
    parser.add_argument("--dest-folder-id", required=True,
                        help="ID hoặc URL folder đích trên Drive B.")
    parser.add_argument("--workers", type=int, default=4,
                        help="Số file tải/upload song song (1..16).")
    parser.add_argument("--checkpoint", default="cross_account_checkpoint.json")
    parser.add_argument("--temp-dir", default="",
                        help="Thư mục chứa file tạm (mặc định: temp hệ thống / RUNNER_TEMP).")
    parser.add_argument("--no-recursive", action="store_true",
                        help="Chỉ copy tầng trên cùng, không vào folder con.")
    parser.add_argument("--no-verify-dup", action="store_true",
                        help="Bỏ bước kiểm tra trùng tên ở đích (nhanh hơn, dựa hoàn toàn vào checkpoint).")
    parser.add_argument("--limit", type=int, default=0,
                        help="Chỉ xử lý tối đa N file (0 = không giới hạn). Dùng để chạy canary.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Chỉ liệt kê, không tải/ghi gì.")
    args = parser.parse_args()

    source_ids = parse_folder_list(args.source_folder_id)
    dest_root = extract_drive_id(args.dest_folder_id)
    workers = max(1, min(args.workers, 16))

    temp_root = args.temp_dir or os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()
    temp_dir = Path(tempfile.mkdtemp(prefix="xacc_", dir=temp_root))

    log("Cross-account Drive sync · A đọc nguồn → đĩa tạm → B upload (B sở hữu)")
    log(f"Nguồn (A): {len(source_ids)} folder  →  Đích (B): {dest_root}")
    log(f"Workers: {workers} · Temp: {temp_dir} · Dry-run: {args.dry_run}")

    src_client = DriveClient(args.source_token, "A/nguồn")
    dst_client = DriveClient(args.dest_token, "B/đích")

    preflight_dest(dst_client.service(), dest_root)

    cp = Checkpoint.load(Path(args.checkpoint))
    stats = Stats()

    tasks: list[FileTask] = []
    for src_id in source_ids:
        name = get_folder_name(src_client.service(), src_id)
        sub_dest = cp.folders.get(src_id) or ensure_dest_folder(
            dst_client.service(), dest_root, name, args.dry_run
        )
        cp.set_folder(src_id, sub_dest)
        cp.save()
        log(f"Nguồn '{name}' ({src_id}) → subfolder đích {sub_dest}")
        tasks.extend(walk_and_collect(
            src_client.service(), dst_client.service(), cp, src_id, sub_dest,
            recursive=not args.no_recursive, dry_run=args.dry_run,
        ))
    pending = [t for t in tasks if t.src_id not in cp.files]
    if args.limit and len(pending) > args.limit:
        log(f"--limit {args.limit}: chỉ xử lý {args.limit}/{len(pending)} file (canary).")
        pending = pending[:args.limit]
    log(f"Tổng {len(tasks)} file, {len(pending)} file cần xử lý "
        f"({len(tasks) - len(pending)} đã có trong checkpoint).")

    verify_dup = not args.no_verify_dup
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(process_file, task, src_client, dst_client, cp,
                        stats, temp_dir, args.dry_run, verify_dup)
            for task in pending
        ]
        for _ in as_completed(futures):
            pass

    cp.save()
    try:
        temp_dir.rmdir()
    except OSError:
        pass

    log("")
    log(f"Hoàn tất: {stats.copied} copy · {stats.skipped} bỏ qua · {stats.failed} lỗi.")
    return 1 if stats.failed else 0


# ID folder là chuỗi ký tự id sau các marker URL quen thuộc, hoặc id dán trực tiếp.
_ID_PATTERNS = (
    re.compile(r"/folders/([A-Za-z0-9_-]+)"),
    re.compile(r"/file/d/([A-Za-z0-9_-]+)"),
    re.compile(r"[?&]id=([A-Za-z0-9_-]+)"),
)


def extract_drive_id(value: str) -> str:
    text = str(value).strip()
    if not text:
        raise SystemExit("Thiếu folder id.")
    for pattern in _ID_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    if "/" in text or "://" in text:
        raise SystemExit(f"Không tách được Drive id từ: {value!r}")
    return text


def parse_folder_list(raw: str) -> list[str]:
    """Tách danh sách URL/id ngăn bằng dấu phẩy thành các folder id (bỏ trùng)."""
    ids: list[str] = []
    seen: set[str] = set()
    for chunk in str(raw).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        folder_id = extract_drive_id(chunk)
        if folder_id not in seen:
            seen.add(folder_id)
            ids.append(folder_id)
    if not ids:
        raise SystemExit("Không có folder nguồn nào trong --source-folder-id.")
    return ids


if __name__ == "__main__":
    raise SystemExit(main())
