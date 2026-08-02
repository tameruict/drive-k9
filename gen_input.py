import re
import os
import sys

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

def parse_markdown(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    files = []
    for line in lines:
        if not line.startswith('|') or '|---|' in line or 'File bị block' in line:
            continue

        file_match = re.search(r'/file/d/([a-zA-Z0-9-_]+)/view', line)
        if not file_match:
            continue
        file_id = file_match.group(1)

        folder_match = re.search(r'/folders/([a-zA-Z0-9-_]+)', line)
        folder_id = folder_match.group(1) if folder_match else 'unknown'

        pattern_pos = line.find('](https://drive.google.com/file/d/')
        if pattern_pos == -1:
            continue

        # Find the opening [ for the markdown link (not nested brackets in filename)
        # Work backwards from pattern_pos to find the first | (table cell start)
        # then find the [ after it
        pipe_pos = line.rfind('|', 0, pattern_pos)
        if pipe_pos == -1:
            continue
        opening_bracket = line.find('[', pipe_pos, pattern_pos)
        if opening_bracket == -1:
            continue
        filename = line[opening_bracket+1:pattern_pos].strip()

        files.append({'filename': filename, 'file_id': file_id, 'folder_id': folder_id})

    return files

files = parse_markdown('block_pdf.md')

# Write input list for batch_cookie_downloader.py
# Format: FILE_ID filename.pdf
with open('blocked_pdf_ids.txt', 'w', encoding='utf-8') as f:
    for item in files:
        # Sanitize filename for the input file
        safe_name = re.sub(r'[<>:"/\\|?*]', '_', item['filename'])
        f.write(f"{item['file_id']} {safe_name}\n")

# Write folder mapping for post-processing
import json
mapping = {item['file_id']: {'filename': item['filename'], 'folder_id': item['folder_id']} for item in files}
with open('file_folder_mapping.json', 'w', encoding='utf-8') as f:
    json.dump(mapping, f, ensure_ascii=False, indent=2)

print(f"Generated blocked_pdf_ids.txt with {len(files)} entries")
print(f"Generated file_folder_mapping.json")
