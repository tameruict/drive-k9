import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cookie_maintenance as cm


COOKIE_TEXT = "\n".join([
    "# Netscape HTTP Cookie File",
    ".google.com\tTRUE\t/\tTRUE\t1824854820\tSAPISID\tsapisid-old",
    ".google.com\tTRUE\t/\tTRUE\t1823211128\t__Secure-1PSIDTS\tsidts-old",
    "#HttpOnly_.google.com\tTRUE\t/\tTRUE\t1824854820\tHSID\thsid-old",
    "drive.google.com\tFALSE\t/\tTRUE\t1825686656\tOSID\tosid-old",
    "",
])


class FakeResponse:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}

    def close(self):
        pass


class CookieMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "cookie.txt"
        self.path.write_text(COOKIE_TEXT, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_netscape_round_trip_keeps_httponly_prefix(self):
        rows = cm.read_netscape(self.path)
        cm.write_netscape(self.path, rows)

        self.assertEqual(self.path.read_text(encoding="utf-8"), COOKIE_TEXT)

    def test_signed_out_redirect_is_detected(self):
        session = cm.session_from_rows(cm.read_netscape(self.path))
        redirect = FakeResponse(302, {"Location": "https://accounts.google.com/ServiceLogin?service=wise"})

        with patch.object(session, "get", return_value=redirect):
            self.assertFalse(cm.is_signed_in(session))
        with patch.object(session, "get", return_value=FakeResponse(200)):
            self.assertTrue(cm.is_signed_in(session))

    def test_rotation_rewrites_only_changed_cookie(self):
        session = cm.session_from_rows(cm.read_netscape(self.path))

        def fake_post(url, **kwargs):
            session.cookies.set("__Secure-1PSIDTS", "sidts-new", domain=".google.com", path="/", secure=True)
            return FakeResponse(200)

        with patch.object(cm, "session_from_rows", return_value=session), \
                patch.object(cm, "is_signed_in", return_value=True), \
                patch.object(session, "post", side_effect=fake_post):
            self.assertEqual(cm.main(["--cookie-file", str(self.path), "--rotate"]), 0)

        text = self.path.read_text(encoding="utf-8")
        self.assertIn("__Secure-1PSIDTS\tsidts-new", text)
        self.assertNotIn("sidts-old", text)
        self.assertIn("#HttpOnly_.google.com\tTRUE\t/\tTRUE\t1824854820\tHSID\thsid-old", text)
        self.assertIn("OSID\tosid-old", text)

    def test_no_new_cookie_leaves_file_untouched(self):
        session = cm.session_from_rows(cm.read_netscape(self.path))

        with patch.object(cm, "session_from_rows", return_value=session), \
                patch.object(cm, "is_signed_in", return_value=True), \
                patch.object(session, "post", return_value=FakeResponse(200)):
            cm.main(["--cookie-file", str(self.path), "--rotate"])

        self.assertEqual(self.path.read_text(encoding="utf-8"), COOKIE_TEXT)

    def test_github_output_reports_signed_out(self):
        output = Path(self.tmp.name) / "out.txt"

        with patch.dict("os.environ", {"GITHUB_OUTPUT": str(output)}), \
                patch.object(cm, "is_signed_in", return_value=False):
            cm.main(["--cookie-file", str(self.path), "--rotate"])

        self.assertEqual(output.read_text(encoding="utf-8"), "signed_in=0\nrotated=0\n")

    def test_non_netscape_cookie_is_skipped_without_failing(self):
        output = Path(self.tmp.name) / "out.txt"
        self.path.write_text('[{"name": "SID", "value": "x"}]', encoding="utf-8")

        with patch.dict("os.environ", {"GITHUB_OUTPUT": str(output)}):
            self.assertEqual(cm.main(["--cookie-file", str(self.path), "--rotate"]), 0)

        self.assertNotIn("signed_in", output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
