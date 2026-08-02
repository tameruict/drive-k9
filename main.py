"""
DRIVE COPY AUTOMATION — GitHub Actions Version
================================================
Copy Google Drive -> Google Drive, detect blocked files, export log.

Auth: OAuth2 via DRIVE_TOKEN (chứa JSON giống token.json)
      (lưu trong GitHub Secrets)

Luồng xử lý mỗi file:
  - THỬ COPY:
      -> Thành công                 => report "COPY - OK"
      -> Đã tồn tại ở đích          => report "SKIP - ĐÃ CÓ"
      -> Lỗi 403/cannotCopy (BLOCK) => kiểm tra đích
            -> Chưa có ở đích       => report "BLOCKED - THIẾU Ở DEST" ← lọc ra log
            -> Đã có ở đích         => report "BLOCKED - ĐÃ CÓ (bỏ qua)"
      -> Lỗi khác                   => report "ERROR"

Output:
  - Console log chi tiết
  - drive_copy_log.json  — toàn bộ kết quả
  - blocked_missing.json — chỉ file BLOCKED chưa có ở đích (upload lên artifact)
  - Bao_Cao_Copy_Drive.xlsx — báo cáo Excel đầy đủ
"""

import os
import sys
import time
import json
import threading
import traceback
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

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

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ===================================================================
# CONFIG — chỉnh tại đây hoặc override bằng env var
# ===================================================================
SOURCE_FOLDER_ID = os.environ.get("SOURCE_FOLDER_ID", "107CZgk5F7iRX_b9Esa1avdOEkwTIMKIh")
DEST_FOLDER_ID   = os.environ.get("DEST_FOLDER_ID",   "1jzw1q9Chb9CQRdWXgu0qMP3jmMgmq8As")

MAX_WORKERS      = int(os.environ.get("MAX_WORKERS", "6"))
FILE_WORKERS     = int(os.environ.get("FILE_WORKERS", str(max(1, MAX_WORKERS * 2))))
MAX_DEPTH        = int(os.environ.get("MAX_DEPTH", "15"))
RETRY_TIMES      = 3
RETRY_DELAY      = 3   # giây

LOG_FILE             = "drive_copy_log.json"
BLOCKED_MISSING_FILE = "blocked_missing.json"
EXCEL_OUTPUT         = "Bao_Cao_Copy_Drive.xlsx"
CHECKPOINT_FILE      = "sync_checkpoint.json"
SYNC_STATE_FILE      = os.environ.get("SYNC_STATE_FILE", "sync_state.json")
SMART_SCAN_ENABLED   = os.environ.get("SMART_SCAN_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
FORCE_FULL_SCAN      = os.environ.get("FORCE_FULL_SCAN", "0").strip().lower() in {"1", "true", "yes", "on"}
SKIPPED_FILE_MIME_TYPES = {
    "application/vnd.google-apps.spreadsheet": "Google Sheet",
}

# ===================================================================
# AUTH — đọc từ GitHub Secrets (env vars)
# ===================================================================

def build_credentials() -> Credentials:
    """Tạo Credentials từ biến môi trường DRIVE_TOKEN (chứa JSON giống token.json)."""
    drive_token = os.environ.get("DRIVE_TOKEN")
    if not drive_token:
        raise ValueError("Không tìm thấy biến môi trường DRIVE_TOKEN.")

    info = json.loads(drive_token)
    creds = Credentials.from_authorized_user_info(info)

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())

    return creds


def build_service(creds: Credentials):
    return build("drive", "v3", credentials=creds, cache_discovery=False)


# ===================================================================
# DRIVE HELPERS
# ===================================================================

def skipped_file_reason(file_info: dict) -> str | None:
    return skipped_file_reason_for_mimes(file_info, SKIPPED_FILE_MIME_TYPES)


