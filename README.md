# Drive K9

Cong cu dong bo noi dung giua cac thu muc Google Drive, co checkpoint, smart
scan va fallback cho mot so file khong the copy truc tiep bang Drive API.

## Chay bang GitHub Actions

Workflow `Drive K9 Manual Sync` chi chay thu cong. Can tao hai Actions secrets:

- `DRIVE_TOKEN`: toan bo noi dung JSON cua `token.json`. Thuong chi can cau
  hinh mot lan.
- `DRIVE_COOKIE`: noi dung cookie JSON, Netscape `cookies.txt`, hoac raw Cookie
  header. Cap nhat secret nay truoc moi lan chay.

Khong nhap cookie vao workflow input: input thong thuong khong duoc bao ve nhu
secret.

### Chay tren giao dien GitHub

1. Mo **Settings > Secrets and variables > Actions**.
2. Cap nhat `DRIVE_COOKIE`.
3. Mo **Actions > Drive K9 Manual Sync > Run workflow**.
4. Kiem tra source/destination folder ID, sau do chay workflow.

### Chay bang GitHub CLI tren PowerShell

```powershell
Get-Content -Raw -LiteralPath .\cookie.txt |
  gh secret set DRIVE_COOKIE --repo tameruict/drive-k9

gh workflow run drive_k9.yml --repo tameruict/drive-k9
```

Xem them tai [GITHUB_ACTIONS.md](GITHUB_ACTIONS.md).

## Chay local

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
python windows_sync_tool_improved.py --no-input-prompt
```

`token.json`, `cookie.txt`, checkpoint, log, bao cao va cac file da tai deu
duoc loai khoi Git.

