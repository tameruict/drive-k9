"""
Upload all downloaded PDFs to their correct Google Drive destination folders.
Uses the checkpoint to get the exact list of downloaded file IDs.
"""
import json
import os
import sys
from pathlib import Path

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

# Load checkpoint to get the exact list of downloaded file IDs
with open('batch_downloader_checkpoint.json', encoding='utf-8') as f:
    checkpoint = json.load(f)

downloaded_ids = [fid for fid, status in checkpoint.items() if status == 'OK']
print(f"Checkpoint shows {len(downloaded_ids)} files downloaded OK")

# Load the mapping
with open('file_folder_mapping.json', encoding='utf-8') as f:
    mapping = json.load(f)  # {file_id: {filename, folder_id}}

# Collect all PDFs from both directories
all_pdfs = {}  # {filename: path}

for pdf in Path('downloaded_pdfs').glob('*.pdf'):
    all_pdfs[pdf.name] = pdf
for pdf in Path('downloaded_pdfs_dedup').glob('*.pdf'):
    all_pdfs[pdf.name] = pdf

print(f"Found {len(all_pdfs)} PDF files on disk")

# Create upload plan using only the downloaded IDs from checkpoint
upload_plan = {}  # {folder_id: [(local_path, original_filename, file_id), ...]}
matched = 0
unmatched = []

for file_id in downloaded_ids:
    if file_id not in mapping:
        print(f"  [WARN] File ID {file_id} not in mapping")
        continue

    info = mapping[file_id]
    folder_id = info['folder_id']
    original_name = info['filename']

    # Find the downloaded file on disk
    # Try exact match first
    local_path = None
    if original_name in all_pdfs:
        local_path = all_pdfs[original_name]
    else:
        # Try with numeric prefix removed
        import re
        core_name = re.sub(r'^\d+\.\s*', '', original_name)
        if core_name in all_pdfs:
            local_path = all_pdfs[core_name]
        else:
            # Try dedup format: {file_id[:8]}_{name}
            for disk_name, disk_path in all_pdfs.items():
                if disk_name.startswith(file_id[:8]):
                    local_path = disk_path
                    break

    if local_path:
        if folder_id not in upload_plan:
            upload_plan[folder_id] = []
        upload_plan[folder_id].append((str(local_path), original_name, file_id))
        matched += 1
    else:
        unmatched.append((file_id, original_name))

print(f"\nMatched {matched}/{len(downloaded_ids)} files")
if unmatched:
    print(f"Unmatched files:")
    for fid, name in unmatched[:10]:
        print(f"  {fid[:12]}... {name}")

print(f"\nUpload plan: {len(upload_plan)} destination folders")
for folder_id, files in sorted(upload_plan.items(), key=lambda x: len(x[1]), reverse=True)[:10]:
    print(f"  Folder {folder_id}: {len(files)} files")

# Save upload plan
with open('upload_plan.json', 'w', encoding='utf-8') as f:
    json.dump(upload_plan, f, ensure_ascii=False, indent=2)

print(f"\nSaved upload_plan.json")
print(f"Total files to upload: {sum(len(files) for files in upload_plan.values())}")
