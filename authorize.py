"""Tạo token Google Drive cho acc A và B bằng OAuth, có in LINK để bấm.

Cần một file `credentials.json` là OAuth client loại **Desktop app** tải từ
Google Cloud Console (xem CROSS_ACCOUNT_SYNC.md). Chạy script, nó in ra một link;
mở link bằng đúng trình duyệt đang đăng nhập acc cần lấy token, đồng ý quyền
Drive, trình duyệt sẽ quay về localhost và token được ghi ra file.

Ví dụ
-----
  # Lấy token cho acc A (.edu, acc xem được nguồn):
  python authorize.py --role A --expected-email ten@truong.edu.vn

  # Lấy token cho acc B (gmail, kho lưu):
  python authorize.py --role B --expected-email tenban@gmail.com

Sinh ra token_A.json / token_B.json. KHÔNG commit các file này (đã .gitignore).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/drive"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Lấy token Google Drive qua OAuth link.")
    parser.add_argument("--role", choices=["A", "B"], help="A = acc đọc nguồn, B = acc kho lưu.")
    parser.add_argument("--credentials", default="credentials.json",
                        help="OAuth client Desktop JSON tải từ Google Cloud Console.")
    parser.add_argument("--output", default="",
                        help="Đường dẫn token ra (mặc định token_<role>.json).")
    parser.add_argument("--expected-email", default="",
                        help="Email mong đợi — chặn ghi nhầm nếu đăng nhập sai acc.")
    parser.add_argument("--port", type=int, default=0,
                        help="Cổng localhost nhận callback (0 = tự chọn).")
    args = parser.parse_args()

    output = args.output or (f"token_{args.role}.json" if args.role else "token.json")
    creds_file = Path(args.credentials)
    if not creds_file.is_file():
        raise SystemExit(
            f"Không thấy {creds_file}. Tải OAuth client (Desktop app) từ Google Cloud "
            "Console rồi lưu thành credentials.json (xem CROSS_ACCOUNT_SYNC.md)."
        )

    target = args.expected_email or (f"acc {args.role}" if args.role else "(tài khoản cần lấy token)")
    print(
        f"\n=== Lấy token cho: {target} ===\n"
        "Mở LINK dưới đây bằng trình duyệt đang đăng nhập đúng tài khoản đó.\n"
        "Nếu máy có nhiều tài khoản Google, dùng cửa sổ ẩn danh cho chắc.\n",
        file=sys.stderr, flush=True,
    )

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_file), scopes=SCOPES)
    creds = flow.run_local_server(
        host="localhost",
        port=args.port,
        open_browser=False,
        access_type="offline",
        prompt="select_account consent",
        authorization_prompt_message="\n[LINK OAUTH]\n{url}\n\nĐang chờ đăng nhập Google...\n",
        success_message="Xong! Đóng tab này và quay lại cửa sổ dòng lệnh.",
    )

    # Xác minh đúng tài khoản trước khi ghi đè token.
    try:
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        email = str(service.about().get(fields="user(emailAddress)")
                    .execute().get("user", {}).get("emailAddress", "")).strip()
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"Không xác minh được email tài khoản: {exc}")

    if args.expected_email and email.casefold() != args.expected_email.casefold():
        raise SystemExit(
            f"Đăng nhập nhầm: '{email}' nhưng cần '{args.expected_email}'. "
            "KHÔNG ghi token. Chạy lại và chọn đúng tài khoản."
        )

    token_info = json.loads(creds.to_json())
    if not token_info.get("refresh_token"):
        raise SystemExit(
            "Google không trả refresh_token nên token sẽ hết hạn nhanh. Vào "
            "https://myaccount.google.com/permissions gỡ quyền app rồi chạy lại."
        )

    out_path = Path(output)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(creds.to_json(), encoding="utf-8")
    tmp.replace(out_path)
    print(f"\n✓ Đã lưu token cho {email} -> {out_path}", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
