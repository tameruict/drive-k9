import argparse
import json
import os
import shutil
import tempfile
import types
import unittest
from unittest.mock import patch
import unittest.mock

import windows_sync_tool_improved as sync_tool
from drive_common import FOLDER_MIME_TYPE, SHORTCUT_MIME_TYPE


SOURCE_ID = "1AbC_def-ghiJKLMnopQRSTuvWX"
DEST_ID = "2AbC_def-ghiJKLMnopQRSTuvWX"
ENV_ID = "3AbC_def-ghiJKLMnopQRSTuvWX"
DEFAULT_ID = "4AbC_def-ghiJKLMnopQRSTuvWX"


def make_args(**overrides):
    values = {
        "source_folder": None,
        "dest_folder": None,
        "source_folder_id": None,
        "dest_folder_id": None,
        "no_input_prompt": True,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class InteractiveStdin:
    @staticmethod
    def isatty():
        return True


class DriveFolderInputTests(unittest.TestCase):
    def test_extract_drive_folder_id_accepts_raw_id(self):
        self.assertEqual(sync_tool.extract_drive_folder_id(SOURCE_ID), SOURCE_ID)

    def test_extract_drive_folder_id_accepts_folder_url(self):
        url = f"https://drive.google.com/drive/folders/{SOURCE_ID}?usp=sharing"
        self.assertEqual(sync_tool.extract_drive_folder_id(url), SOURCE_ID)

    def test_extract_drive_folder_id_accepts_open_id_url(self):
        url = f"https://drive.google.com/open?id={SOURCE_ID}"
        self.assertEqual(sync_tool.extract_drive_folder_id(url), SOURCE_ID)

    def test_extract_drive_folder_id_strips_quotes_and_spaces(self):
        self.assertEqual(sync_tool.extract_drive_folder_id(f'  "{SOURCE_ID}"  '), SOURCE_ID)

    def test_extract_drive_folder_id_rejects_invalid_input(self):
        with self.assertRaises(ValueError):
            sync_tool.extract_drive_folder_id("bad id!")

    def test_resolve_folder_id_prefers_cli_over_positional_and_env(self):
        with patch.dict(os.environ, {"SOURCE_FOLDER_ID": ENV_ID}, clear=False):
            resolved = sync_tool.resolve_folder_id(
                "Source", SOURCE_ID, DEST_ID, "SOURCE_FOLDER_ID", DEFAULT_ID, False
            )

        self.assertEqual(resolved, SOURCE_ID)

    def test_resolve_folder_id_prefers_positional_over_env(self):
        with patch.dict(os.environ, {"SOURCE_FOLDER_ID": ENV_ID}, clear=False):
            resolved = sync_tool.resolve_folder_id(
                "Source", None, SOURCE_ID, "SOURCE_FOLDER_ID", DEFAULT_ID, False
            )

        self.assertEqual(resolved, SOURCE_ID)

    def test_resolve_folder_id_prefers_env_over_default(self):
        with patch.dict(os.environ, {"SOURCE_FOLDER_ID": ENV_ID}, clear=False):
            resolved = sync_tool.resolve_folder_id(
                "Source", None, None, "SOURCE_FOLDER_ID", DEFAULT_ID, False
            )

        self.assertEqual(resolved, ENV_ID)

    def test_resolve_folder_id_uses_default_without_input(self):
        with patch.dict(os.environ, {}, clear=True):
            resolved = sync_tool.resolve_folder_id(
                "Source", None, None, "SOURCE_FOLDER_ID", DEFAULT_ID, False
            )

        self.assertEqual(resolved, DEFAULT_ID)

    def test_resolve_folder_id_rejects_invalid_env_when_prompt_disabled(self):
        with patch.dict(os.environ, {"SOURCE_FOLDER_ID": "bad id!"}, clear=False):
            with self.assertRaises(ValueError):
                sync_tool.resolve_folder_id(
                    "Source", None, None, "SOURCE_FOLDER_ID", DEFAULT_ID, False
                )

    def test_resolve_folder_id_prompts_before_env_when_interactive(self):
        with (
            patch.dict(os.environ, {"SOURCE_FOLDER_ID": ENV_ID}, clear=False),
            patch("sys.stdin", InteractiveStdin()),
            patch("builtins.input", return_value=""),
        ):
            resolved = sync_tool.resolve_folder_id(
                "Source", None, None, "SOURCE_FOLDER_ID", DEFAULT_ID, True
            )

        self.assertEqual(resolved, DEFAULT_ID)

    def test_resolve_folder_id_prompts_interactively_and_retries_invalid_input(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("sys.stdin", InteractiveStdin()),
            patch("builtins.input", side_effect=["bad id!", SOURCE_ID]),
            patch.object(sync_tool.UI, "warn"),
        ):
            resolved = sync_tool.resolve_folder_id(
                "Source", None, None, "SOURCE_FOLDER_ID", DEFAULT_ID, True
            )

        self.assertEqual(resolved, SOURCE_ID)

    def test_config_manager_normalizes_positional_source_and_dest(self):
        config = sync_tool.ConfigManager()
        config.apply_args(
            make_args(
                source_folder=f"https://drive.google.com/drive/folders/{SOURCE_ID}",
                dest_folder=f"https://drive.google.com/open?id={DEST_ID}",
            )
        )

        self.assertEqual(config.SOURCE_FOLDER_IDS, [SOURCE_ID])
        self.assertEqual(config.DEST_FOLDER_ID, DEST_ID)


class CheckpointResilienceTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "checkpoint.json")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

    def _engine_stub(self):
        import threading
        import types

        stub = types.SimpleNamespace()
        stub.lock = threading.Lock()
        stub.config = types.SimpleNamespace(CHECKPOINT_FILE=self.path)
        stub.checkpoint = {}
        return stub

    def test_load_checkpoint_reads_valid_file(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"a": "1"}, f)
        self.assertEqual(sync_tool.SyncEngine._load_checkpoint(self.path), {"a": "1"})

    def test_load_checkpoint_recovers_from_tmp_when_main_corrupt(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ not valid json")
        with open(f"{self.path}.tmp", "w", encoding="utf-8") as f:
            json.dump({"recovered": "yes"}, f)
        with patch.object(sync_tool.UI, "warn"):
            result = sync_tool.SyncEngine._load_checkpoint(self.path)
        self.assertEqual(result, {"recovered": "yes"})

    def test_load_checkpoint_returns_empty_when_all_corrupt(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("garbage")
        with patch.object(sync_tool.UI, "warn"):
            self.assertEqual(sync_tool.SyncEngine._load_checkpoint(self.path), {})

    def test_flush_checkpoint_writes_atomically_and_leaves_no_tmp(self):
        stub = self._engine_stub()
        sync_tool.SyncEngine._flush_checkpoint(stub, {"x": "y"})
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"x": "y"})
        self.assertFalse(os.path.exists(f"{self.path}.tmp"))


