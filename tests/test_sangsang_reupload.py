import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import sangsang_reupload as app


COURSE = {
    "title": "Khóa thử",
    "sections": [
        {
            "title": "Nhóm chính",
            "items": [],
            "children": [
                {
                    "title": "Nhóm con",
                    "items": [
                        {
                            "type": "video",
                            "title": "Bài / một: <test>",
                            "lessonId": 101,
                            "fileId": 202,
                            "hls": "https://storage-cf.sangsang.edu.vn/a/master.m3u8",
                        },
                        {
                            "type": "video",
                            "title": "Thiếu HLS",
                            "lessonId": 102,
                            "fileId": 203,
                        },
                    ],
                    "children": [],
                }
            ],
        }
    ],
}


class SangsangManifestTests(unittest.TestCase):
    def test_playback_url_uses_site_proxy_and_encodes_source(self):
        result = app.playback_hls_url(
            "https://storage-cf.sangsang.edu.vn/a/master.m3u8", 202
        )
        self.assertTrue(result.startswith("https://sangsang-thuvien.vercel.app/api/playlist?src="))
        self.assertIn("%3A%2F%2Fstorage-cf.sangsang.edu.vn%2Fa%2Fmaster.m3u8", result)
        self.assertTrue(result.endswith("&id=202&seg=1"))

    def test_flatten_preserves_ui_path_and_missing_hls(self):
        lessons = app.flatten_course(COURSE)
        self.assertEqual([lesson.key for lesson in lessons], ["101", "102"])
        self.assertEqual(lessons[0].path, ("Nhóm chính", "Nhóm con"))
        self.assertEqual(lessons[0].file_name, "001 - Bài một test.mp4")
        self.assertTrue(lessons[0].playback_hls)
        self.assertEqual(lessons[1].playback_hls, "")

    def test_resolve_course_page_url(self):
        url, slug = app.resolve_course_url(
            "https://sangsang-thuvien.vercel.app/?khoa=dgnl-bca", "other"
        )
        self.assertEqual(slug, "dgnl-bca")
        self.assertEqual(url, app.course_json_url("dgnl-bca"))

    def test_safe_filename_removes_path_and_reserved_characters(self):
        name = app.safe_filename('A/B: C? "D"', 7)
        self.assertEqual(name, "007 - A B C D.mp4")

    def test_checkpoint_rejects_different_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "checkpoint.json"
            path.write_text(json.dumps({"signature": "old", "lessons": {"x": {}}}), encoding="utf-8")
            result = app.load_checkpoint(path, "new")
        self.assertEqual(result["signature"], "new")
        self.assertEqual(result["lessons"], {})

    def test_download_timeout_has_a_reasonable_floor(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = app.SangsangReupload(
                COURSE,
                "demo",
                object(),
                "dest",
                pathlib.Path(tmp) / "checkpoint.json",
                pathlib.Path(tmp) / "report.json",
                1,
                False,
                app.DEFAULT_BASE_URL,
                10,
            )
        self.assertEqual(engine.download_timeout, 60)


class FfmpegTests(unittest.TestCase):
    def test_local_playlist_rewrites_key_and_segments(self):
        class Response:
            def __init__(self, text):
                self.text = text

            def raise_for_status(self):
                return None

        class Session:
            def get(self, url, timeout):
                if url.endswith("master.m3u8"):
                    return Response("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100\n480p/index.m3u8\n")
                return Response(
                    '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="ENCRYPTED"\n'
                    '#EXTINF:2,\ndata000.ts\n'
                )

        with tempfile.TemporaryDirectory() as tmp:
            playlist = app.prepare_local_media_playlist(
                "https://storage.example/master.m3u8",
                "202",
                "https://sangsang-thuvien.vercel.app/",
                pathlib.Path(tmp),
                Session(),
            )
            content = playlist.read_text(encoding="utf-8")
        self.assertIn("/api/key?id=202", content)
        self.assertIn("https://storage.example/480p/data000.ts", content)

    def test_download_command_uses_local_playlist_and_mp4_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = pathlib.Path(tmp) / "out.mp4"
            playlist = pathlib.Path(tmp) / "media.m3u8"
            playlist.write_text("#EXTM3U\n", encoding="utf-8")

            def fake_run(command, **kwargs):
                output.write_bytes(b"0" * 2048)
                return type("Completed", (), {"returncode": 0, "stderr": ""})()

            with patch.object(app.shutil, "which", return_value="ffmpeg"), patch.object(
                app.subprocess, "run", side_effect=fake_run
            ) as run:
                app.download_hls_to_mp4(playlist, output)

            command = run.call_args.args[0]
            self.assertIn(str(playlist), command)
            self.assertIn("-c", command)
            self.assertIn("copy", command)
            self.assertEqual(command[-1], str(output))


if __name__ == "__main__":
    unittest.main()
