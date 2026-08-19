import json
import tempfile
import unittest
from pathlib import Path

from classin_reupload import ReuploadEngine, load_payloads, reference_index, sanitize_error


class FakeDriveClient:
    def __init__(self):
        self.shortcuts = []

    def ensure_folder(self, parent_id, name):
        return f"folder:{parent_id}:{name}"

    def get_file(self, file_id):
        return None

    def find_media(self, media_id, dest_root):
        return None

    def ensure_shortcut(self, parent_id, name, target_id, media_id, activity_id):
        self.shortcuts.append((parent_id, name, target_id, media_id, activity_id))
        return f"shortcut:{activity_id}", True


class FakeCredentialFactory:
    def __init__(self):
        self.drive = FakeDriveClient()

    def client(self):
        return self.drive


class StubReuploadEngine(ReuploadEngine):
    def _upload_with_fallbacks(self, client, urls, name, parent_id, media_id, expected_size):
        return f"file:{media_id}", expected_size


class ClassInReuploadTests(unittest.TestCase):
    def payloads(self):
        media = {
            "version": 1,
            "manifest_id": "a" * 64,
            "media": [
                {
                    "id": "classin:M1",
                    "format": "mp4",
                    "size": 100,
                    "sources": ["https://cdn.test/f0.mp4", "https://mirror.test/f0.mp4"],
                }
            ],
        }
        mapping = {
            "version": 1,
            "manifest_id": "a" * 64,
            "activities": [
                {
                    "key": "C/CAT/U/A1",
                    "activity_id": "A1",
                    "path": ["Course", "Category"],
                    "refs": [{"id": "classin:M1", "name": "Bài 1.mp4"}],
                },
                {
                    "key": "C/CAT/U/A2",
                    "activity_id": "A2",
                    "path": ["Course", "Category"],
                    "refs": [{"id": "classin:M1", "name": "Bài 2.mp4"}],
                },
            ],
        }
        return media, mapping

    def test_load_payloads_and_reference_index(self):
        media, mapping = self.payloads()
        with tempfile.TemporaryDirectory() as temp:
            mp = Path(temp) / "media.json"
            rp = Path(temp) / "mapping.json"
            mp.write_text(json.dumps(media), encoding="utf-8")
            rp.write_text(json.dumps(mapping), encoding="utf-8")
            loaded_media, loaded_mapping = load_payloads(mp, rp)
        refs = reference_index(loaded_mapping)
        self.assertEqual(len(loaded_media["media"]), 1)
        self.assertEqual(len(refs["classin:M1"]), 2)
        self.assertEqual(refs["classin:M1"][1]["file_name"], "Bài 2.mp4")

    def test_rejects_unknown_media_reference(self):
        media, mapping = self.payloads()
        mapping["activities"][0]["refs"][0]["id"] = "classin:UNKNOWN"
        with tempfile.TemporaryDirectory() as temp:
            mp = Path(temp) / "media.json"
            rp = Path(temp) / "mapping.json"
            mp.write_text(json.dumps(media), encoding="utf-8")
            rp.write_text(json.dumps(mapping), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Invalid media ref"):
                load_payloads(mp, rp)

    def test_rejects_duplicate_source_urls(self):
        media, mapping = self.payloads()
        media["media"][0]["sources"].append(media["media"][0]["sources"][0])
        with tempfile.TemporaryDirectory() as temp:
            mp = Path(temp) / "media.json"
            rp = Path(temp) / "mapping.json"
            mp.write_text(json.dumps(media), encoding="utf-8")
            rp.write_text(json.dumps(mapping), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Duplicate sources"):
                load_payloads(mp, rp)

    def test_error_sanitizer_removes_urls(self):
        text = sanitize_error("GET https://secret.test/path?token=abc failed")
        self.assertNotIn("secret.test", text)
        self.assertIn("<url>", text)

    def test_engine_uploads_once_and_creates_alias_shortcut(self):
        media, mapping = self.payloads()
        credentials = FakeCredentialFactory()
        with tempfile.TemporaryDirectory() as temp:
            checkpoint = Path(temp) / "checkpoint.json"
            report = Path(temp) / "report.json"
            engine = StubReuploadEngine(
                media,
                mapping,
                credentials,
                "DEST",
                checkpoint,
                report,
                max_workers=1,
            )
            result = engine.run()
            saved = json.loads(checkpoint.read_text(encoding="utf-8"))
        self.assertEqual(result["stats"]["uploaded"], 1)
        self.assertEqual(result["stats"]["shortcuts_created"], 1)
        self.assertEqual(len(credentials.drive.shortcuts), 1)
        self.assertIn("classin:M1", saved["media"])


if __name__ == "__main__":
    unittest.main()