class BlockedPdfRetryTests(unittest.TestCase):
    """The Playwright PDF fallback must retry transient capture failures instead
    of dumping the file into bad_paths on the first miss (root cause of the
    "Đường dẫn lỗi" report on view-only PDFs)."""

    def _engine_stub(self, retry_times=3):
        import tempfile
        import types

        stub = types.SimpleNamespace()
        stub._temp_dir = tempfile.TemporaryDirectory()
        stub.config = types.SimpleNamespace(
            RETRY_TIMES=retry_times,
            TOKEN_FILE="token.json",
            COOKIE_FILE="cookie.txt",
            BLOCKED_PDF_CACHE_DIR=stub._temp_dir.name,
        )
        stub._counts = {}

        def _count(key, amount=1):
            stub._counts[key] = stub._counts.get(key, 0) + amount

        stub._count = _count
        stub._save_checkpoint = lambda *a, **k: None
        stub._remember_dest_item = lambda *a, **k: None
        for method_name in (
            "_blocked_pdf_cache_path",
            "_pdf_rescue_session",
            "_load_blocked_pdf_cookie_dict",
            "_download_blocked_pdf_to_cache",
        ):
            method = getattr(sync_tool.SyncEngine, method_name)
            setattr(stub, method_name, types.MethodType(method, stub))
        return stub

    def test_pdf_fallback_retries_then_succeeds(self):
        stub = self._engine_stub(retry_times=3)
        src_file = {"id": "fid", "name": "doc.pdf", "size": "100000"}

        # Capture fails twice, succeeds on the third attempt.
        capture_results = [False, False, True]

        def fake_capture(**kwargs):
            ok = capture_results.pop(0)
            if ok:
                kwargs["output_path"].with_suffix(".pdf").write_bytes(b"%PDF-1.4" + b"0" * 20000)
            return ok

        api = unittest.mock.Mock()
        api.upload_local_file.return_value = "dest123"

        with (
            patch.object(sync_tool, "load_oauth_token", return_value="tok"),
            patch.object(sync_tool, "download_direct_uc", return_value=False),
            patch.object(sync_tool, "download_playwright_viewer", side_effect=fake_capture),
            patch.object(sync_tool, "_validate_pdf", return_value=True),
            patch.object(sync_tool, "time") as fake_time,
            patch.object(sync_tool.UI, "status"),
            patch.object(sync_tool.UI, "warn"),
            patch.object(sync_tool.UI, "error"),
        ):
            fake_time.sleep = lambda *_: None
            result = sync_tool.SyncEngine._handle_blocked_pdf_playwright(
                stub, api, src_file, "destparent", "doc.pdf", "fid"
            )

        self.assertTrue(result)
        self.assertEqual(stub._counts.get("fallback_uploaded"), 1)
        self.assertNotIn("blocked", stub._counts)
        api.upload_local_file.assert_called_once()

    def test_pdf_fallback_gives_up_after_retry_times(self):
        stub = self._engine_stub(retry_times=2)
        src_file = {"id": "fid", "name": "doc.pdf", "size": "100000"}
        api = unittest.mock.Mock()

        with (
            patch.object(sync_tool, "load_oauth_token", return_value="tok"),
            patch.object(sync_tool, "download_direct_uc", return_value=False),
            patch.object(sync_tool, "download_playwright_viewer", return_value=False),
            patch.object(sync_tool, "_validate_pdf", return_value=False),
            patch.object(sync_tool, "time") as fake_time,
            patch.object(sync_tool.UI, "status"),
            patch.object(sync_tool.UI, "warn"),
            patch.object(sync_tool.UI, "error"),
        ):
            fake_time.sleep = lambda *_: None
            result = sync_tool.SyncEngine._handle_blocked_pdf_playwright(
                stub, api, src_file, "destparent", "doc.pdf", "fid"
            )

        self.assertFalse(result)
        self.assertEqual(stub._counts.get("blocked"), 1)
        api.upload_local_file.assert_not_called()


