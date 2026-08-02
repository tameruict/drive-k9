# GitHub Actions

Repository nay dung workflow `Drive K9 Manual Sync` va chi chay khi bam
**Run workflow**. Workflow khong nhan cookie qua o input vi workflow input
khong phai secret va co the bi lo trong lich su run.

## Secrets can tao

Vao **Settings > Secrets and variables > Actions** va tao:

- `DRIVE_TOKEN`: toan bo noi dung JSON cua file `token.json`. Chi can cap nhat
  lai khi OAuth token bi thu hoi.
- `DRIVE_COOKIE`: toan bo noi dung file cookie. Cap nhat secret nay truoc moi
  lan chay.

Cookie co the o mot trong cac dang ma chuong trinh dang ho tro: JSON export,
Netscape `cookies.txt`, hoac raw `Cookie` header.

## Chay bang giao dien GitHub

1. Cap nhat secret `DRIVE_COOKIE`.
2. Mo tab **Actions**.
3. Chon **Drive K9 Manual Sync**.
4. Bam **Run workflow**, kiem tra source/destination folder ID va worker count.

## Chay bang GitHub CLI tren PowerShell

```powershell
Get-Content -Raw -LiteralPath .\cookie.txt |
  gh secret set DRIVE_COOKIE --repo tameruict/drive-k9

gh workflow run drive_k9.yml --repo tameruict/drive-k9
```

Khong commit `token.json`, `cookie.txt`, file da tai, checkpoint, log hoac bao
cao vao repository.