def list_children(service, folder_id: str) -> list:
    """Liệt kê toàn bộ children trong folder (có phân trang)."""
    items, page_token = [], None
    while True:
        try:
            resp = service.files().list(
                q=f"'{folder_id}' in parents and trashed = false",
                pageSize=1000,
                fields=(
                    "files(id, name, mimeType, size, modifiedTime, shortcutDetails), "
                    "nextPageToken"
                ),
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
            print(f"  [WARN] list_children({folder_id}): {e}")
            break
    return items




def find_in_parent(service, parent_id: str, name: str, mime_type: str | None = None) -> str | None:
    """Trả về file/folder id nếu đã tồn tại trong parent, ngược lại None."""
    try:
        safe_parent = drive_query_literal(parent_id)
        safe_name = drive_query_literal(name)
        query_parts = [
            f"'{safe_parent}' in parents",
            f"name = '{safe_name}'",
            "trashed = false",
        ]
        if mime_type:
            safe_mime = drive_query_literal(mime_type)
            query_parts.append(f"mimeType = '{safe_mime}'")
        resp = service.files().list(
            q=" and ".join(query_parts),
            pageSize=10,
            fields="files(id, name, mimeType, parents)",
            corpora="allDrives",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        items = resp.get("files", [])

        match = find_matching_item(items, name, mime_type)
        if match:
            return match["id"]

        query_parts = [
            f"'{safe_parent}' in parents",
            "trashed = false",
        ]
        if mime_type:
            safe_mime = drive_query_literal(mime_type)
            query_parts.append(f"mimeType = '{safe_mime}'")

        items, page_token = [], None
        while True:
            resp = service.files().list(
                q=" and ".join(query_parts),
                pageSize=1000,
                fields="files(id, name, mimeType, parents), nextPageToken",
                pageToken=page_token,
                corpora="allDrives",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            items.extend(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        match = find_matching_item(items, name, mime_type)
        return match["id"] if match else None
    except HttpError:
        return None


def find_folder_in_parent(service, parent_id: str, name: str) -> str | None:
    return find_in_parent(service, parent_id, name, FOLDER_MIME_TYPE)


def get_item_metadata(service, file_id: str) -> dict | None:
    try:
        return service.files().get(
            fileId=file_id,
            fields="id, name, mimeType, size, modifiedTime, parents, trashed, shortcutDetails",
            supportsAllDrives=True,
        ).execute()
    except HttpError:
        return None


def resolve_shortcut_item(service, item: dict) -> tuple[dict | None, str | None]:
    """Legacy compatibility hook; Drive shortcuts are intentionally skipped."""
    if not is_shortcut(item):
        return item, None
    return None, skipped_shortcut_reason(item)


def create_folder(service, parent_id: str, name: str) -> str | None:
    """Create a destination folder without doing another existence lookup."""
    try:
        body = {
            "name": name,
            "mimeType": FOLDER_MIME_TYPE,
            "parents": [parent_id],
        }
        folder = service.files().create(
            body=body, fields="id", supportsAllDrives=True
        ).execute()
        return folder["id"]
    except HttpError as e:
        print(f"  [ERROR] ensure_folder({name}): {e}")
        return None


def ensure_folder(service, parent_id: str, name: str) -> str | None:
    """Tạo folder nếu chưa có; trả về id."""
    existing = find_folder_in_parent(service, parent_id, name)
    if existing:
        return existing
    return create_folder(service, parent_id, name)


def is_blocked_error(e: HttpError) -> bool:
    """Phát hiện file bị Drive chặn download/copy."""
    return is_blocked_drive_error(e)


def is_retryable_error(e: HttpError) -> bool:
    return is_retryable_drive_error(e)


# ===================================================================
# CORE COPY LOGIC
# ===================================================================

def load_checkpoint() -> dict:
    if os.path.exists(CHECKPOINT_FILE):
        try:
            with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"  [WARN] Lỗi đọc checkpoint: {e}")
    return {}

def save_checkpoint(data: dict):
    try:
        with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        print(f"  [ERROR] Lỗi lưu checkpoint: {e}")


class SyncState:
    VERSION = 2
    BAD_STATUSES = {"ERROR", "BLOCKED_MISSING"}

    def __init__(
        self,
        filepath: str,
        source_root_id: str,
        dest_root_id: str,
        *,
        enabled: bool = True,
        force_full_scan: bool = False,
    ):
        self.filepath = filepath
        self.source_root_id = source_root_id
        self.dest_root_id = dest_root_id
        self.enabled = enabled
        self.force_full_scan = force_full_scan
        self.incremental_ready = False
        self.next_change_token: str | None = None
        self.changed_count = 0
        self.related_changed_count = 0
        self.dirty_folder_ids: set[str] = set()
        self.dirty_subtree_ids: set[str] = set()
        self.scanned_folders: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.data = self._empty_state()
        self._load()

    def _empty_state(self) -> dict:
        return {
            "version": self.VERSION,
            "source_root_id": self.source_root_id,
            "dest_root_id": self.dest_root_id,
            "change_token": None,
            "folders": {},
            "item_parents": {},
            "item_types": {},
            "shortcut_targets": {},
        }

    def _load(self):
        if not self.enabled or not os.path.exists(self.filepath):
            return
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"  [WARN] Loi doc sync state, se quet day du: {e}")
            return

        if (
            data.get("version") != self.VERSION
            or data.get("source_root_id") != self.source_root_id
            or data.get("dest_root_id") != self.dest_root_id
        ):
            print("  [SMART] sync_state khong khop source/dest hien tai, se tao lai.")
            return
        self.data = data

    def prepare(self, service):
        if not self.enabled:
            print("  [SMART] Smart scan dang tat.")
            return
        if self.force_full_scan:
            print("  [SMART] FORCE_FULL_SCAN=1, quet day du lan nay.")
            return

        token = self.data.get("change_token")
        if not token:
            print("  [SMART] Chua co sync_state/change token, quet day du lan dau.")
            return

        try:
            changes, next_token = self._list_changes(service, token)
        except Exception as e:
            print(f"  [WARN] Khong doc duoc Drive changes, fallback quet day du: {e}")
            return

        self.incremental_ready = True
        self.next_change_token = next_token
        self.changed_count = len(changes)
        self._mark_dirty_from_changes(changes)
        if self.related_changed_count:
            print(
                "  [SMART] Drive changes: "
                f"{self.related_changed_count}/{self.changed_count} thay doi lien quan; "
                f"quet lai {len(self.dirty_subtree_ids)} folder/ancestor."
            )
        else:
            print(f"  [SMART] Khong co thay doi lien quan ({self.changed_count} changes).")

    def _list_changes(self, service, token: str) -> tuple[list[dict], str | None]:
        changes: list[dict] = []
        page_token: str | None = token
        next_token: str | None = None
        while page_token:
            resp = service.changes().list(
                pageToken=page_token,
                pageSize=1000,
                fields=(
                    "changes(fileId,removed,file(id,name,mimeType,parents,trashed,modifiedTime)),"
                    "nextPageToken,newStartPageToken"
                ),
                spaces="drive",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            changes.extend(resp.get("changes", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                next_token = resp.get("newStartPageToken")
        return changes, next_token

    def _mark_dirty_from_changes(self, changes: list[dict]):
        folders = self.data.setdefault("folders", {})
        item_parents = self.data.setdefault("item_parents", {})
        tracked_folders = set(folders)
        tracked_folders.add(self.source_root_id)

        for change in changes:
            file_meta = change.get("file") or {}
            file_id = change.get("fileId") or file_meta.get("id")
            if not file_id:
                continue

            parents = file_meta.get("parents") or item_parents.get(file_id, [])
            shortcut_targets = self.data.setdefault("shortcut_targets", {})
            shortcut_aliases = shortcut_targets.get(file_id, [])
            parent_aliases = [
                alias
                for parent_id in parents
                for alias in shortcut_targets.get(parent_id, [])
            ]
            related = (
                file_id in tracked_folders
                or file_id in item_parents
                or bool(shortcut_aliases)
                or bool(parent_aliases)
                or any(parent_id in tracked_folders for parent_id in parents)
            )
            if not related:
                continue

            self.related_changed_count += 1
            if file_meta:
                self._remember_source_item(file_meta, parents)

            if file_meta.get("mimeType") == FOLDER_MIME_TYPE or file_id in tracked_folders:
                self.dirty_folder_ids.add(file_id)
            self.dirty_folder_ids.update(shortcut_aliases)
            self.dirty_folder_ids.update(parent_aliases)
            for parent_id in parents:
                self.dirty_folder_ids.add(parent_id)

        self.dirty_subtree_ids = self._expand_to_ancestors(self.dirty_folder_ids)

    def _expand_to_ancestors(self, folder_ids: set[str]) -> set[str]:
        item_parents = self.data.setdefault("item_parents", {})
        expanded: set[str] = set()
        stack = list(folder_ids)
        while stack:
            folder_id = stack.pop()
            if folder_id in expanded:
                continue
            expanded.add(folder_id)
            stack.extend(item_parents.get(folder_id, []))
        return expanded

    def _remember_source_item(self, item: dict, parents: list[str] | None = None):
        if is_shortcut(item):
            return
        item_id = item.get("id")
        if not item_id:
            return
        if parents is not None:
            self.data.setdefault("item_parents", {})[item_id] = list(parents)
        self.data.setdefault("item_types", {})[item_id] = item.get("mimeType", "")

    def root_complete(self) -> bool:
        root = self.data.get("folders", {}).get(self.source_root_id)
        return bool(root and root.get("complete"))

    def should_skip_everything(self) -> bool:
        return (
            self.enabled
            and self.incremental_ready
            and self.root_complete()
            and not self.dirty_subtree_ids
        )

    def can_skip_folder(self, folder_id: str) -> bool:
        folder = self.data.get("folders", {}).get(folder_id)
        return (
            self.enabled
            and self.incremental_ready
            and bool(folder and folder.get("complete"))
            and folder_id not in self.dirty_subtree_ids
        )

    def record_scanned_folder(
        self,
        folder_id: str,
        dest_id: str,
        path: str,
        children: list[dict],
        real_folder_id: str | None = None,
    ):
        if not self.enabled:
            return
        with self.lock:
            file_count = 0
            folder_count = 0
            for child in children:
                if is_shortcut(child):
                    continue
                child_id = child.get("id")
                if not child_id:
                    continue
                self._remember_source_item(child, [folder_id])
                if child.get("mimeType") == FOLDER_MIME_TYPE:
                    folder_count += 1
                else:
                    file_count += 1

            self.data.setdefault("item_types", {})[folder_id] = FOLDER_MIME_TYPE
            if real_folder_id and real_folder_id != folder_id:
                aliases = self.data.setdefault("shortcut_targets", {}).setdefault(real_folder_id, [])
                if folder_id not in aliases:
                    aliases.append(folder_id)
            self.scanned_folders[folder_id] = {
                "dest_id": dest_id,
                "path": path,
                "real_folder_id": real_folder_id or folder_id,
                "file_count": file_count,
                "folder_count": folder_count,
                "child_count": file_count + folder_count,
            }

    def commit(self, results: list[dict], service=None):
        if not self.enabled:
            return

        bad_paths = [
            r.get("path", "")
            for r in results
            if r.get("status") in self.BAD_STATUSES and r.get("path")
        ]
        now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        folders = self.data.setdefault("folders", {})

        for folder_id, info in self.scanned_folders.items():
            path = info["path"]
            is_root = folder_id == self.source_root_id
            complete = not self._path_has_bad_result(path, bad_paths, is_root)
            folders[folder_id] = {
                "dest_id": info["dest_id"],
                "path": path,
                "complete": complete,
                "updated_at": now,
                "file_count": info["file_count"],
                "folder_count": info["folder_count"],
                "child_count": info["child_count"],
            }

        if self.next_change_token:
            self.data["change_token"] = self.next_change_token
        elif not self.data.get("change_token") and service is not None:
            token = self._get_start_page_token(service)
            if token:
                self.data["change_token"] = token

        self._save()

    @staticmethod
    def _path_has_bad_result(path: str, bad_paths: list[str], is_root: bool) -> bool:
        if is_root:
            return bool(bad_paths)
        prefix = f"{path}/"
        return any(bad == path or bad.startswith(prefix) for bad in bad_paths)

    @staticmethod
    def _get_start_page_token(service) -> str | None:
        try:
            resp = service.changes().getStartPageToken(
                supportsAllDrives=True
            ).execute()
            return resp.get("startPageToken")
        except Exception as e:
            print(f"  [WARN] Khong lay duoc Drive startPageToken: {e}")
            return None

    def _save(self):
        directory = os.path.dirname(os.path.abspath(self.filepath))
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp_path = f"{self.filepath}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.filepath)


class CopyEngine:
    def __init__(self, creds: Credentials, sync_state: SyncState | None = None):
        self.creds = creds
        self.sync_state = sync_state
        self.lock = threading.Lock()
        self.folder_lock = threading.Lock()
        self.parent_index_lock = threading.Lock()
        self.folder_cache: dict[tuple[str, str], str] = {}
        self.dest_parent_items: dict[str, list[dict]] = {}
        self.dest_parent_index: dict[str, dict[tuple[str, str], dict]] = {}
        self.parent_load_locks: dict[str, threading.Lock] = {}
        self.thread_local = threading.local()
        self.file_pool = ThreadPoolExecutor(max_workers=FILE_WORKERS)
        self.file_futures = []
        self.file_futures_lock = threading.Lock()
        self.results: list[dict] = []   # thread-safe accumulator
        self.total_bytes_copied = 0
        self.checkpoint = load_checkpoint()
        self.save_counter = 0

    def _save_checkpoint_throttled(self):
        self.save_counter += 1
        if self.save_counter % 100 == 0:
            save_checkpoint(self.checkpoint)

    def _svc(self):
        """Mỗi worker tự tạo service riêng để tránh race condition."""
        return build_service(self.creds)

    def _thread_service(self):
        service = getattr(self.thread_local, "service", None)
        if service is None:
            service = build_service(self.creds)
            self.thread_local.service = service
        return service

    def _record(self, entry: dict):
        with self.lock:
            self.results.append(entry)

    def _parent_load_lock(self, parent_id: str) -> threading.Lock:
        with self.parent_index_lock:
            lock = self.parent_load_locks.get(parent_id)
            if lock is None:
                lock = threading.Lock()
                self.parent_load_locks[parent_id] = lock
            return lock

    @staticmethod
    def _lookup_key(name: str, mime_type: str | None = None) -> tuple[str, str]:
        return (normalize_drive_name(name), mime_type or "")

    def _rebuild_parent_index_locked(self, parent_id: str, items: list[dict]):
        index: dict[tuple[str, str], dict] = {}
        for item in items:
            name = item.get("name", "")
            mime_type = item.get("mimeType", "")
            index.setdefault(self._lookup_key(name, mime_type), item)
            index.setdefault(self._lookup_key(name), item)
        self.dest_parent_index[parent_id] = index

    def _index_dest_item_locked(self, parent_id: str, item: dict):
        index = self.dest_parent_index.setdefault(parent_id, {})
        name = item.get("name", "")
        mime_type = item.get("mimeType", "")
        index.setdefault(self._lookup_key(name, mime_type), item)
        index.setdefault(self._lookup_key(name), item)

    def _get_dest_parent_items(self, service, parent_id: str, force_refresh: bool = False) -> list[dict]:
        if not force_refresh:
            with self.parent_index_lock:
                cached = self.dest_parent_items.get(parent_id)
                if cached is not None:
                    if parent_id not in self.dest_parent_index:
                        self._rebuild_parent_index_locked(parent_id, cached)
                    return cached

        load_lock = self._parent_load_lock(parent_id)
        with load_lock:
            if not force_refresh:
                with self.parent_index_lock:
                    cached = self.dest_parent_items.get(parent_id)
                    if cached is not None:
                        if parent_id not in self.dest_parent_index:
                            self._rebuild_parent_index_locked(parent_id, cached)
                        return cached

            items = list_children(service, parent_id)
            with self.parent_index_lock:
                if force_refresh or parent_id not in self.dest_parent_items:
                    self.dest_parent_items[parent_id] = items
                    self._rebuild_parent_index_locked(parent_id, items)
                return self.dest_parent_items[parent_id]

    def _remember_dest_item(self, parent_id: str, item: dict):
        item_id = item.get("id")
        if not item_id:
            return

        with self.parent_index_lock:
            cached = self.dest_parent_items.get(parent_id)
            if cached is None:
                return
            if not any(existing.get("id") == item_id for existing in cached):
                cached.append(item)
            self._index_dest_item_locked(parent_id, item)

    def _find_dest_item_id(
        self,
        service,
        parent_id: str,
        name: str,
        mime_type: str | None = None,
        force_refresh: bool = False,
    ) -> str | None:
        items = self._get_dest_parent_items(service, parent_id, force_refresh=force_refresh)
        with self.parent_index_lock:
            index = self.dest_parent_index.get(parent_id, {})
            match = index.get(self._lookup_key(name, mime_type))
            if match:
                return match.get("id")

        match = find_matching_item(items, name, mime_type)
        if match:
            with self.parent_index_lock:
                self._index_dest_item_locked(parent_id, match)
        return match.get("id") if match else None

    def _print_file_result(self, result: dict):
        status_icon = {
            "COPY":            "✅",
            "SKIP":            "⏭️ ",
            "BLOCKED_MISSING": "🚫",
            "BLOCKED_EXISTS":  "⚠️ ",
            "ERROR":           "❌",
        }.get(result["status"], "❓")
        print(f"    {status_icon} [{result['status']}] {result['path']}")

    def _copy_file_worker(self, src_file: dict, dest_parent_id: str, path: str) -> dict:
        service = self._thread_service()
        result = self.copy_file(service, src_file, dest_parent_id, path)
        self._record(result)
        self._print_file_result(result)
        return result

    def submit_file_copy(self, src_file: dict, dest_parent_id: str, path: str):
        future = self.file_pool.submit(self._copy_file_worker, src_file, dest_parent_id, path)
        with self.file_futures_lock:
            self.file_futures.append(future)

    def wait_for_file_copies(self):
        with self.file_futures_lock:
            futures = list(self.file_futures)
            self.file_futures.clear()

        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                print(f"  [ERROR] File worker failed: {e}")
                traceback.print_exc()

    def shutdown(self):
        self.file_pool.shutdown(wait=True)

    def ensure_dest_folder(self, service, src_folder_id: str, dest_parent_id: str, name: str) -> str | None:
        cache_key = (dest_parent_id, normalize_drive_name(name))
        with self.folder_lock:
            cached_id = self.folder_cache.get(cache_key)
            if cached_id:
                with self.lock:
                    self.checkpoint[src_folder_id] = cached_id
                    self._save_checkpoint_throttled()
                return cached_id

            existing_id = self._find_dest_item_id(service, dest_parent_id, name, FOLDER_MIME_TYPE)
            if existing_id:
                self.folder_cache[cache_key] = existing_id
                with self.lock:
                    self.checkpoint[src_folder_id] = existing_id
                    self._save_checkpoint_throttled()
                return existing_id

            with self.lock:
                checkpoint_id = self.checkpoint.get(src_folder_id)
            if checkpoint_id:
                meta = get_item_metadata(service, checkpoint_id)
                if (
                    meta
                    and not meta.get("trashed")
                    and same_drive_name(meta.get("name", ""), name, loose_folder=True)
                    and meta.get("mimeType") == FOLDER_MIME_TYPE
                    and dest_parent_id in meta.get("parents", [])
                ):
                    self.folder_cache[cache_key] = checkpoint_id
                    self._remember_dest_item(dest_parent_id, meta)
                    return checkpoint_id

            created_id = create_folder(service, dest_parent_id, name)
            if created_id:
                self.folder_cache[cache_key] = created_id
                self._remember_dest_item(
                    dest_parent_id,
                    {"id": created_id, "name": name, "mimeType": FOLDER_MIME_TYPE},
                )
                with self.lock:
                    self.checkpoint[src_folder_id] = created_id
                    self._save_checkpoint_throttled()
            return created_id

    # ------------------------------------------------------------------
    def copy_file(self, service, src_file: dict, dest_parent_id: str,
                  path: str) -> dict:
        """
        Copy một file đơn. Trả về dict kết quả.
        """
        name      = src_file["name"]
        file_id   = src_file["id"]
        copy_file_id = src_file.get("_copy_file_id", file_id)
        checkpoint_key = src_file.get("_checkpoint_key", file_id)
        size_bytes = int(src_file.get("size", 0))
        full_path  = f"{path}/{name}"

        result = {
            "path":     full_path,
            "name":     name,
            "id":       file_id,
            "target_id": src_file.get("_target_id", ""),
            "target_name": src_file.get("_target_name", ""),
            "source_type": src_file.get("_source_type", "file"),
            "size_mb":  round(size_bytes / 1_048_576, 2),
            "modified": src_file.get("modifiedTime", ""),
            "status":   "",
            "note":     "",
        }

        skip_reason = skipped_file_reason(src_file)
        if skip_reason:
            result["status"] = "SKIP"
            result["note"]   = skip_reason
            return result

        if checkpoint_key in self.checkpoint:
            result["status"] = "SKIP"
            result["note"]   = "Đã copy (từ checkpoint)"
            return result

        # Kiểm tra đã tồn tại ở đích chưa
        existing_id = self._find_dest_item_id(
            service,
            dest_parent_id,
            name,
            src_file.get("mimeType"),
        )

        for attempt in range(1, RETRY_TIMES + 1):
            try:
                if existing_id:
                    with self.lock:
                        self.checkpoint[checkpoint_key] = existing_id
                        self._save_checkpoint_throttled()
                    result["status"] = "SKIP"
                    result["note"]   = "Đã tồn tại ở đích"
                    return result

                copied_file = service.files().copy(
                    fileId=copy_file_id,
                    body={"parents": [dest_parent_id], "name": name},
                    fields="id, name, mimeType",
                    supportsAllDrives=True,
                ).execute()

                result["status"] = "COPY"
                result["note"]   = "Đã copy thành công"
                self._remember_dest_item(
                    dest_parent_id,
                    {
                        "id": copied_file.get("id"),
                        "name": copied_file.get("name", name),
                        "mimeType": copied_file.get("mimeType", src_file.get("mimeType")),
                    },
                )
                with self.lock:
                    self.total_bytes_copied += size_bytes
                    self.checkpoint[checkpoint_key] = copied_file.get("id", "DONE")
                    self._save_checkpoint_throttled()
                return result

            except HttpError as e:
                if is_retryable_error(e):
                    if attempt < RETRY_TIMES:
                        time.sleep(RETRY_DELAY * attempt)
                        continue
                    result["status"] = "ERROR"
                    result["note"]   = str(e)
                    return result

                if is_blocked_error(e):
                    # Kiểm tra lại thực tế ở đích xem đã có chưa (vì existing_id có thể đang là None)
                    actual_existing_id = self._find_dest_item_id(
                        service,
                        dest_parent_id,
                        name,
                        src_file.get("mimeType"),
                        force_refresh=True,
                    )
                    if actual_existing_id:
                        result["status"] = "BLOCKED_EXISTS"
                        result["note"]   = "Bị block copy nhưng file thực tế đã tồn tại ở đích (bỏ qua)"
                        with self.lock:
                            self.checkpoint[checkpoint_key] = actual_existing_id
                            self._save_checkpoint_throttled()
                    else:
                        result["status"] = "BLOCKED_MISSING"
                        result["note"]   = f"Bị Drive chặn & CHƯA CÓ ở đích: {e}"
                    return result

                if attempt < RETRY_TIMES:
                    time.sleep(RETRY_DELAY * attempt)
                else:
                    result["status"] = "ERROR"
                    result["note"]   = str(e)
                    return result

            except Exception as e:
                result["status"] = "ERROR"
                result["note"]   = str(e)
                return result

        return result   # fallback

    # ------------------------------------------------------------------
    def _record_item_error(self, item: dict, path: str, note: str):
        self._record({
            "path": f"{path}/{item.get('name', '')}",
            "name": item.get("name", ""),
            "id": item.get("id", ""),
            "target_id": shortcut_target_id(item) or "",
            "target_name": "",
            "source_type": "shortcut" if is_shortcut(item) else "file",
            "size_mb": 0,
            "modified": item.get("modifiedTime", ""),
            "status": "ERROR",
            "note": note,
        })

    def _record_item_skip(self, item: dict, path: str, note: str):
        self._record({
            "path": f"{path}/{item.get('name', '')}",
            "name": item.get("name", ""),
            "id": item.get("id", ""),
            "target_id": shortcut_target_id(item) or "",
            "target_name": "",
            "source_type": "shortcut" if is_shortcut(item) else "file",
            "size_mb": 0,
            "modified": item.get("modifiedTime", ""),
            "status": "SKIP",
            "note": note,
        })

    def process_tree_item(
        self,
        service,
        item: dict,
        dest_folder_id: str,
        path: str,
        depth: int,
    ):
        if is_shortcut(item):
            self._record_item_skip(item, path, skipped_shortcut_reason(item))
            return

        resolved, error = resolve_shortcut_item(service, item)
        if error or not resolved:
            self._record_item_error(item, path, error or "Khong resolve duoc shortcut")
            return

        name = resolved["name"]
        if resolved["mimeType"] == FOLDER_MIME_TYPE:
            folder_id = resolved.get("_list_folder_id", resolved["id"])
            state_folder_id = resolved.get("_checkpoint_key", folder_id)
            sub_dest_id = self.ensure_dest_folder(
                service, state_folder_id, dest_folder_id, name
            )
            if sub_dest_id:
                self.recursive_copy(
                    service,
                    folder_id,
                    sub_dest_id,
                    f"{path}/{name}",
                    depth + 1,
                    state_folder_id=state_folder_id,
                )
            else:
                self._record({
                    "path": f"{path}/{name}",
                    "name": name,
                    "id": resolved["id"],
                    "target_id": resolved.get("_target_id", ""),
                    "target_name": resolved.get("_target_name", ""),
                    "source_type": resolved.get("_source_type", "folder"),
                    "size_mb": 0,
                    "modified": "",
                    "status": "ERROR",
                    "note": "Khong tao duoc folder dich",
                })
        else:
            self.submit_file_copy(resolved, dest_folder_id, path)

    def recursive_copy(self, service, src_folder_id: str,
                       dest_folder_id: str, path: str, depth: int = 0,
                       state_folder_id: str | None = None):
        """
        Đệ quy copy toàn bộ cây folder.
        Chạy TUẦN TỰ trong từng worker (tránh nested thread pool).
        """
        if depth > MAX_DEPTH:
            print(f"  [WARN] Vượt MAX_DEPTH tại: {path}")
            return

        state_folder_id = state_folder_id or src_folder_id
        if self.sync_state and self.sync_state.can_skip_folder(state_folder_id):
            print(f"  [SMART SKIP] {path} khong co thay doi, bo qua ca nhanh.")
            return

        children = list_children(service, src_folder_id)
        if self.sync_state:
            self.sync_state.record_scanned_folder(
                state_folder_id, dest_folder_id, path, children,
                real_folder_id=src_folder_id,
            )
        print(f"  [{path}] {len(children)} items (depth={depth})")

        for item in children:
            self.process_tree_item(service, item, dest_folder_id, path, depth)
        return

        for item in children:
            name = item["name"]
            if item["mimeType"] == FOLDER_MIME_TYPE:
                # Tạo folder đích rồi đi sâu hơn
                folder_id = item["id"]
                sub_dest_id = self.ensure_dest_folder(service, folder_id, dest_folder_id, name)

                if sub_dest_id:
                    self.recursive_copy(
                        service, folder_id, sub_dest_id,
                        f"{path}/{name}", depth + 1
                    )
                else:
                    self._record({
                        "path": f"{path}/{name}", "name": name,
                        "id": folder_id, "size_mb": 0, "modified": "",
                        "status": "ERROR", "note": "Không tạo được folder đích",
                    })
            else:
                self.submit_file_copy(item, dest_folder_id, path)

    # ------------------------------------------------------------------
    def run_pair(self, src_folder_id: str, dest_folder_id: str,
                 pair_label: str):
        """Entry-point cho mỗi worker thread."""
        service = self._svc()
        try:
            # Lấy tên folder nguồn
            info = service.files().get(
                fileId=src_folder_id,
                fields="name",
                supportsAllDrives=True,
            ).execute()
            name = info.get("name", src_folder_id)
            print(f"\n🚀 [{pair_label}] Bắt đầu: {name}")

            # Tạo/lấy folder tương ứng ở đích
            if self.sync_state and self.sync_state.can_skip_folder(src_folder_id):
                print(f"  [SMART SKIP] {name} khong co thay doi, bo qua ca nhanh.")
                return

            dest_sub_id = self.ensure_dest_folder(service, src_folder_id, dest_folder_id, name)

            if not dest_sub_id:
                print(f"  ❌ Không tạo được folder đích cho: {name}")
                return

            self.recursive_copy(service, src_folder_id, dest_sub_id, name)
            print(f"  ✅ [{pair_label}] Đã duyệt xong: {name}")

        except Exception as e:
            print(f"  ❌ [{pair_label}] Lỗi: {e}")
            traceback.print_exc()


# ===================================================================
# EXPORT
# ===================================================================

def export_json(data: list, filepath: str):
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"📄 JSON đã lưu: {filepath}  ({len(data)} records)")


def export_excel(data: list, filepath: str):
    try:
        import pandas as pd

        df = pd.DataFrame(data, columns=[
            "path", "name", "id", "size_mb", "modified", "status", "note"
        ])
        df.rename(columns={
            "path":     "Đường dẫn",
            "name":     "Tên file",
            "id":       "Drive ID",
            "size_mb":  "Dung lượng (MB)",
            "modified": "Ngày sửa",
            "status":   "Trạng thái",
            "note":     "Ghi chú",
        }, inplace=True)

        with pd.ExcelWriter(filepath, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Tất cả")

            # Sheet riêng: file BLOCKED chưa có ở đích
            blocked = df[df["Trạng thái"] == "BLOCKED_MISSING"]
            if not blocked.empty:
                blocked.to_excel(writer, index=False, sheet_name="BLOCKED_THIẾU_Ở_ĐÍCH")

        print(f"📊 Excel đã lưu: {filepath}")
    except ImportError:
        print("[WARN] pandas/openpyxl chưa cài. Bỏ qua export Excel.")


def print_summary(results: list, elapsed: float, total_bytes: int):
    from collections import Counter
    counts = Counter(r["status"] for r in results)

    print("\n" + "=" * 60)
    print("📋 TỔNG KẾT")
    print("=" * 60)
    print(f"  ✅ COPY (thành công)          : {counts.get('COPY', 0)}")
    print(f"  ⏭️  SKIP (bỏ qua/đã tồn tại)   : {counts.get('SKIP', 0)}")
    print(f"  🚫 BLOCKED_MISSING (chưa có)  : {counts.get('BLOCKED_MISSING', 0)}")
    print(f"  ⚠️  BLOCKED_EXISTS (đã có)     : {counts.get('BLOCKED_EXISTS', 0)}")
    print(f"  ❌ ERROR                       : {counts.get('ERROR', 0)}")
    print(f"  📦 Tổng dung lượng đã copy    : {total_bytes/1_073_741_824:.3f} GB")
    print(f"  ⏱️  Thời gian chạy             : {elapsed:.1f}s")
    print("=" * 60)


# ===================================================================
# MAIN
# ===================================================================

def main():
    print("=" * 60)
    print("  DRIVE COPY AUTOMATION — GitHub Actions")
    print(f"  Source : {SOURCE_FOLDER_ID}")
    print(f"  Dest   : {DEST_FOLDER_ID}")
    print(f"  Workers: {MAX_WORKERS}")
    print(f"  File workers: {FILE_WORKERS}")
    print(f"  Bắt đầu: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # --- Auth ---
    try:
        creds = build_credentials()
        print("✅ Xác thực Google Drive thành công.")
    except Exception as e:
        print(f"❌ Lỗi xác thực: {e}")
        sys.exit(1)

    # --- Liệt kê các folder con trực tiếp của SOURCE để phân chia job ---
    svc_main = build_service(creds)
    sync_state = SyncState(
        SYNC_STATE_FILE,
        SOURCE_FOLDER_ID,
        DEST_FOLDER_ID,
        enabled=SMART_SCAN_ENABLED,
        force_full_scan=FORCE_FULL_SCAN,
    )
    sync_state.prepare(svc_main)

    engine = CopyEngine(creds, sync_state)
    t_start = time.time()

    if sync_state.should_skip_everything():
        print("\n[SMART] Source khong co thay doi moi, bo qua quet folder.")
        top_children = []
    else:
        top_children = list_children(svc_main, SOURCE_FOLDER_ID)
        sync_state.record_scanned_folder(
            SOURCE_FOLDER_ID, DEST_FOLDER_ID, "ROOT", top_children
        )
    folders = [
        c for c in top_children
        if not is_shortcut(c) and c["mimeType"] == FOLDER_MIME_TYPE
    ]
    loose_files = [
        c for c in top_children
        if not is_shortcut(c) and c["mimeType"] != FOLDER_MIME_TYPE
    ]

    print(f"\n📂 Source có {len(folders)} sub-folder + {len(loose_files)} file rời.")

    # --- Copy file rời ở tầng top ---
    if loose_files:
        print(f"\n📄 Đang copy {len(loose_files)} file rời ở tầng root...")
        for f in loose_files:
            engine.process_tree_item(svc_main, f, DEST_FOLDER_ID, "ROOT", 0)

    # --- Song song theo folder con ---
    if folders:
        print(f"\n🔀 Chạy {MAX_WORKERS} worker thread cho {len(folders)} sub-folder...")
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {
                pool.submit(engine.run_pair, folder["id"], DEST_FOLDER_ID, str(i + 1)): folder
                for i, folder in enumerate(folders)
            }
            for future in as_completed(futures):
                folder = futures[future]
                try:
                    future.result()
                except Exception as e:
                    print(f"  ❌ Worker lỗi ({folder['name']}): {e}")

    # --- Fallback: nếu SOURCE không có sub-folder, copy trực tiếp ---
    if not folders and not loose_files and not sync_state.should_skip_everything():
        print("  ⚠️ Source folder rỗng hoặc không truy cập được.")

    if loose_files or folders:
        print("\n⏳ Đang chờ các file copy song song hoàn tất...")
        engine.wait_for_file_copies()
    engine.shutdown()

    elapsed = time.time() - t_start

    # --- Export kết quả ---
    results = engine.results
    blocked_missing = [r for r in results if r["status"] == "BLOCKED_MISSING"]

    export_json(results, LOG_FILE)
    export_json(blocked_missing, BLOCKED_MISSING_FILE)
    export_excel(results, EXCEL_OUTPUT)
    sync_state.commit(results, svc_main)
    save_checkpoint(engine.checkpoint)
    print(f"📄 Đã lưu checkpoint cuối cùng ({len(engine.checkpoint)} mục)")
    print_summary(results, elapsed, engine.total_bytes_copied)

    if blocked_missing:
        print(f"\n🚫 Có {len(blocked_missing)} file bị BLOCK và CHƯA CÓ ở đích:")
        for r in blocked_missing[:20]:   # in tối đa 20 dòng đầu
            print(f"   - {r['path']}  [{r['id']}]")
        if len(blocked_missing) > 20:
            print(f"   ... (xem thêm trong {BLOCKED_MISSING_FILE})")

    # Exit code non-zero nếu có lỗi/block để GitHub Actions đánh dấu warning
    error_count = sum(1 for r in results if r["status"] in ("ERROR",))
    sys.exit(1 if error_count > 0 else 0)


if __name__ == "__main__":
    main()
