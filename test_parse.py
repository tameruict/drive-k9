import re

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
        # We need to find the last [ that's not escaped
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

files = parse_markdown('block_pdf.md')
print(f"Found {len(files)} files")
