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

## Re-upload video ClassIn

Workflow `ClassIn Video Reupload` nhan hai payload tam thoi duoc tao tu
`video_manifest.v1.private.json`. Manifest chuan chi luu mot media mot lan,
giu cac URL mirror lam fallback va anh xa media den dung bai hoc.

```powershell
python "E:\Chui Bailearn\CLASSIN_CRAWLER_V2_20260819\video_manifest.py" publish-actions `
  --manifest "E:\Chui Bailearn\CLASSIN_CRAWLER_V2_20260819\final_math_20260819\video_manifest.v1.private.json" `
  --repo tameruict/drive-k9

gh workflow run classin_reupload.yml `
  --repo tameruict/drive-k9 `
  -f dest_folder_id=DEST_FOLDER_ID `
  -f max_workers=3
```

Sau khi run thanh cong, xoa hai secret payload:

```powershell
python "E:\Chui Bailearn\CLASSIN_CRAWLER_V2_20260819\video_manifest.py" cleanup-actions `
  --repo tameruict/drive-k9
```

## Re-upload video Sangsang nhanh

Workflow `Sangsang Course Video Reupload` tao plan theo thoi luong/bitrate,
chia thanh 40 shard va chay toi da 20 runner. Moi video dung yt-dlp native HLS
de tai fragment song song, sau do remux MP4 khong encode lai. `DRIVE_TOKEN` van
la secret OAuth hien co.

```powershell
gh workflow run sangsang_dgnl_bca.yml `
  --repo tameruict/drive-vercel `
  -f course_slug=dgnl-bca `
  -f dest_folder_id=DEST_FOLDER_ID `
  -f max_workers=2 `
  -f fragment_concurrency=4 `
  -f download_timeout=7200 `
  -f limit=0 `
  -f force_retry=false
```

`fragment_concurrency=4` la diem khoi dau; chi tang len `8` sau khi canary
khong co HTTP 429/403 va toc do khong con tang dang ke. Moi shard luu
checkpoint va report rieng, nen co the rerun de chi xu ly video loi/thieu.
