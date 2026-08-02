"""
[worm gpt - CAC] BATCH COOKIE PDF DOWNLOADER
=============================================
Tool DOC LAP download hang loat file PDF bi Google Drive block bang cookie browser.
Ho tro ca cookie browser VA OAuth2 token (token.json).

FALLBACK CHAIN (thu tu uu tien):
  1. HTTP direct download qua /uc?export=download + cookie session
  2. Google Workspace export (Docs/Sheets/Slides -> PDF)
  3. Playwright viewer capture (OAuth token inject + scroll scrollable div):
     - Capture tat ca page images tu drive.google.com/viewer/img
     - Fallback: JS canvas capture tu blob images trong DOM
  4. Playwright screenshot (last resort)

AUTH:
  - token.json (OAuth2): duoc uu tien, tu dong refresh, hoat dong ca khi cookie het han
  - cookie.txt / cookies.json: fallback khi khong co token.json

YEU CAU:
  pip install requests playwright Pillow tqdm google-auth google-auth-oauthlib google-api-python-client
  playwright install chromium

SU DUNG:
  python batch_cookie_downloader.py --cookie cookies.json --input list.txt
  python batch_cookie_downloader.py --input blocked_pdfs.txt          # dung token.json mac dinh
  python batch_cookie_downloader.py --id FILE_ID1 FILE_ID2 ...

INPUT FORMAT (list.txt):
  Mot dong 1 file, co the la:
    - Google Drive File ID
    - URL: https://drive.google.com/file/d/FILE_ID/view
    - ID + ten: FILE_ID ten_file.pdf
"""

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
import traceback

# Fix Unicode output on Windows (Vietnamese filenames)
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf-8-sig"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr.encoding and sys.stderr.encoding.lower() not in ("utf-8", "utf-8-sig"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ============================================================
# CONSTANTS
# ============================================================
DRIVE_UC_URL = "https://drive.google.com/uc"
DRIVE_VIEW_URL = "https://drive.google.com/file/d/{file_id}/view"
DRIVE_PREVIEW_URL = "https://drive.google.com/file/d/{file_id}/preview"
DRIVE_VIDEO_INFO_URL = "https://drive.google.com/get_video_info"
DEFAULT_TOKEN_PATH = "token.json"

# CSS class of the Drive PDF viewer's scrollable container.
# Detected empirically; may change — code falls back to dynamic detection.
VIEWER_SCROLL_CLASS = "ndfHFb-c4YZDc-cYSp0e-s2gQvd"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

# ============================================================
# OAUTH TOKEN LOADER
# ============================================================

def load_oauth_token(token_path: str = DEFAULT_TOKEN_PATH) -> Optional[str]:
    """Load and refresh OAuth2 access token from token.json. Returns bearer token string or None."""
    if not os.path.exists(token_path):
        return None
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request as GRequest
        with open(token_path, "r", encoding="utf-8") as f:
            info = json.load(f)
        creds = Credentials.from_authorized_user_info(info)
        if not creds.valid and creds.expired and creds.refresh_token:
            creds.refresh(GRequest())
            # Persist refreshed token
            with open(token_path, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
        return creds.token if creds.valid else None
    except Exception as e:
        print(f"  [WARN] OAuth token load failed: {e}", file=sys.stderr)
        return None


# ============================================================
# IMAGE → PDF ASSEMBLY
# ============================================================

def _pdf_from_images(images: list[bytes], output_path: Path, fmt: str = "webp") -> bool:
    """Convert list of image bytes (jpeg/webp/png) to a PDF file.
    Preserves original aspect ratio. Returns True on success."""
    from PIL import Image
    import io

    if not images:
        return False

    pil_imgs = []
    for raw in images:
        try:
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            pil_imgs.append(img)
        except Exception as e:
            print(f"    [WARN] skipping corrupt image: {e}")

    if not pil_imgs:
        return False

    output_path = output_path.with_suffix(".pdf")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pil_imgs[0].save(
        str(output_path),
        save_all=True,
        append_images=pil_imgs[1:],
        quality=95,
    )
    return output_path.stat().st_size > 5000


def _validate_pdf(path: Path, expected_size_bytes: int = 0, min_pages: int = 1) -> bool:
    """Sanity-check a downloaded file. Returns True if it looks like real content."""
    if not path.exists():
        return False
    size = path.stat().st_size
    if size < 10_000:          # < 10KB is almost certainly junk
        return False
    # If we know original size, output should be at least 15% of it (images compress differently)
    if expected_size_bytes > 0 and size < expected_size_bytes * 0.15:
        return False
    return True


EXPORT_MIME_MAP = {
    "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.spreadsheet": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.drawing": ("application/pdf", ".pdf"),
}

MIME_TO_EXT = {
    "application/pdf": ".pdf",
    "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
    "image/webp": ".webp", "image/svg+xml": ".svg",
    "text/plain": ".txt", "text/html": ".html", "text/csv": ".csv",
    "application/json": ".json",
}

CHECKPOINT_FILE = "batch_downloader_checkpoint.json"


# ============================================================
# COOKIE LOADING
# ============================================================

def load_cookies_json(path: str) -> dict:
    """Load cookies from EditThisCookie/Cookie-Editor JSON format."""
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    items = raw if isinstance(raw, list) else raw.get("cookies", raw.get("data", []))
    cookie_dict = {}
    for c in items:
        name = c.get("name", "")
        value = c.get("value", "")
        cookie_dict[name] = value
    return cookie_dict


def load_cookies_netscape(path: str) -> dict:
    """Load cookies from Netscape/Mozilla cookies.txt format."""
    cookie_dict = {}
    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            elif line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                domain, _, _, _, _, name, value = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5], parts[6]
                if any(d in domain for d in ["google.com", "googleusercontent.com"]):
                    cookie_dict[name] = value
    return cookie_dict


