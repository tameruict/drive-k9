"""
Re-download the 4 groups of files that collided on disk (same filename, different IDs).
Each file is saved as {file_id}_{original_name}.pdf so nothing overwrites.
"""
import json, re, sys, subprocess
from pathlib import Path

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

# Groups that had name collisions (3 IDs → same filename)
COLLISION_GROUPS = {
    "NP 2027_Bài tập ngày 21-5": [
        "1jdmL9K4NgR2y4oyQ-df3GAOXxiU6J9EW",
        "1pXqJFh4bBwUaAs1Y4YCEzKm-WUuhczjI",
        "1iarn_yWzRYzZBdkLb6fYJDy4Cg7JPbJL",
    ],
    "NP 2027_Bài tập ngày 20-5": [
        "1PUmKLt93hIgKPOJPSM80-WybuTb79CVr",
        "1MJv2ISADFoi3Ux-sthJw3mmRc3mHMhhi",
        "1XK0qxwNjQE0Uu7cbj6bpYX5649eS-_L-",
    ],
    "NP 2027_Bài tập ngày 19-5": [
        "1uITFtokzTG8lMo8ipLoWFcxdKSJ3U2w0",
        "1TUb3UOFmhl6PaLSS68-Gz9Fz_zFt3bQD",
        "1ny-a5_L92pUmNAI8EsmPaDKmpUTTkw78",
    ],
    "NP 2027_Bài tập ngày 18-5": [
        "1PI8gXd7tgv2jJeDeBa43x5BFe8AZ7fnk",
        "1m7dLtung6safotWrm2P1UGytSrZBkNXu",
        "1kLvM_6PJ3rEpO3Cv4o7KaiaaZy2FiGs_",
    ],
}

# Load the folder mapping to get proper filenames and destinations
with open("file_folder_mapping.json", encoding="utf-8") as f:
    mapping = json.load(f)   # {file_id: {filename, folder_id}}

output_base = Path("downloaded_pdfs_dedup")
output_base.mkdir(exist_ok=True)

# Write input file: one ID per line, with unique output name = file_id prefix
input_lines = []
id_to_name = {}

for group_label, ids in COLLISION_GROUPS.items():
    for fid in ids:
        info = mapping.get(fid, {})
        orig_name = info.get("filename", fid + ".pdf")
        # Prefix with short file ID to guarantee uniqueness
        unique_name = fid[:8] + "_" + orig_name
        input_lines.append(f"{fid} {unique_name}")
        id_to_name[fid] = {"orig": orig_name, "unique": unique_name,
                           "folder_id": info.get("folder_id", "")}

input_file = Path("dedup_ids.txt")
input_file.write_text("\n".join(input_lines) + "\n", encoding="utf-8")
print(f"Written {len(input_lines)} IDs to {input_file}")
for fid, info in id_to_name.items():
    print(f"  {fid[:12]}... → {info['unique']}")
