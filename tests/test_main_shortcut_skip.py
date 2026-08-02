import threading
import types
import unittest
from unittest import mock

import main
from drive_common import FOLDER_MIME_TYPE, SHORTCUT_MIME_TYPE


class MainShortcutSkipTests(unittest.TestCase):
    def test_process_tree_item_records_shortcut_as_skip_without_creating_folder(self):
        stub = types.SimpleNamespace()
        stub.lock = threading.Lock()
        stub.results = []
        stub.ensure_dest_folder = mock.Mock()
        stub.recursive_copy = mock.Mock()
        stub.submit_file_copy = mock.Mock()
        stub._record = main.CopyEngine._record.__get__(stub, type(stub))
        stub._record_item_skip = main.CopyEngine._record_item_skip.__get__(stub, type(stub))
        stub._record_item_error = main.CopyEngine._record_item_error.__get__(stub, type(stub))

        shortcut = {
            "id": "shortcut-id",
            "name": "Course shortcut",
            "mimeType": SHORTCUT_MIME_TYPE,
            "shortcutDetails": {
                "targetId": "target-folder-id",
                "targetMimeType": FOLDER_MIME_TYPE,
            },
        }

        main.CopyEngine.process_tree_item(
            stub,
            mock.Mock(),
            shortcut,
            "dest-folder-id",
            "ROOT",
            0,
        )

        self.assertEqual(len(stub.results), 1)
        self.assertEqual(stub.results[0]["status"], "SKIP")
        self.assertEqual(stub.results[0]["note"], "Bo qua shortcut: Course shortcut")
        stub.ensure_dest_folder.assert_not_called()
        stub.recursive_copy.assert_not_called()
        stub.submit_file_copy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
