import json
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
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
    def test_empty_shard_is_successful_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = pathlib.Path(tmp) / "report.json"
            engine = app.SangsangReupload(
                {"title": "Empty", "sections": []},
                "demo",
                object(),
                "dest",
                pathlib.Path(tmp) / "checkpoint.json",
                report,
                1,
                False,
                app.DEFAULT_BASE_URL,
                60,
            )
            result = engine.run()
            self.assertEqual(result["stats"]["lessons_total"], 0)
            self.assertTrue(report.exists())

    def test_extract_drive_folder_id_accepts_raw_id_and_full_url(self):
        folder_id = "1AbC_def-ghiJKLMnopQRSTuvWX"
        self.assertEqual(app.extract_drive_folder_id(folder_id), folder_id)
        self.assertEqual(
            app.extract_drive_folder_id(
                f"https://drive.google.com/drive/u/0/folders/{folder_id}"
            ),
            folder_id,
        )

    def test_extract_drive_folder_id_accepts_open_id_url(self):
        folder_id = "1AbC_def-ghiJKLMnopQRSTuvWX"
        self.assertEqual(
            app.extract_drive_folder_id(f"https://drive.google.com/open?id={folder_id}"),
            folder_id,
        )

    def test_extract_drive_folder_id_rejects_invalid_value(self):
        with self.assertRaises(ValueError):
            app.extract_drive_folder_id("https://drive.google.com/drive/u/0/my-drive")

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
        self.assertEqual(lessons[0].drive_path, ("01 - Nhóm chính", "01 - Nhóm con"))
        self.assertEqual(lessons[0].file_name, "001 - Bài một test [bai-101].mp4")
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

    def test_drive_path_numbers_sibling_sections_in_ui_order(self):
        course = {
            "sections": [
                {"title": "Một", "items": [{"type": "video", "title": "A", "lessonId": 1, "fileId": 2, "hls": "x"}]},
                {"title": "Hai", "items": [{"type": "video", "title": "B", "lessonId": 3, "fileId": 4, "hls": "y"}]},
            ]
        }
        lessons = app.flatten_course(course)
        self.assertEqual(lessons[0].drive_path, ("01 - Một",))
        self.assertEqual(lessons[1].drive_path, ("02 - Hai",))

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
    def test_fast_download_uses_concurrent_fragments(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = pathlib.Path(tmp) / "out.mp4"
            playlist = pathlib.Path(tmp) / "media.m3u8"
            playlist.write_text("#EXTM3U\n#EXTINF:2,\nsegment.ts\n", encoding="utf-8")
            captured = {}

            class FakeYoutubeDL:
                def __init__(self, options):
                    captured.update(options)

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def download(self, urls):
                    pathlib.Path(captured["outtmpl"].replace("%(ext)s", "mp4")).write_bytes(b"0" * 2048)

            with patch.dict(sys.modules, {"yt_dlp": SimpleNamespace(YoutubeDL=FakeYoutubeDL)}):
                app.download_hls_to_mp4(playlist, output, fragment_concurrency=8)

            self.assertEqual(captured["concurrent_fragment_downloads"], 8)
            self.assertTrue(captured["enable_file_urls"])
            self.assertTrue(output.exists())

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

            class FakeProcess:
                def __init__(self):
                    self.stdout = iter(["out_time=00:00:02.000\n", "progress=end\n"])
                    self.stderr = type("Err", (), {"read": lambda self: ""})()

                def poll(self):
                    return 0

                def wait(self, timeout=None):
                    return 0

                def communicate(self, timeout=None):
                    return "", ""

                def kill(self):
                    return None

            def fake_popen(command, **kwargs):
                output.write_bytes(b"0" * 2048)
                return FakeProcess()

            with patch.object(app.shutil, "which", return_value="ffmpeg"), patch.object(
                app.subprocess, "Popen", side_effect=fake_popen
            ) as run:
                app.download_hls_to_mp4(playlist, output)

            command = run.call_args.args[0]
            self.assertIn(str(playlist), command)
            self.assertIn("-c", command)
            self.assertIn("copy", command)
            self.assertEqual(command[-1], str(output))


if __name__ == "__main__":
    unittest.main()
