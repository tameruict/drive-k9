import re

with open('block_pdf.md', 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Print first few data rows
for i in range(5, 10):
    line = lines[i]
    print(f"Line {i}:")
    print(repr(line[:200]))
    print()
