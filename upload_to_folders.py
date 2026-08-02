"""
Upload downloaded PDFs to their correct Google Drive destination folders.
Matches files by file_id prefix in filename (for dedup files) or by exact filename.
"""
import json
import os
import re
import sys
from pathlib import Path

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GRequest
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

def load_credentials(token_path='token.json'):
    with open(token_path, 'r', encoding='utf-8') as f:
        info = json.load(f)
    creds = Credentials.from_authorized_user_info(info)
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(GRequest())
        with open(token_path, 'w', encoding='utf-8') as f:
            f.write(creds.to_json())
    return creds

# Load file-to-folder mapping: {file_id: {filename, folder_id}}
with open('file_folder_mapping.json', encoding='utf-8') as f:
    mapping = json.load(f)

# Build reverse lookup: sanitized_filename -> [(file_id, folder_id, original_filename)]
# The downloader sanitizes: re.sub(r'[<>:"/\\|?*]', '_', name).strip()[:200]
def sanitize(name):
    return re.sub(r'[<>:"/\\|?*]', '_', name).strip()[:200]

sanitized_to_entries = {}
for fid, info in mapping.items():
    orig = info['filename']
    san = sanitize(orig)
    if san not in sanitized_to_entries:
        sanitized_to_entries[san] = []
    sanitized_to_entries[san].append((fid, info['folder_id'], orig))

# Build service
creds = load_credentials()
service = build('drive', 'v3', credentials=creds)

# Process all PDFs in downloaded_pdfs/
pdf_dir = Path('downloaded_pdfs')
uploaded = 0
failed = 0
skipped = 0
no_match = 0

print(f"Uploading PDFs from {pdf_dir} to Google Drive...")
print("=" * 60)

all_pdfs = sorted(pdf_dir.glob('*.pdf'))
print(f"Found {len(all_pdfs)} PDF files\n")

for i, pdf_file in enumerate(all_pdfs, 1):
    disk_name = pdf_file.stem + pdf_file.suffix  # full filename on disk

    # Try exact sanitized match
    entries = sanitized_to_entries.get(disk_name, [])

    if not entries:
        # Try matching without the leading bracket issue (some filenames start with [)
        # The downloader strips the opening [ from filenames like "[NP 2027]..."
        # Try prepending common prefixes
        for prefix in ['[', '']:
            candidate = prefix + disk_name
            entries = sanitized_to_entries.get(sanitize(candidate), [])
            if entries:
                break

    if not entries:
        print(f"[{i}/{len(all_pdfs)}] ⚠ NO MATCH: {disk_name}")
        no_match += 1
        continue

    # Upload to each destination folder
    for file_id, folder_id, original_name in entries:
        print(f"[{i}/{len(all_pdfs)}] {original_name}")
        print(f"    → folder: {folder_id}")

        try:
            # Check if already exists
            safe_query_name = original_name.replace("'", "\\'")
            results = service.files().list(
                q=f"name='{safe_query_name}' and '{folder_id}' in parents and trashed=false",
                fields='files(id, name)',
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()

            existing = results.get('files', [])
            if existing:
                print(f"    ✓ Already exists — skip")
                skipped += 1
                continue

            # Upload
            file_metadata = {'name': original_name, 'parents': [folder_id]}
            media = MediaFileUpload(str(pdf_file), mimetype='application/pdf', resumable=True)
            result = service.files().create(
                body=file_metadata,
                media_body=media,
                fields='id',
                supportsAllDrives=True,
            ).execute()
            print(f"    ✓ Uploaded (ID: {result['id']})")
            uploaded += 1

        except Exception as e:
            print(f"    ✗ FAILED: {e}")
            failed += 1

print(f"\n{'=' * 60}")
print(f"RESULTS: {uploaded} uploaded | {failed} failed | {skipped} already existed | {no_match} no match")
print(f"{'=' * 60}")
