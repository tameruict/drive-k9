import re
import os
import sys
import requests

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

def download_file(file_id, filename, output_dir):
    """Download file from Google Drive using direct download URL"""
    url = f"https://drive.google.com/uc?id={file_id}&export=download"
    os.makedirs(output_dir, exist_ok=True)
    filepath = os.path.join(output_dir, filename)

    try:
        response = requests.get(url, stream=True, timeout=30)
        response.raise_for_status()
        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        print(f"✓")
        return True
    except Exception as e:
        print(f"✗ {str(e)}")
        return False

def parse_markdown(filepath):
    """Parse markdown table and extract file info"""
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    files = []

    for line in lines:
        if not line.startswith('|') or '|---|' in line or 'File bị block' in line:
            continue

        # Extract file ID from /file/d/ID/view
        file_match = re.search(r'/file/d/([a-zA-Z0-9-_]+)/view', line)
        if not file_match:
            continue
        file_id = file_match.group(1)

        # Extract folder ID from /folders/ID
        folder_match = re.search(r'/folders/([a-zA-Z0-9-_]+)', line)
        if not folder_match:
            continue
        folder_id = folder_match.group(1)

        # Find the pattern: ](https://drive.google.com/file/d/
        # Then work backwards to find the opening [
        pattern_pos = line.find('](https://drive.google.com/file/d/')
        if pattern_pos == -1:
            continue

        # Find the opening [ before this position
        opening_bracket = line.rfind('[', 0, pattern_pos)
        if opening_bracket == -1:
            continue

        filename = line[opening_bracket+1:pattern_pos].strip()

        files.append({
            'filename': filename,
            'file_id': file_id,
            'folder_id': folder_id
        })

    return files

def main():
    md_file = 'block_pdf.md'
    output_base = 'downloaded_pdfs'

    if not os.path.exists(md_file):
        print(f"Error: {md_file} not found")
        return

    files = parse_markdown(md_file)
    print(f"Found {len(files)} files to download\n")

    success = 0
    failed = 0

    for i, file_info in enumerate(files, 1):
        filename = file_info['filename']
        file_id = file_info['file_id']
        folder_id = file_info['folder_id']
        output_dir = os.path.join(output_base, folder_id)

        print(f"[{i:3d}/{len(files)}] ", end='', flush=True)
        if download_file(file_id, filename, output_dir):
            success += 1
        else:
            failed += 1

    print(f"\n{'='*50}")
    print(f"Complete: {success} succeeded, {failed} failed")

if __name__ == '__main__':
    main()
