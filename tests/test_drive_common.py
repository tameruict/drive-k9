import unittest

from drive_common import (
    FOLDER_MIME_TYPE,
    SHORTCUT_MIME_TYPE,
    drive_query_literal,
    find_matching_item,
    is_shortcut,
    normalize_drive_name,
    normalize_drive_path,
    same_drive_name,
    skipped_shortcut_reason,
    without_drive_shortcuts,
)


class DriveCommonTests(unittest.TestCase):
    def test_drive_query_literal_escapes_google_query_string(self):
        self.assertEqual(drive_query_literal(r"Bob's \ Drive"), r"Bob\'s \\ Drive")

    def test_normalize_drive_name_handles_spacing_case_and_zero_width(self):
        self.assertEqual(normalize_drive_name("  A\u00a0B\u200b  "), "a b")

    def test_normalize_drive_path_normalizes_each_segment(self):
        self.assertEqual(normalize_drive_path("/Folder\u00a0A//File  B/"), "folder a/file b")

    def test_same_drive_name_can_use_loose_folder_separator_matching(self):
        self.assertFalse(same_drive_name("Course_A", "Course A"))
        self.assertTrue(same_drive_name("Course_A", "Course A", loose_folder=True))

    def test_find_matching_item_rejects_ambiguous_loose_folder_match(self):
        items = [
            {"id": "1", "name": "Course-A", "mimeType": FOLDER_MIME_TYPE},
            {"id": "2", "name": "Course_A", "mimeType": FOLDER_MIME_TYPE},
        ]

        self.assertIsNone(find_matching_item(items, "Course A", FOLDER_MIME_TYPE))

    def test_without_drive_shortcuts_filters_shortcut_items(self):
        folder = {"id": "1", "name": "Folder", "mimeType": FOLDER_MIME_TYPE}
        shortcut = {"id": "2", "name": "Shortcut", "mimeType": SHORTCUT_MIME_TYPE}
        file_item = {"id": "3", "name": "File", "mimeType": "application/pdf"}

        self.assertTrue(is_shortcut(shortcut))
        self.assertEqual(without_drive_shortcuts([folder, shortcut, file_item]), [folder, file_item])
        self.assertEqual(skipped_shortcut_reason(shortcut), "Bo qua shortcut: Shortcut")


if __name__ == "__main__":
    unittest.main()
