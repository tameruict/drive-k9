"""
CRAWL BLOCKED PDFs
==================
Recursive crawl Google Drive folder, detect PDF files where the owner has disabled
download/copy, and export an ID list ready to feed into batch_cookie_downloader.py.

Detection: a file is considered blocked when
  - capabilities.canDownload is False, OR
  - capabilities.canCopy is False
on a non-folder Drive item. This avoids hammering the API with copy attempts.

OUTPUT:
  blocked_pdfs.txt        — one entry per line: "FILE_ID display_name.ext"
  blocked_pdfs.json       — full metadata for debugging / resume

USAGE:
  $env:DRIVE_TOKEN = Get-Content token.json -Raw
  python crawl_blocked_pdfs.py
  python crawl_blocked_pdfs.py --root FOLDER_ID --include-workspace
  python crawl_blocked_pdfs.py --include-non-pdf
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
WORKSPACE_MIMES = {
    "application/vnd.google-apps.document",
    "application/vnd.google-apps.spreadsheet",
    "application/vnd.google-apps.presentation",
    "application/vnd.google-apps.drawing",
}

DEFAULT_ROOT = os.environ.get("SOURCE_FOLDER_ID", "107CZgk5F7iRX_b9Esa1avdOEkwTIMKIh")
DEFAULT_TOKEN_PATH = "token.json"


def build_credentials(token_path: str) -> Credentials:
    drive_token = os.environ.get("DRIVE_TOKEN")
    if drive_token:
        info = json.loads(drive_token)
    else:
        with open(token_path, "r", encoding="utf-8") as f:
            info = json.load(f)

    creds = Credentials.from_authorized_user_info(info)
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds


def list_children(service, folder_id: str) -> list[dict]:
    items, page_token = [], None
    fields = (
        "files(id, name, mimeType, size, capabilities/canDownload, "
        "capabilities/canCopy, shortcutDetails), nextPageToken"
    )
    while True:
        try:
            resp = service.files().list(
                q=f"'{folder_id}' in parents and trashed = false",
                pageSize=1000,
                fields=fields,
                pageToken=page_token,
                corpora="allDrives",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
        except HttpError as e:
            print(f"  [WARN] list_children({folder_id}): {e}", file=sys.stderr)
            return items
        items.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            return items


def get_metadata(service, file_id: str) -> dict | None:
    try:
        return service.files().get(
            fileId=file_id,
            fields=(
                "id, name, mimeType, size, capabilities/canDownload, "
                "capabilities/canCopy"
            ),
            supportsAllDrives=True,
        ).execute()
    except HttpError as e:
        print(f"  [WARN] get_metadata({file_id}): {e}", file=sys.stderr)
        return None


def is_blocked(item: dict) -> bool:
    caps = item.get("capabilities") or {}
    if caps.get("canDownload") is False:
        return True
    if caps.get("canCopy") is False:
        return True
    return False


def target_mime(mime: str, include_workspace: bool, include_non_pdf: bool) -> bool:
    if mime == "application/pdf":
        return True
    if include_workspace and mime in WORKSPACE_MIMES:
        return True
    if include_non_pdf and mime != FOLDER_MIME and mime != SHORTCUT_MIME:
        return True
    return False


def crawl(
    service,
    root_id: str,
    *,
    include_workspace: bool,
    include_non_pdf: bool,
    max_depth: int,
) -> list[dict]:
    """BFS through the folder tree, return blocked target files only."""
    blocked: list[dict] = []
    seen_folders: set[str] = set()
    queue: list[tuple[str, str, int]] = [(root_id, "", 0)]

    total_scanned = 0
    total_folders = 0

    while queue:
        folder_id, rel_path, depth = queue.pop(0)
        if folder_id in seen_folders:
            continue
        seen_folders.add(folder_id)
        total_folders += 1

        if depth > max_depth:
            print(f"  [SKIP] depth>{max_depth}: {rel_path}", file=sys.stderr)
            continue

        children = list_children(service, folder_id)
        print(f"  [DIR] {rel_path or '/'} ({len(children)} items)", file=sys.stderr, flush=True)

        for item in children:
            total_scanned += 1
            mime = item.get("mimeType", "")
            name = item.get("name", "")

            if mime == FOLDER_MIME:
                queue.append((item["id"], f"{rel_path}/{name}", depth + 1))
                continue

            if mime == SHORTCUT_MIME:
                details = item.get("shortcutDetails") or {}
                tgt_id = details.get("targetId")
                tgt_mime = details.get("targetMimeType", "")
                if tgt_mime == FOLDER_MIME and tgt_id:
                    queue.append((tgt_id, f"{rel_path}/{name}", depth + 1))
                    continue
                if tgt_id and target_mime(tgt_mime, include_workspace, include_non_pdf):
                    real = get_metadata(service, tgt_id)
                    if real and is_blocked(real):
                        real["_path"] = f"{rel_path}/{name}"
                        blocked.append(real)
                continue

            if not target_mime(mime, include_workspace, include_non_pdf):
                continue

            if is_blocked(item):
                item["_path"] = f"{rel_path}/{name}"
                blocked.append(item)

    print(
        f"\n[*] Scanned {total_scanned} items across {total_folders} folders. "
        f"Found {len(blocked)} blocked files.",
        file=sys.stderr,
    )
    return blocked


def sanitize_for_listing(name: str) -> str:
    forbidden = '<>:"/\\|?*\n\r\t'
    for ch in forbidden:
        name = name.replace(ch, "_")
    return name.strip()[:200] or "untitled"


def write_outputs(blocked: list[dict], txt_path: Path, json_path: Path) -> None:
    txt_path.parent.mkdir(parents=True, exist_ok=True)

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(
            "# Blocked PDFs - feed into batch_cookie_downloader.py via --input\n"
            "# Format: FILE_ID  display_name\n"
        )
        for item in blocked:
            fid = item["id"]
            name = sanitize_for_listing(item.get("name", fid))
            f.write(f"{fid}  {name}\n")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(blocked, f, ensure_ascii=False, indent=2)

    print(f"\n[OK] Wrote {len(blocked)} entries to:")
    print(f"     - {txt_path}")
    print(f"     - {json_path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Crawl Drive folder and export list of blocked PDFs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--root", default=DEFAULT_ROOT, help="Root folder ID to crawl")
    parser.add_argument("--token", default=DEFAULT_TOKEN_PATH, help="OAuth token file")
    parser.add_argument(
        "--include-workspace",
        action="store_true",
        help="Also include blocked Google Docs/Sheets/Slides files",
    )
    parser.add_argument(
        "--include-non-pdf",
        action="store_true",
        help="Include all blocked files, not just PDFs",
    )
    parser.add_argument("--max-depth", type=int, default=20)
    parser.add_argument("--out-txt", default="blocked_pdfs.txt")
    parser.add_argument("--out-json", default="blocked_pdfs.json")
    args = parser.parse_args()

    print(f"[*] Root folder: {args.root}")
    print(f"[*] Token:       {args.token}")
    print(f"[*] Workspace:   {args.include_workspace}")
    print(f"[*] Non-PDF:     {args.include_non_pdf}")

    creds = build_credentials(args.token)
    service = build("drive", "v3", credentials=creds, cache_discovery=False)

    blocked = crawl(
        service,
        args.root,
        include_workspace=args.include_workspace,
        include_non_pdf=args.include_non_pdf,
        max_depth=args.max_depth,
    )

    write_outputs(blocked, Path(args.out_txt), Path(args.out_json))
    return 0


if __name__ == "__main__":
    sys.exit(main())