def load_cookies(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Cookie file khong ton tai: {path}")
    text = p.read_text(encoding="utf-8-sig").strip()
    if text.startswith("[") or text.startswith('{"cookies"'):
        return load_cookies_json(path)
    elif text.startswith("# Netscape") or "\t.TRUE\t" in text or "\t.FALSE\t" in text:
        return load_cookies_netscape(path)
    else:
        raise ValueError(f"Khong nhan dang duoc format cookie file: {path}")


def build_cookie_header(cookie_dict: dict) -> str:
    """Build Cookie header string from dict."""
    return "; ".join(f"{k}={v}" for k, v in cookie_dict.items() if v)


def create_session(cookie_path: str) -> requests.Session:
    cookie_dict = load_cookies(cookie_path)

    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,vi;q=0.8",
        "Cookie": build_cookie_header(cookie_dict),
    })

    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    return session


# ============================================================
# GOOGLE DRIVE HTML PARSING
# ============================================================

def parse_confirm_form(html: str) -> Optional[str]:
    """Parse Google Drive virus scan confirmation form, return download action URL."""
    action_match = re.search(r'action="([^"]*)"', html)
    if not action_match:
        action_match = re.search(r'action=\'([^\']*)\'', html)

    if action_match:
        action = action_match.group(1)
        action = action.replace("&amp;", "&")

        input_pattern = re.findall(r'<input\s+[^>]*name="(.*?)"[^>]*value="(.*?)"[^>]*>', html)
        params = {name: value for name, value in input_pattern} if input_pattern else {}

        if "confirm" in html.lower():
            if params:
                param_str = "&".join(f"{k}={v}" for k, v in params.items())
                return f"{action}?{param_str}" if "?" not in action else f"{action}&{param_str}"
            return action

        if "download_warning" in html.lower():
            return action

    return None


def extract_title_from_html(html: str) -> str:
    m = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE)
    return m.group(1).strip() if m else "Unknown"


def looks_like_auth_block(html: str) -> bool:
    return any(kw in html.lower() for kw in [
        "permission denied", "access denied", "sign in", "login needed",
        "you need access", "request access", "need permission",
    ])


def looks_like_blocked_download(html: str) -> bool:
    """Detect if the response indicates download is disabled by owner."""
    lower = html.lower()
    return any(kw in lower for kw in [
        "cannot download", "download is disabled", "download not available",
        "download permission", "owner has disabled downloads",
    ])


# ============================================================
# FILE INFO FETCHING
# ============================================================

