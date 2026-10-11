import unittest
from unittest.mock import patch

import requests

import windows_sync_tool_improved as sync_tool


FILE_ID = "15CEk1ruSaP5VG4vpQJV1cV7husI6Aqfs"


class FakeResponse:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self._payload

    def close(self):
        pass


def playback_payload():
    return {
        "mediaStreamingData": {
            "formatStreamingData": {
                "progressiveTranscodes": [
                    {"itag": 18, "url": "https://rr1.c.drive.google.com/videoplayback?itag=18"},
                    {"itag": 37, "url": "https://rr1.c.drive.google.com/videoplayback?itag=37"},
                ],
                "adaptiveTranscodes": [
                    {"itag": 137, "url": "https://rr1.c.drive.google.com/videoplayback?itag=137"},
                ],
            }
        }
    }


def make_session():
    session = requests.Session()
    session.headers["User-Agent"] = "UA-test"
    session.cookies.set("SAPISID", "sapisid-value", domain=".google.com")
    return session


class PlaybackSourceTests(unittest.TestCase):
    def test_uses_playback_api_and_picks_best_progressive_stream(self):
        session = make_session()
        calls = []

        def fake_get(url, **kwargs):
            calls.append((url, kwargs))
            if "/v1/drive/media/" in url:
                return FakeResponse(200, playback_payload())
            return FakeResponse(206, headers={"Content-Range": "bytes 0-0/1234"})

        with patch.object(session, "get", side_effect=fake_get), \
                patch.object(sync_tool.UI, "status"):
            source = sync_tool.StreamDownloader(session).get_video_source(FILE_ID)

        playback_url, playback_kwargs = calls[0]
        self.assertIn(f"/v1/drive/media/{FILE_ID}/playback", playback_url)
        self.assertTrue(
            playback_kwargs["headers"]["Authorization"].startswith("SAPISIDHASH ")
        )
        self.assertEqual(source.itag, "37")
        self.assertEqual(source.size, 1234)
        # URL luồng gắn với UA của request playback.
        self.assertEqual(source.headers["User-Agent"], "UA-test")

    def test_tries_next_authuser_on_permission_denied(self):
        session = make_session()
        authusers = []

        def fake_get(url, **kwargs):
            if "/v1/drive/media/" in url:
                authuser = kwargs["headers"]["X-Goog-AuthUser"]
                authusers.append(authuser)
                if authuser == "0":
                    return FakeResponse(403, {"error": {"message": "permission denied"}})
                return FakeResponse(200, playback_payload())
            return FakeResponse(206, headers={"Content-Range": "bytes 0-0/10"})

        with patch.object(session, "get", side_effect=fake_get), \
                patch.object(sync_tool.UI, "status"):
            sync_tool.StreamDownloader(session).get_video_source(FILE_ID)

        self.assertEqual(authusers, ["0", "1"])

    def test_permission_error_does_not_look_like_expired_cookie(self):
        session = make_session()

        with patch.object(
            session, "get",
            return_value=FakeResponse(403, {"error": {"message": "permission denied"}}),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                sync_tool.StreamDownloader(session).get_video_source(FILE_ID)

        self.assertFalse(sync_tool.looks_like_cookie_auth_error(ctx.exception))

    def test_unauthenticated_error_triggers_cookie_refresh(self):
        session = make_session()

        with patch.object(session, "get", return_value=FakeResponse(401)):
            with self.assertRaises(RuntimeError) as ctx:
                sync_tool.StreamDownloader(session).get_video_source(FILE_ID)

        self.assertTrue(sync_tool.looks_like_cookie_auth_error(ctx.exception))

    def test_sends_resource_key_header(self):
        session = make_session()
        seen = {}

        def fake_get(url, **kwargs):
            if "/v1/drive/media/" in url:
                seen.update(kwargs["headers"])
                return FakeResponse(200, playback_payload())
            return FakeResponse(206, headers={"Content-Range": "bytes 0-0/10"})

        with patch.object(session, "get", side_effect=fake_get), \
                patch.object(sync_tool.UI, "status"):
            sync_tool.StreamDownloader(session).get_video_source(FILE_ID, "rk-123")

        self.assertEqual(seen["X-Goog-Drive-Resource-Keys"], f"{FILE_ID}/rk-123")

    def test_unexpected_status_stops_without_crashing(self):
        session = make_session()
        authusers = []

        def fake_get(url, **kwargs):
            authusers.append(kwargs["headers"]["X-Goog-AuthUser"])
            return FakeResponse(500)

        with patch.object(session, "get", side_effect=fake_get):
            with self.assertRaises(sync_tool.stream_dl.DrivePlaybackError) as ctx:
                sync_tool.stream_dl.fetch_drive_playback_streams(session, FILE_ID)

        self.assertEqual(authusers, ["0"])
        self.assertEqual(ctx.exception.status_codes, [500])

    def test_missing_sapisid_is_reported(self):
        session = requests.Session()

        with self.assertRaises(sync_tool.stream_dl.DownloadError):
            sync_tool.stream_dl.fetch_drive_playback_streams(session, FILE_ID)


if __name__ == "__main__":
    unittest.main()