class ShortcutSkipTests(unittest.TestCase):
    def test_process_tree_item_skips_folder_shortcut_without_creating_dest_folder(self):
        stub = types.SimpleNamespace()
        stub._counts = {}

        def _count(key, amount=1):
            stub._counts[key] = stub._counts.get(key, 0) + amount

        stub._count = _count
        stub.ensure_dest_folder = unittest.mock.Mock()
        stub.recursive_copy = unittest.mock.Mock()
        stub.process_single_file = unittest.mock.Mock()

        shortcut = {
            "id": "shortcut-id",
            "name": "Course shortcut",
            "mimeType": SHORTCUT_MIME_TYPE,
            "shortcutDetails": {
                "targetId": "target-folder-id",
                "targetMimeType": FOLDER_MIME_TYPE,
            },
        }

        with patch.object(sync_tool.UI, "status"):
            result = sync_tool.SyncEngine.process_tree_item(
                stub,
                unittest.mock.Mock(),
                unittest.mock.Mock(),
                shortcut,
                "dest-folder-id",
                "ROOT",
                0,
            )

        self.assertTrue(result)
        self.assertEqual(stub._counts.get("skipped"), 1)
        stub.ensure_dest_folder.assert_not_called()
        stub.recursive_copy.assert_not_called()
        stub.process_single_file.assert_not_called()


if __name__ == "__main__":
    unittest.main()