def get_file_metadata(session: requests.Session, file_id: str) -> dict:
    """Try to get file metadata via Drive API or HTML page."""
    info = {"id": file_id, "name": file_id, "mimeType": "application/pdf", "size": 0}

    # Try API first (might fail with 403 for blocked files)
    try:
        resp = session.get(
            f"https://www.googleapis.com/drive/v3/files/{file_id}",
            params={"fields": "name,mimeType,size", "supportsAllDrives": "true"},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            info["name"] = data.get("name", file_id)
            info["mimeType"] = data.get("mimeType", "application/pdf")
            info["size"] = int(data.get("size", 0))
            return info
    except Exception:
        pass

    # Fallback: fetch the view page and extract title
    try:
        resp = session.get(DRIVE_VIEW_URL.format(file_id=file_id), timeout=20)
        if resp.status_code == 200:
            title = extract_title_from_html(resp.text)
            if title and title != "Unknown" and "Google Drive" not in title:
                info["name"] = title.replace(" - Google Drive", "").strip()

            if "application/vnd.google-apps.spreadsheet" in resp.text.lower():
                info["mimeType"] = "application/vnd.google-apps.spreadsheet"
            elif "application/vnd.google-apps.document" in resp.text.lower():
                info["mimeType"] = "application/vnd.google-apps.document"
            elif "application/vnd.google-apps.presentation" in resp.text.lower():
                info["mimeType"] = "application/vnd.google-apps.presentation"
    except Exception:
        pass

    return info


# ============================================================
# DOWNLOAD METHODS
# ============================================================

def download_direct_uc(session: requests.Session, file_id: str, output_path: Path) -> bool:
    """Try direct download via /uc?export=download URL with cookie session."""
    url = f"{DRIVE_UC_URL}?export=download&id={file_id}"
    resp = session.get(url, allow_redirects=True, timeout=30, stream=True)

    # Handle confirmation page
    if "text/html" in resp.headers.get("Content-Type", ""):
        text = resp.text[:10000]

        if looks_like_auth_block(text):
            return False

        confirm_url = parse_confirm_form(text)
        if confirm_url:
            parsed = urlparse(url)
            if confirm_url.startswith("/"):
                confirm_url = f"{parsed.scheme}://{parsed.netloc}{confirm_url}"
            resp = session.get(confirm_url, allow_redirects=True, timeout=30, stream=True)

    # Check final response
    ct = resp.headers.get("Content-Type", "")
    if "text/html" in ct:
        snippet = resp.text[:5000].lower()
        if looks_like_blocked_download(snippet) or "sign in" in snippet:
            return False
        content_disposition = resp.headers.get("Content-Disposition", "")
        if "attachment" not in content_disposition.lower():
            return False

    if resp.status_code in (200, 206) and "application/pdf" in ct or "application/octet-stream" in ct:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(output_path), "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        return True

    return False


def download_workspace_export(session: requests.Session, file_id: str, mime_type: str,
                              output_path: Path) -> bool:
    """Export Google Workspace file to PDF."""
    if mime_type not in EXPORT_MIME_MAP:
        return False

    export_mime, ext = EXPORT_MIME_MAP[mime_type]
    output_path = output_path.with_suffix(ext)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    export_url = f"https://www.googleapis.com/drive/v3/files/{file_id}/export"
    resp = session.get(export_url, params={"mimeType": export_mime}, timeout=60, stream=True)

    if resp.status_code == 200:
        with open(str(output_path), "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        return True

    return False


def _images_to_pdf(images: list, output_path: Path):
    """Legacy helper: (page_num, bytes) list -> PDF. Kept for backward compat."""
    ordered = [data for _, data in sorted(images, key=lambda x: x[0])]
    _pdf_from_images(ordered, output_path)


def _find_scrollable_div_js() -> str:
    """JS snippet: return the best scrollable element selector in the viewer."""
    return """
        () => {
            // Try known Drive viewer scroll container class first
            const known = document.querySelector('.ndfHFb-c4YZDc-cYSp0e-s2gQvd');
            if (known && known.scrollHeight > known.clientHeight + 200) return 'known';

            // Fallback: find any large scrollable div
            const candidates = [...document.querySelectorAll('div')]
                .filter(e => e.scrollHeight > 3000 && e.clientHeight > 400)
                .sort((a, b) => b.scrollHeight - a.scrollHeight);
            return candidates.length ? 'fallback' : 'none';
        }
    """


def _scroll_and_capture_js(max_pages: int, scroll_step: int, delay_ms: int) -> str:
    """JS that scrolls the viewer container and captures all blob images."""
    return f"""
        async () => {{
            const d = ms => new Promise(r => setTimeout(r, ms));

            // Find the scrollable viewer container
            let scroller = document.querySelector('.{VIEWER_SCROLL_CLASS}');
            if (!scroller || scroller.scrollHeight <= scroller.clientHeight + 50) {{
                scroller = [...document.querySelectorAll('div')]
                    .filter(e => e.scrollHeight > 3000 && e.clientHeight > 400)
                    .sort((a, b) => b.scrollHeight - a.scrollHeight)[0];
            }}
            if (!scroller) scroller = document.body;

            window.__captured_blobs = window.__captured_blobs || new Set();
            window.__blob_data = window.__blob_data || [];

            const captureVisible = () => {{
                const imgs = document.querySelectorAll('img[src^="blob:"]');
                for (const img of imgs) {{
                    if (img.naturalWidth < 100 || img.naturalHeight < 100) continue;
                    if (window.__captured_blobs.has(img.src)) continue;
                    window.__captured_blobs.add(img.src);
                    try {{
                        const canvas = document.createElement('canvas');
                        canvas.width = img.naturalWidth;
                        canvas.height = img.naturalHeight;
                        canvas.getContext('2d').drawImage(img, 0, 0);
                        const dataUrl = canvas.toDataURL('image/jpeg', 0.92);
                        window.__blob_data.push({{w: img.naturalWidth, h: img.naturalHeight, dataUrl}});
                    }} catch(e) {{
                        window.__blob_data.push({{error: e.toString()}});
                    }}
                }}
            }};

            let stuck = 0;
            for (let i = 0; i < {max_pages} && stuck < 8; i++) {{
                captureVisible();
                const before = scroller.scrollTop;
                scroller.scrollBy(0, {scroll_step});
                await d({delay_ms});
                const after = scroller.scrollTop;
                stuck = Math.abs(after - before) < 3 ? stuck + 1 : 0;
                if (after + scroller.clientHeight >= scroller.scrollHeight - 10) break;
            }}
            // Scroll back to top and capture any remaining pages
            scroller.scrollTo(0, 0);
            await d(1500);
            captureVisible();

            return window.__blob_data;
        }}
    """


def download_playwright_viewer(
    file_id: str,
    mime_type: str,
    output_path: Path,
    cookie_dict: dict,
    headless: bool = True,
    viewport_width: int = 1280,
    viewport_height: int = 900,
    scroll_delay: float = 0.7,
    max_pages: int = 300,
    access_token: Optional[str] = None,
    expected_size: int = 0,
) -> bool:
    """
    Bypass Google Drive 'download disabled' restriction.

    Strategy (2025 Drive viewer):
      1. Load /preview with OAuth Bearer token injected via route interception.
      2. Scroll the inner viewer DIV (ndfHFb-c4YZDc-cYSp0e-s2gQvd) to trigger lazy loading.
      3. Capture page images from drive.google.com/viewer/img network responses (webp).
      4. Fallback: canvas-capture blob images that appear in the DOM during scroll.
      5. Assemble images into a PDF with Pillow.

    If access_token is None, falls back to cookie injection (may fail if cookies expired).
    """
    from playwright.sync_api import sync_playwright

    if mime_type in EXPORT_MIME_MAP:
        return _playwright_export_workspace(file_id, mime_type, output_path, cookie_dict, headless)

    output_path = output_path.with_suffix(".pdf")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            viewport={"width": viewport_width, "height": viewport_height},
            user_agent=USER_AGENT,
        )

        # ----------------------------------------------------------------
        # Auth injection via route:
        #   - OAuth Bearer token takes priority (works even with expired cookies)
        #   - Cookie header as fallback
        # ----------------------------------------------------------------
        cookie_header = build_cookie_header(cookie_dict) if cookie_dict else ""

        def route_handler(route):
            url = route.request.url
            is_google = any(d in url for d in [
                "drive.google.com", "googleusercontent.com", "docs.google.com",
                "googleapis.com", "clients6.google.com",
            ])
            if not is_google:
                route.continue_()
                return
            headers = dict(route.request.headers)
            if access_token:
                headers["Authorization"] = f"Bearer {access_token}"
            elif cookie_header:
                headers["cookie"] = cookie_header
            route.continue_(headers=headers)

        context.route("**/*", route_handler)
        page = context.new_page()

        # ----------------------------------------------------------------
        # Network capture: collect ALL viewer/img responses in order
        # ----------------------------------------------------------------
        net_images: list[bytes] = []
        captured_pdf: Optional[bytes] = None

        def on_response(response):
            nonlocal captured_pdf
            url = response.url
            ct = response.headers.get("content-type", "").lower()

            if "application/pdf" in ct:
                try:
                    body = response.body()
                    if body and len(body) > 5000 and body.startswith(b"%PDF"):
                        captured_pdf = body
                        print(f"      [NET] Raw PDF binary: {len(body):,} bytes")
                except Exception:
                    pass
                return

            # Current Drive viewer endpoint for per-page images
            if ("viewer/img" in url or "drive-viewer/" in url) and \
               any(x in ct for x in ("image/webp", "image/png", "image/jpeg")):
                try:
                    body = response.body()
                    if body and len(body) > 5_000:
                        net_images.append(body)
                        print(f"      [NET] Page img #{len(net_images)}: {len(body):,}b ({ct.split('/')[1].split(';')[0]})")
                except Exception:
                    pass

        page.on("response", on_response)

        try:
            preview_url = DRIVE_PREVIEW_URL.format(file_id=file_id)
            print(f"      [PW] Opening: {preview_url}")
            page.goto(preview_url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(3000)

            # Verify we're not on the sign-in page
            current_url = page.url
            if "accounts.google.com" in current_url:
                print(f"      [WARN] Redirected to sign-in — auth failed. "
                      f"Cookie expired or token invalid.")
                return False

            print(f"      [PW] Page title: {page.title()}")

            # ----------------------------------------------------------------
            # Scroll the viewer container and capture blob images via JS
            # ----------------------------------------------------------------
            print(f"      [PW] Scrolling viewer to capture all pages...")
            seen_blob_srcs: list[str] = []
            js_images: list[bytes] = []

            for step in range(max_pages // 2):
                new_blobs = page.evaluate(f"""
                    (seenSrcs) => {{
                        const results = [];
                        const imgs = document.querySelectorAll('img[src^="blob:"]');
                        for (const img of imgs) {{
                            if (img.naturalWidth < 200 || img.naturalHeight < 200) continue;
                            if (seenSrcs.includes(img.src)) continue;
                            try {{
                                const c = document.createElement('canvas');
                                c.width = img.naturalWidth; c.height = img.naturalHeight;
                                c.getContext('2d').drawImage(img, 0, 0);
                                const dataUrl = c.toDataURL('image/jpeg', 0.92);
                                results.push({{src: img.src, ok: true, data: dataUrl}});
                            }} catch(e) {{
                                results.push({{src: img.src, ok: false, error: e.toString()}});
                            }}
                        }}
                        return results;
                    }}
                """, seen_blob_srcs)

                for item in new_blobs:
                    seen_blob_srcs.append(item["src"])
                    if item.get("ok") and item.get("data"):
                        raw = base64.b64decode(item["data"].split(",", 1)[1])
                        js_images.append(raw)
                        print(f"      [JS] Blob #{len(js_images)}: {len(raw):,}b")
                    elif not item.get("ok"):
                        print(f"      [JS] CORS on blob: {item.get('error','?')[:60]}")

                # Scroll the viewer container
                moved = page.evaluate(f"""
                    () => {{
                        // Try known class first
                        let el = document.querySelector('.{VIEWER_SCROLL_CLASS}');
                        if (!el || el.scrollHeight <= el.clientHeight + 50) {{
                            const divs = [...document.querySelectorAll('div')]
                                .filter(e => e.scrollHeight > 3000 && e.clientHeight > 400);
                            el = divs.sort((a,b)=>b.scrollHeight-a.scrollHeight)[0];
                        }}
                        if (!el) return false;
                        const before = el.scrollTop;
                        el.scrollBy(0, 700);
                        return Math.abs(el.scrollTop - before) > 2;
                    }}
                """)
                page.wait_for_timeout(int(scroll_delay * 1000))

                # Stop if all expected pages captured (use net count as oracle)
                pages_captured = max(len(net_images), len(js_images))
                if not moved and step > 5:
                    print(f"      [PW] Scroll done at step {step} — {pages_captured} pages")
                    break

            # Final wait for any in-flight requests
            page.wait_for_timeout(2000)
            print(f"      [PW] Captured: {len(net_images)} network images, "
                  f"{len(js_images)} JS blob images")

            # ----------------------------------------------------------------
            # Pick best source and assemble PDF
            # ----------------------------------------------------------------

            # Priority 1: Raw PDF binary from network
            if captured_pdf and len(captured_pdf) > 10_000:
                output_path.write_bytes(captured_pdf)
                if _validate_pdf(output_path, expected_size):
                    print(f"      [OK] Raw PDF: {len(captured_pdf):,} bytes")
                    return True
                output_path.unlink(missing_ok=True)

            # Priority 2: Network webp images (highest quality, no re-encoding)
            if len(net_images) >= 1:
                ok = _pdf_from_images(net_images, output_path)
                if ok and _validate_pdf(output_path, expected_size):
                    print(f"      [OK] {len(net_images)} network webp pages → PDF "
                          f"({output_path.stat().st_size:,} bytes)")
                    return True
                output_path.unlink(missing_ok=True)

            # Priority 3: JS blob canvas images
            if len(js_images) >= 1:
                ok = _pdf_from_images(js_images, output_path)
                if ok and _validate_pdf(output_path, expected_size):
                    print(f"      [OK] {len(js_images)} JS blob pages → PDF "
                          f"({output_path.stat().st_size:,} bytes)")
                    return True
                output_path.unlink(missing_ok=True)

            print(f"      [FAIL] No valid page images captured "
                  f"(net={len(net_images)}, js={len(js_images)})")
            return False

        except Exception:
            traceback.print_exc()
            return False
        finally:
            context.close()
            browser.close()


def _playwright_export_workspace(file_id: str, mime_type: str, output_path: Path,
                                 cookie_dict: dict, headless: bool) -> bool:
    from playwright.sync_api import sync_playwright

    export_mime, ext = EXPORT_MIME_MAP[mime_type]
    output_path = output_path.with_suffix(ext)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    export_url = f"https://www.googleapis.com/drive/v3/files/{file_id}/export?mimeType={export_mime.replace('+', '%2B')}"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent=USER_AGENT,
        )
        cookie_header = build_cookie_header(cookie_dict)
        context.set_extra_http_headers({"Cookie": cookie_header})

        page = context.new_page()
        try:
            resp = page.goto(export_url, wait_until="networkidle", timeout=45000)
            if resp and resp.status == 200:
                body = resp.body()
                output_path.write_bytes(body)
                return True
            return False
        except Exception:
            return False
        finally:
            context.close()
            browser.close()


def download_playwright_screenshot(file_id: str, output_path: Path,
                                   cookie_dict: dict, headless: bool = True) -> bool:
    """Last resort: screenshot each rendered page as images, then combine."""
    from playwright.sync_api import sync_playwright

    output_path = output_path.with_suffix(".png")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=USER_AGENT,
        )
        cookie_header = build_cookie_header(cookie_dict)
        context.set_extra_http_headers({"Cookie": cookie_header})

        page = context.new_page()
        try:
            page.goto(DRIVE_VIEW_URL.format(file_id=file_id), wait_until="networkidle", timeout=45000)
            page.wait_for_timeout(3000)
            page.screenshot(path=str(output_path), full_page=True)
            return True
        except Exception:
            return False
        finally:
            context.close()
            browser.close()


# ============================================================
# FALLBACK CHAIN ORCHESTRATOR
# ============================================================

def download_file(
    file_id: str,
    output_dir: Path,
    session: requests.Session,
    cookie_dict: dict,
    headless: bool,
    dry_run: bool = False,
    access_token: Optional[str] = None,
    custom_name: Optional[str] = None,
) -> dict:
    """Execute the full fallback chain for one file."""
    result = {
        "id": file_id,
        "name": file_id,
        "mimeType": "unknown",
        "status": "FAILED",
        "method": "none",
    }

    # Get metadata — try with OAuth token first, then cookie session
    info = get_file_metadata(session, file_id)
    name = custom_name or info["name"]  # Use custom name if provided
    mime_type = info["mimeType"]
    expected_size = info.get("size", 0)
    result["name"] = name
    result["mimeType"] = mime_type

    safe_name = re.sub(r'[<>:"/\\|?*]', '_', name).strip()[:200]
    output_path = output_dir / safe_name

    if dry_run:
        result["status"] = "DRY_RUN"
        return result

    print(f"  [*] {name} ({mime_type}, {int(expected_size)//1024}KB expected)")

    # --- Step 1: Direct UC download ---
    print(f"      [1/4] Trying direct download...")
    try:
        if download_direct_uc(session, file_id, output_path):
            if _validate_pdf(output_path.with_suffix(".pdf") if not output_path.suffix else output_path,
                             expected_size):
                result["status"] = "OK"
                result["method"] = "direct_uc"
                result["path"] = str(output_path)
                print(f"      [OK] Direct UC — {output_path.stat().st_size:,} bytes")
                return result
    except Exception as e:
        print(f"      [FAIL] Direct download error: {e}")

    # --- Step 2: Workspace export ---
    if mime_type in EXPORT_MIME_MAP:
        print(f"      [2/4] Trying workspace export...")
        try:
            if download_workspace_export(session, file_id, mime_type, output_path):
                result["status"] = "OK"
                result["method"] = "workspace_export"
                result["path"] = str(output_path)
                print(f"      [OK] Workspace export")
                return result
        except Exception as e:
            print(f"      [FAIL] Workspace export error: {e}")
    else:
        print(f"      [2/4] Skipped (not workspace file)")

    # --- Step 3: Playwright viewer capture (new implementation) ---
    print(f"      [3/4] Playwright viewer capture (token={'YES' if access_token else 'NO'})...")
    try:
        if download_playwright_viewer(
            file_id, mime_type, output_path, cookie_dict, headless,
            access_token=access_token, expected_size=expected_size,
        ):
            result["status"] = "OK"
            result["method"] = "playwright_viewer"
            result["path"] = str(output_path.with_suffix(".pdf"))
            return result
    except Exception as e:
        print(f"      [FAIL] Playwright viewer error: {e}")
        traceback.print_exc()

    # --- Step 4: Playwright screenshot (last resort) ---
    print(f"      [4/4] Playwright screenshot fallback...")
    try:
        if download_playwright_screenshot(file_id, output_path, cookie_dict, headless):
            result["status"] = "OK"
            result["method"] = "playwright_screenshot"
            result["path"] = str(output_path)
            print(f"      [OK] Screenshot saved")
            return result
    except Exception as e:
        print(f"      [FAIL] Screenshot error: {e}")

    print(f"      [DEAD] All methods failed for: {name}")
    return result


# ============================================================
# INPUT PARSING
# ============================================================

def parse_file_id(raw: str) -> str:
    """Extract file ID from various input formats."""
    raw = raw.strip()

    # Direct ID (alphanumeric, dash, underscore, 20-50 chars)
    if re.match(r'^[a-zA-Z0-9_-]{20,50}$', raw.split()[0]):
        return raw.split()[0]

    # URL formats
    patterns = [
        r'/d/([a-zA-Z0-9_-]{20,50})',       # /d/FILE_ID
        r'[?&]id=([a-zA-Z0-9_-]{20,50})',    # ?id=FILE_ID
        r'/folders/([a-zA-Z0-9_-]{20,50})',   # /folders/FOLDER_ID
        r'/open\?id=([a-zA-Z0-9_-]{20,50})',  # /open?id=FILE_ID
    ]
    for pat in patterns:
        m = re.search(pat, raw)
        if m:
            return m.group(1)

    return raw.split()[0] if raw else ""


def parse_custom_name(raw: str) -> Optional[str]:
    """Extract custom filename if provided (format: ID name.pdf)."""
    parts = raw.strip().split(None, 1)
    if len(parts) == 2 and not parts[1].startswith("http"):
        name = parts[1].strip()
        forbidden = '<>:"/\\|?*'
        for ch in forbidden:
            name = name.replace(ch, "_")
        return name
    return None


def load_input_list(path: str) -> list:
    """Load file IDs from a text file."""
    items = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                fid = parse_file_id(line)
                cname = parse_custom_name(line)
                if fid:
                    items.append((fid, cname))
    return items


# ============================================================
# CHECKPOINT
# ============================================================

def load_checkpoint() -> dict:
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_checkpoint(data: dict):
    with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="[worm gpt - CAC] Batch Cookie PDF Downloader - Bypass Google Drive block",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EXAMPLES:
  # Dung OAuth token (khuyen nghi - khong can cookie):
  python batch_cookie_downloader.py --input blocked_pdfs.txt
  python batch_cookie_downloader.py --id FILE_ID1 FILE_ID2

  # Dung cookie browser:
  python batch_cookie_downloader.py --cookie cookies.json --input list.txt
  python batch_cookie_downloader.py --cookie cookies.txt --id ABC123 --show-browser
        """,
    )
    parser.add_argument("--cookie", default=None,
                        help="Cookie file (JSON or Netscape). Optional if token.json exists.")
    parser.add_argument("--token", default=DEFAULT_TOKEN_PATH,
                        help=f"OAuth token file (default: {DEFAULT_TOKEN_PATH})")
    parser.add_argument("--input", help="File with Google Drive IDs/URLs (one per line)")
    parser.add_argument("--id", nargs="+", help="Google Drive file IDs or URLs to download")
    parser.add_argument("--output", "-o", default="./downloaded_pdfs",
                        help="Output directory (default: ./downloaded_pdfs)")
    parser.add_argument("--workers", "-w", type=int, default=1,
                        help="Parallel downloads (default: 1; >1 requires --cookie auth)")
    parser.add_argument("--show-browser", action="store_true",
                        help="Show Playwright browser window (disables parallel)")
    parser.add_argument("--retry", type=int, default=3, help="Retries per file (default: 3)")
    parser.add_argument("--dry-run", action="store_true", help="List files without downloading")
    parser.add_argument("--no-resume", action="store_true",
                        help="Ignore checkpoint, re-download all")
    parser.add_argument("--reset-failed", action="store_true",
                        help="Remove FAILED entries from checkpoint so they get retried")

    args = parser.parse_args()

    # Must have at least one source
    if not args.input and not args.id:
        print("[!] No file IDs provided. Use --input or --id.")
        parser.print_help()
        sys.exit(1)

    # Collect file IDs
    items = []
    if args.input:
        items = load_input_list(args.input)
    if args.id:
        for raw in args.id:
            fid = parse_file_id(raw)
            cname = parse_custom_name(raw)
            if fid:
                items.append((fid, cname))

    # Deduplicate
    seen: set[str] = set()
    items = [(fid, cn) for fid, cn in items if not seen.__contains__(fid) and not seen.add(fid)]  # type: ignore

    # ----------------------------------------------------------------
    # Auth setup
    # ----------------------------------------------------------------
    access_token: Optional[str] = load_oauth_token(args.token)

    cookie_dict: dict = {}
    if args.cookie:
        try:
            cookie_dict = load_cookies(args.cookie)
        except Exception as e:
            print(f"[WARN] Could not load cookie file: {e}")

    if not access_token and not cookie_dict:
        print("[ERROR] No valid auth: token.json not found/expired and no --cookie provided.")
        sys.exit(1)

    # ----------------------------------------------------------------
    # Session for HTTP requests (metadata, direct download)
    # ----------------------------------------------------------------
    if args.cookie:
        session = create_session(args.cookie)
    else:
        # Minimal session with OAuth header
        session = requests.Session()
        session.headers.update({
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {access_token}",
        })
        retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503])
        session.mount("https://", HTTPAdapter(max_retries=retry))
        session.mount("http://",  HTTPAdapter(max_retries=retry))

    # ----------------------------------------------------------------
    # Summary
    # ----------------------------------------------------------------
    print("=" * 60)
    print(f"[worm gpt - CAC] BATCH COOKIE PDF DOWNLOADER")
    print(f"  Total files: {len(items)}")
    print(f"  Output dir:  {args.output}")
    print(f"  Auth:        {'OAuth token [OK]' if access_token else ''}  "
          f"{'Cookies (' + str(len(cookie_dict)) + ')' if cookie_dict else ''}")
    print(f"  Headless:    {not args.show_browser}")
    print(f"  Workers:     {args.workers}")
    print("=" * 60)

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    headless = not args.show_browser

    # ----------------------------------------------------------------
    # Checkpoint
    # ----------------------------------------------------------------
    checkpoint = {} if args.no_resume else load_checkpoint()
    if args.reset_failed:
        removed = {k: v for k, v in checkpoint.items() if v == "FAILED"}
        for k in removed: del checkpoint[k]
        if removed:
            save_checkpoint(checkpoint)
            print(f"[*] Reset {len(removed)} FAILED entries from checkpoint")
    if checkpoint:
        n_ok = sum(1 for v in checkpoint.values() if v == "OK")
        n_fail = sum(1 for v in checkpoint.values() if v == "FAILED")
        print(f"[*] Checkpoint: {n_ok} OK, {n_fail} FAILED, "
              f"{len(items) - n_ok - n_fail} pending")

    # ----------------------------------------------------------------
    # Processing
    # ----------------------------------------------------------------
    results = []
    success = 0
    failed = 0
    skipped = 0

    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading
    ckpt_lock = threading.Lock()

    def process_one(fid, cname):
        nonlocal access_token
        if fid in checkpoint and checkpoint[fid] == "OK":
            return {"id": fid, "name": cname or fid, "status": "SKIPPED",
                    "method": "checkpoint", "mimeType": "unknown"}

        # Refresh token if needed before Playwright step
        tok = load_oauth_token(args.token) or access_token

        for attempt in range(args.retry):
            r = download_file(
                fid, output_dir, session, cookie_dict, headless,
                dry_run=args.dry_run, access_token=tok,
                custom_name=cname,
            )
            if r["status"] == "OK" or args.dry_run:
                with ckpt_lock:
                    checkpoint[fid] = r["status"]
                    save_checkpoint(checkpoint)
                return r
            if attempt < args.retry - 1:
                wait = 3 * (attempt + 1)
                print(f"      Retry {attempt+2}/{args.retry} in {wait}s...")
                time.sleep(wait)

        with ckpt_lock:
            checkpoint[fid] = "FAILED"
            save_checkpoint(checkpoint)
        return r

    print(f"\n[*] Starting download...\n")

    # Workers > 1 only makes sense with cookie auth; token-based Playwright can't parallelise well
    effective_workers = args.workers if (not args.show_browser) else 1

    if effective_workers > 1:
        with ThreadPoolExecutor(max_workers=effective_workers) as executor:
            futures = {executor.submit(process_one, fid, cname): (fid, cname) for fid, cname in items}
            for i, future in enumerate(as_completed(futures)):
                fid, cname = futures[future]
                try:
                    r = future.result()
                    results.append(r)
                except Exception as e:
                    results.append({"id": fid, "name": cname or fid, "status": "ERROR", "error": str(e)})

                if r["status"] == "OK":
                    success += 1
                elif r["status"] == "SKIPPED":
                    skipped += 1
                else:
                    failed += 1

                if (i + 1) % 5 == 0:
                    print(f"  ... {i + 1}/{len(items)} ({success} ok, {failed} fail, {skipped} skip)")
    else:
        for i, (fid, cname) in enumerate(items):
            r = process_one(fid, cname)
            results.append(r)

            if r["status"] == "OK":
                success += 1
            elif r["status"] == "SKIPPED":
                skipped += 1
            else:
                failed += 1

    # Summary
    print(f"\n{'=' * 60}")
    print(f"KET QUA: {success} OK | {failed} FAILED | {skipped} SKIPPED | {len(items)} TOTAL")
    print(f"Output: {args.output}")
    print(f"Checkpoint: {CHECKPOINT_FILE}")
    print(f"{'=' * 60}")

    # Print failed items
    if failed > 0:
        print("\nFAILED FILES:")
        for r in results:
            if r["status"] not in ("OK", "SKIPPED", "DRY_RUN"):
                print(f"  - {r['id']} : {r.get('name', '?')} ({r['status']})")


if __name__ == "__main__":
    main()
