# Cross-Account Drive Sync (A đọc → B sở hữu)

Tool cho tình huống: **A** (ví dụ mail `.edu`, ít dung lượng) xem được folder
nguồn; **B** (ví dụ Gmail cá nhân, kho lưu nhiều dung lượng) không xem được
nguồn nhưng cần sở hữu toàn bộ nội dung.

Vì A và B **khác domain** (Gmail ↔ .edu), Google **không cho chuyển ownership
trực tiếp**. Tool này né hẳn việc đó: A tải từng file xuống đĩa máy chạy (runner
GitHub Actions), rồi B upload lên Drive của B. File chỉ đi qua đĩa tạm nên
**Drive của A không tốn quota**, và **B là chủ sở hữu ngay từ đầu**.

```
Folder nguồn ──(token A đọc)──> đĩa runner ──(token B ghi)──> Drive B (B sở hữu)
```

---

## Bước 1 — Tạo OAuth client trên Google Cloud

Làm **một lần**, dùng chung cho cả A và B.

1. Vào https://console.cloud.google.com/ → tạo/chọn 1 project.
2. **APIs & Services → Library** → tìm **Google Drive API** → **Enable**.
3. **APIs & Services → OAuth consent screen**:
   - User type: **External** → Create.
   - Điền tên app + email, lưu.
   - Mục **Test users** → **Add users** → thêm **cả email A và email B**
     (bắt buộc khi app còn ở chế độ Testing).
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - Application type: **Desktop app**.
   - Create → **Download JSON** → lưu vào thư mục repo thành `credentials.json`.

> `credentials.json` đã nằm trong `.gitignore`, không bị commit.

## Bước 2 — Lấy token cho A và B (có link để bấm)

Cài thư viện rồi chạy `authorize.py`. Script in ra **link OAuth**; mở link bằng
trình duyệt đang đăng nhập đúng tài khoản (nên dùng **cửa sổ ẩn danh** mỗi acc).

```powershell
python -m pip install google-api-python-client google-auth google-auth-httplib2 google-auth-oauthlib

# Token A (.edu, acc xem được nguồn):
python authorize.py --role A --expected-email ten@truong.edu.vn

# Token B (gmail, kho lưu):
python authorize.py --role B --expected-email tenban@gmail.com
```

Sinh ra `token_A.json` và `token_B.json` (đã `.gitignore`). `--expected-email`
chặn ghi nhầm nếu lỡ đăng nhập sai acc.

## Bước 3 — Đưa token lên GitHub Secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret**,
hoặc dùng GitHub CLI (không in token ra màn hình):

```powershell
Get-Content -Raw token_A.json | gh secret set TOKEN_A --repo tameruict/drive-k9
Get-Content -Raw token_B.json | gh secret set TOKEN_B --repo tameruict/drive-k9
```

| Secret | Nội dung |
| --- | --- |
| `TOKEN_A` | toàn bộ JSON của `token_A.json` (acc đọc nguồn) |
| `TOKEN_B` | toàn bộ JSON của `token_B.json` (acc kho lưu) |

Token tự refresh bằng `refresh_token` bên trong, nên chỉ cập nhật lại khi bị thu hồi.

## Bước 4 — Chạy (treo trên GitHub Actions)

Repo → tab **Actions** → **Cross-Account Drive Sync** → **Run workflow**, điền:

- **source_folder_id**: folder nguồn trên Drive A (dán ID hoặc cả URL đều được).
- **dest_folder_id**: folder đích trên Drive B.
- **max_workers**: số file song song (mặc định 4).
- **dry_run**: bật để chạy thử (chỉ liệt kê, không ghi gì).

Hoặc bằng CLI:

```powershell
gh workflow run cross_account_sync.yml `
  --repo tameruict/drive-k9 `
  -f source_folder_id=SOURCE_FOLDER_ID `
  -f dest_folder_id=DEST_FOLDER_ID `
  -f max_workers=4 `
  -f dry_run=false
```

Tắt trình duyệt job vẫn chạy (tối đa ~6h/lần). Chạy lại cùng tham số sẽ dùng
checkpoint, bỏ qua file đã xong. Nên **dry_run=true** một lần đầu để kiểm tra.

---

## Chạy local (tuỳ chọn)

```powershell
python cross_account_sync.py `
  --source-token token_A.json `
  --dest-token token_B.json `
  --source-folder-id SOURCE_FOLDER_ID `
  --dest-folder-id DEST_FOLDER_ID `
  --workers 4
```

## Ghi chú

- File Google Docs/Sheets/Slides không tải nhị phân được nên tool **export**:
  Docs/Slides → PDF, Sheets → XLSX, Drawing → PNG. File thường (video, PDF, ảnh,
  zip...) tải nguyên gốc.
- Shortcut được giải về file gốc rồi copy thành file thật trong Drive B.
- Trùng tên ở đích được bỏ qua để tránh tạo bản sao (tắt bằng `--no-verify-dup`).
- Dung lượng đĩa runner có hạn (~14GB trống); tool tải từng file rồi xoá ngay
  sau khi upload nên chỉ cần đủ chỗ cho file lớn nhất.
