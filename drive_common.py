"""Shared Google Drive helpers used by the copy and reconcile tools."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any


FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
SHORTCUT_MIME_TYPE = "application/vnd.google-apps.shortcut"
MAX_SHORTCUT_HOPS = 8

DEFAULT_BLOCKED_KEYWORDS = (
    "cannotcopyfile",
    "filenotdownloadable",
    "cannotdownload",
    "downloadquotaexceeded",
    "insufficientpermissions",
    "forbidden",
    "cannotmovefolderbetweenshareddrives",
)

DEFAULT_RETRYABLE_KEYWORDS = (
    "ratelimitexceeded",
    "userratelimitexceeded",
    "backenderror",
    "internalerror",
)

DRIVE_FOLDER_SEPARATOR_TRANSLATION = {
    ord("_"): " ",
    ord("|"): " ",
    ord("\u00a6"): " ",
    ord("\u2016"): " ",
    ord("\uff5c"): " ",
    ord("-"): " ",
    ord("\u2010"): " ",
    ord("\u2011"): " ",
    ord("\u2012"): " ",
    ord("\u2013"): " ",
    ord("\u2014"): " ",
    ord("\u2015"): " ",
}


def drive_query_literal(value: str) -> str:
    """Escape a string literal for Google Drive query expressions."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def normalize_drive_name(name: str) -> str:
    """Return a stable comparison key for Drive file and folder names."""
    normalized = unicodedata.normalize("NFKC", str(name or ""))
    normalized = normalized.translate(
        {
            ord("\u00a0"): " ",
            ord("\u200b"): None,
            ord("\u200c"): None,
            ord("\u200d"): None,
            ord("\ufeff"): None,
        }
    )
    return re.sub(r"\s+", " ", normalized).strip().casefold()


def normalize_drive_path(path: str) -> str:
    """Normalize each path segment while preserving Drive hierarchy."""
    parts = [normalize_drive_name(part) for part in path.strip("/").split("/")]
    return "/".join(part for part in parts if part)


def loose_drive_folder_name(name: str) -> str:
    """Normalize folder names with common visual separators treated as spaces."""
    normalized = normalize_drive_name(name)
    normalized = normalized.translate(DRIVE_FOLDER_SEPARATOR_TRANSLATION)
    return re.sub(r"\s+", " ", normalized).strip()


def same_drive_name(left: str, right: str, *, loose_folder: bool = False) -> bool:
    if normalize_drive_name(left) == normalize_drive_name(right):
        return True
    if loose_folder:
        return loose_drive_folder_name(left) == loose_drive_folder_name(right)
    return False


def find_matching_item(
    items: Sequence[Mapping[str, Any]],
    name: str,
    mime_type: str | None = None,
    *,
    folder_mime_type: str = FOLDER_MIME_TYPE,
) -> Mapping[str, Any] | None:
    """Find the best Drive item match by exact, normalized, then loose folder name."""
    exact_match = None
    normalized_match = None
    loose_match = None
    loose_ambiguous = False
    loose_name = loose_drive_folder_name(name) if mime_type == folder_mime_type else None

    for item in items:
        if mime_type and item.get("mimeType") != mime_type:
            continue

        item_name = str(item.get("name", ""))
        if item_name == name:
            exact_match = item
            break
        if normalized_match is None and same_drive_name(item_name, name):
            normalized_match = item
        if loose_name is not None and loose_drive_folder_name(item_name) == loose_name:
            if loose_match is None:
                loose_match = item
            elif loose_match.get("id") != item.get("id"):
                loose_ambiguous = True

    if exact_match or normalized_match:
        return exact_match or normalized_match
    if loose_ambiguous:
        return None
    return loose_match


def is_shortcut(file_info: Mapping[str, Any]) -> bool:
    return file_info.get("mimeType") == SHORTCUT_MIME_TYPE


def without_drive_shortcuts(
    items: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    return [item for item in items if not is_shortcut(item)]


def skipped_shortcut_reason(file_info: Mapping[str, Any]) -> str:
    name = str(file_info.get("name") or "").strip()
    return f"Bo qua shortcut: {name}" if name else "Bo qua shortcut"


def shortcut_details(file_info: Mapping[str, Any]) -> Mapping[str, Any]:
    details = file_info.get("shortcutDetails") or {}
    return details if isinstance(details, Mapping) else {}


def shortcut_target_id(file_info: Mapping[str, Any]) -> str | None:
    target_id = shortcut_details(file_info).get("targetId")
    return str(target_id) if target_id else None


def shortcut_target_mime_type(file_info: Mapping[str, Any]) -> str:
    return str(shortcut_details(file_info).get("targetMimeType") or "")


def shortcut_checkpoint_key(shortcut_id: str, target_id: str) -> str:
    return f"shortcut:{shortcut_id}:{target_id}"


def skipped_file_reason_for_mimes(
    file_info: Mapping[str, Any],
    skipped_mime_types: Mapping[str, str],
) -> str | None:
    file_type = skipped_mime_types.get(str(file_info.get("mimeType", "")))
    return f"B\u1ecf qua file {file_type}" if file_type else None


def http_error_status(error: Any) -> int | None:
    response = getattr(error, "resp", None)
    return getattr(response, "status", None)


def http_error_has_keywords(error: Any, keywords: Sequence[str]) -> bool:
    reason = str(error).lower()
    return any(keyword in reason for keyword in keywords)


def is_blocked_drive_error(
    error: Any,
    *,
    keywords: Sequence[str] = DEFAULT_BLOCKED_KEYWORDS,
    statuses: Sequence[int] = (403, 400),
) -> bool:
    return http_error_status(error) in statuses and http_error_has_keywords(error, keywords)


def is_retryable_drive_error(
    error: Any,
    *,
    keywords: Sequence[str] = DEFAULT_RETRYABLE_KEYWORDS,
    statuses: Sequence[int] = (429, 500, 502, 503, 504),
) -> bool:
    status = http_error_status(error)
    return status in statuses or http_error_has_keywords(error, keywords)
