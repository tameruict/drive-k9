"""Kiểm tra và làm mới cookie Google (file Netscape) dùng cho fallback tải video bị chặn.

Dùng trên GitHub Actions trước mỗi lần sync:
  python cookie_maintenance.py --cookie-file cookie.txt --rotate

- Kiểm tra phiên đăng nhập còn sống không (Drive redirect về ServiceLogin => đã đăng xuất).
- --rotate: gọi accounts.google.com/RotateCookies để Google cấp __Secure-1PSIDTS mới,
  ghi đè lại file cookie nếu có thay đổi. Phiên đã bị đăng xuất thì không cứu lại được.
- Ghi kết quả signed_in=0/1, rotated=0/1 vào $GITHUB_OUTPUT (nếu có).

Không bao giờ in giá trị cookie ra log (repo public): giá trị mới được che bằng ::add-mask::.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import requests

ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR / "gdrive-download"))
import gdrive_stream_downloader as stream_dl  # noqa: E402

ROTATE_COOKIES_URL = "https://accounts.google.com/RotateCookies"
LOGIN_CHECK_URL = "https://drive.google.com/drive/my-drive"
NETSCAPE_HEADER = "# Netscape HTTP Cookie File"
HTTPONLY_PREFIX = "#HttpOnly_"
ROTATED_COOKIE = "__Secure-1PSIDTS"


@dataclass
class CookieRow:
    domain: str
    include_subdomains: str
    path: str
    secure: str
    expires: str
    name: str
    value: str
    http_only: bool = False

    def to_line(self) -> str:
        domain = f"{HTTPONLY_PREFIX}{self.domain}" if self.http_only else self.domain
        return "\t".join(
            [domain, self.include_subdomains, self.path, self.secure, self.expires, self.name, self.value]
        )


def read_netscape(path: Path) -> list[CookieRow]:
    rows: list[CookieRow] = []
    for raw_line in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        line = raw_line.strip()
        http_only = line.startswith(HTTPONLY_PREFIX)
        if http_only:
            line = line[len(HTTPONLY_PREFIX):]
        elif not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7 or not parts[5]:
            continue
        rows.append(CookieRow(*parts[:6], "\t".join(parts[6:]), http_only=http_only))
    return rows


def write_netscape(path: Path, rows: list[CookieRow]) -> None:
    lines = [NETSCAPE_HEADER] + [row.to_line() for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def session_from_rows(rows: list[CookieRow]) -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = stream_dl.DEFAULT_USER_AGENT
    for row in rows:
        session.cookies.set(
            row.name,
            row.value,
            domain=row.domain,
            path=row.path or "/",
            secure=row.secure.upper() == "TRUE",
        )
    return session


def merge_session_cookies(rows: list[CookieRow], session: requests.Session) -> list[str]:
    """Cập nhật rows theo cookie hiện có trong session; trả về tên các cookie đã đổi giá trị."""
    changed: list[str] = []
    for cookie in session.cookies:
        match = next(
            (
                row for row in rows
                if row.name == cookie.name
                and row.domain.lstrip(".") == cookie.domain.lstrip(".")
                and (row.path or "/") == cookie.path
            ),
            None,
        )
        if match is None:
            rows.append(
                CookieRow(
                    domain=cookie.domain,
                    include_subdomains="TRUE" if cookie.domain.startswith(".") else "FALSE",
                    path=cookie.path,
                    secure="TRUE" if cookie.secure else "FALSE",
                    expires=str(cookie.expires or 0),
                    name=cookie.name,
                    value=cookie.value or "",
                )
            )
            changed.append(cookie.name)
            continue
        if match.value != cookie.value:
            match.value = cookie.value or ""
            if cookie.expires:
                match.expires = str(cookie.expires)
            changed.append(cookie.name)
    return changed


def is_signed_in(session: requests.Session, timeout: int = 30) -> bool:
    response = session.get(LOGIN_CHECK_URL, timeout=timeout, allow_redirects=False)
    location = response.headers.get("Location", "")
    response.close()
    if response.status_code in (301, 302, 303, 307) and (
        "ServiceLogin" in location or "accounts.google.com" in location
    ):
        return False
    return response.status_code < 400


def rotate_cookies(session: requests.Session, timeout: int = 30) -> bool:
    """Gọi RotateCookies; True nếu Google cấp __Secure-1PSIDTS mới."""
    before = {c.value for c in session.cookies if c.name == ROTATED_COOKIE}
    response = session.post(
        ROTATE_COOKIES_URL,
        headers={"Content-Type": "application/json", "Origin": "https://accounts.google.com"},
        data='[000,"-0000000000000000000"]',
        timeout=timeout,
        allow_redirects=False,
    )
    response.close()
    if response.status_code != 200:
        print(f"RotateCookies trả HTTP {response.status_code}.")
        return False
    after = {c.value for c in session.cookies if c.name == ROTATED_COOKIE}
    return bool(after - before)


def write_github_output(**values: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as f:
        for key, value in values.items():
            f.write(f"{key}={value}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cookie-file", type=Path, required=True)
    parser.add_argument("--rotate", action="store_true", help="Gọi RotateCookies và ghi đè file cookie nếu có thay đổi.")
    args = parser.parse_args(argv)

    rows = read_netscape(args.cookie_file)
    if not rows:
        # Cookie dạng JSON / raw header vẫn dùng được cho sync, chỉ là không kiểm tra/làm mới ở đây.
        print("::warning::Cookie không ở dạng Netscape cookies.txt — bỏ qua kiểm tra và làm mới cookie.")
        write_github_output(rotated="0")
        return 0

    session = session_from_rows(rows)
    if not is_signed_in(session):
        print("Cookie đã bị Google đăng xuất. Cần xuất cookie mới và cập nhật secret DRIVE_COOKIE.")
        write_github_output(signed_in="0", rotated="0")
        return 0

    rotated = False
    if args.rotate:
        rotated = rotate_cookies(session)
        if rotated:
            changed = merge_session_cookies(rows, session)
            for row in rows:
                if row.name in changed and row.value:
                    print(f"::add-mask::{row.value}")
            write_netscape(args.cookie_file, rows)
            print(f"Đã làm mới cookie ({', '.join(sorted(set(changed)))}).")
        else:
            print("Google chưa cấp cookie mới (phiên vẫn sống) — giữ nguyên cookie cũ.")

    print("Cookie còn đăng nhập.")
    write_github_output(signed_in="1", rotated="1" if rotated else "0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
