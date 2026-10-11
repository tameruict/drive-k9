# GitHub Actions

Repository nay dung workflow `Drive K9 Sync`. Workflow tu chay luc 22:00 gio
Viet Nam moi ngay (cron `0 15 * * *` theo UTC; GitHub co the tre vai chuc phut)
va van chay tay duoc bang **Run workflow**. Workflow khong nhan cookie qua o
input vi workflow input khong phai secret va co the bi lo trong lich su run.

Luu y: GitHub tu tat lich chay cua repo public sau 60 ngay khong co commit moi;
khi do vao tab Actions bam **Enable workflow**.

## Secrets can tao

Vao **Settings > Secrets and variables > Actions** va tao:

- `DRIVE_TOKEN`: toan bo noi dung JSON cua file `token.json`. Chi can cap nhat
  lai khi OAuth token bi thu hoi.
- `DRIVE_COOKIE`: toan bo noi dung file cookie (nen dung Netscape `cookies.txt`
  de duoc tu kiem tra va tu lam moi).
- `SECRETS_PAT` (khuyen dung): fine-grained personal access token, chi cap cho
  repo `tameruict/drive-k9`, quyen **Secrets: Read and write**. Co token nay thi
  moi lan chay workflow goi `RotateCookies` va ghi cookie moi nguoc vao
  `DRIVE_COOKIE`.

## Tu kiem tra va lam moi cookie

Truoc khi sync, `cookie_maintenance.py` kiem tra cookie con dang nhap khong va
(neu co `SECRETS_PAT`) xin Google cap `__Secure-1PSIDTS` moi. Neu cookie da bi
Google dang xuat, sync van chay phan copy binh thuong nhung run se bao **failed**
o buoc cuoi (GitHub gui email) — luc do can xuat cookie moi va cap nhat
`DRIVE_COOKIE`. Lam moi chi keo dai phien con song, khong cuu duoc phien da bi
dang xuat.

Meo de phien song lau: dang nhap tai khoan dich trong mot cua so an danh / profile
rieng, xuat cookie, roi dong cua so **khong bam dang xuat**, va khong dung
phien do de duyet web nua.

Cookie co the o mot trong cac dang ma chuong trinh dang ho tro: JSON export,
Netscape `cookies.txt`, hoac raw `Cookie` header.

## Chay bang giao dien GitHub

1. Cap nhat secret `DRIVE_COOKIE`.
2. Mo tab **Actions**.
3. Chon **Drive K9 Sync**.
4. Bam **Run workflow**, kiem tra source/destination folder ID va worker count.

## Chay bang GitHub CLI tren PowerShell

```powershell
Get-Content -Raw -LiteralPath .\cookie.txt |
  gh secret set DRIVE_COOKIE --repo tameruict/drive-k9

gh workflow run drive_k9.yml --repo tameruict/drive-k9
```

Khong commit `token.json`, `cookie.txt`, file da tai, checkpoint, log hoac bao
cao vao repository.
