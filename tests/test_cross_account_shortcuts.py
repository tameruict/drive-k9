"""Tests cho cross_account_sync: giữ shortcut đúng như nguồn + chống loop vô hạn."""

import unittest
from pathlib import Path
from unittest import mock

import cross_account_sync as xa
from drive_common import FOLDER_MIME_TYPE, SHORTCUT_MIME_TYPE


def _folder(fid, name):
    return {"id": fid, "name": name, "mimeType": FOLDER_MIME_TYPE}


def _file(fid, name, size=10):
    return {"id": fid, "name": name, "mimeType": "application/pdf", "size": size}


def _shortcut(fid, name, target_id, target_mime=FOLDER_MIME_TYPE):
    return {
        "id": fid,
        "name": name,
        "mimeType": SHORTCUT_MIME_TYPE,
        "shortcutDetails": {"targetId": target_id, "targetMimeType": target_mime},
    }


class WalkShortcutTests(unittest.TestCase):
    def _run_walk(self, tree, root="root"):
        """tree: dict src_folder_id -> list[children]. Trả (tasks, shortcuts, calls)."""
        cp = xa.Checkpoint(path=Path("unused.json"))
        cp.save = lambda: None  # không ghi đĩa trong test
        visited: set[str] = set()
        shortcuts: list[xa.ShortcutTask] = []
        list_calls: list[str] = []

        def fake_list_children(_service, folder_id):
            list_calls.append(folder_id)
            return list(tree.get(folder_id, []))

        # ensure_dest_folder: dest id = "d:" + src id, ghi lại để tra ngược.
        def fake_ensure(_svc, _parent, _name, _dry, _sid=None):
            return None

        with mock.patch.object(xa, "list_children", fake_list_children), \
             mock.patch.object(xa, "ensure_dest_folder",
                               side_effect=lambda svc, parent, name, dry: f"d:{name}"):
            tasks = xa.walk_and_collect(
                src_service=mock.Mock(), dst_service=mock.Mock(), cp=cp,
                src_folder=root, dest_folder=f"d:{root}", recursive=True,
                dry_run=False, visited=visited, shortcuts=shortcuts,
            )
        return tasks, shortcuts, list_calls

    def test_shortcut_is_recorded_not_dereferenced(self):
        # root chứa 1 folder thật "A" và 1 shortcut trỏ tới A.
        tree = {
            "root": [_folder("A", "A"), _shortcut("sc1", "Lối tắt A", "A")],
            "A": [_file("f1", "tai-lieu.pdf")],
        }
        tasks, shortcuts, _ = self._run_walk(tree)

        # File trong A copy đúng 1 lần (không nhân đôi qua shortcut).
        self.assertEqual([t.src_id for t in tasks], ["f1"])
        # Shortcut được ghi nhận để tái tạo, không bị deref thành copy.
        self.assertEqual(len(shortcuts), 1)
        self.assertEqual(shortcuts[0].src_id, "sc1")
        self.assertEqual(shortcuts[0].target_id, "A")
        self.assertEqual(shortcuts[0].name, "Lối tắt A")

    def test_shortcut_cycle_does_not_loop_forever(self):
        # B chứa shortcut trỏ ngược về A (A là tổ tiên) -> nếu deref + không
        # visited set thì loop vô hạn. walk phải kết thúc.
        tree = {
            "root": [_folder("A", "A")],
            "A": [_folder("B", "B"), _file("fa", "a.pdf")],
            "B": [_shortcut("scB", "về A", "A"), _file("fb", "b.pdf")],
        }
        tasks, shortcuts, list_calls = self._run_walk(tree)

        # Mỗi folder chỉ được list đúng 1 lần.
        self.assertEqual(sorted(list_calls), ["A", "B", "root"])
        self.assertEqual(sorted(t.src_id for t in tasks), ["fa", "fb"])
        self.assertEqual(len(shortcuts), 1)
        self.assertEqual(shortcuts[0].target_id, "A")


class ProcessShortcutTests(unittest.TestCase):
    def test_recreates_shortcut_when_target_copied(self):
        cp = xa.Checkpoint(path=Path("unused.json"))
        cp.save = lambda: None
        cp.folders["A"] = "dest-A"  # target đã copy -> có id đích
        stats = xa.Stats()
        st = xa.ShortcutTask("sc1", "Lối tắt A", "A", FOLDER_MIME_TYPE,
                             "dest-parent", "root/Lối tắt A")

        with mock.patch.object(xa, "create_shortcut",
                               return_value="new-sc-id") as created:
            xa.process_shortcuts([st], mock.Mock(), cp, stats, dry_run=False)

        created.assert_called_once()
        _svc, name, dest_target, dest_parent = created.call_args.args
        self.assertEqual((name, dest_target, dest_parent),
                         ("Lối tắt A", "dest-A", "dest-parent"))
        self.assertEqual(cp.shortcuts["sc1"], "new-sc-id")
        self.assertEqual(stats.shortcuts, 1)

    def test_skips_when_target_not_copied(self):
        cp = xa.Checkpoint(path=Path("unused.json"))
        cp.save = lambda: None
        stats = xa.Stats()
        st = xa.ShortcutTask("sc1", "Lối tắt X", "X", FOLDER_MIME_TYPE,
                             "dest-parent", "root/Lối tắt X")

        with mock.patch.object(xa, "create_shortcut") as created:
            xa.process_shortcuts([st], mock.Mock(), cp, stats, dry_run=False)

        created.assert_not_called()
        self.assertEqual(stats.shortcut_unresolved, 1)
        self.assertNotIn("sc1", cp.shortcuts)

    def test_skips_already_done_from_checkpoint(self):
        cp = xa.Checkpoint(path=Path("unused.json"))
        cp.save = lambda: None
        cp.folders["A"] = "dest-A"
        cp.shortcuts["sc1"] = "existing"
        stats = xa.Stats()
        st = xa.ShortcutTask("sc1", "Lối tắt A", "A", FOLDER_MIME_TYPE,
                             "dest-parent", "root/Lối tắt A")

        with mock.patch.object(xa, "create_shortcut") as created:
            xa.process_shortcuts([st], mock.Mock(), cp, stats, dry_run=False)

        created.assert_not_called()
        self.assertEqual(stats.skipped, 1)


if __name__ == "__main__":
    unittest.main()
