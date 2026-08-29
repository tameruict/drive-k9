import unittest
from unittest.mock import patch

import sangsang_reupload as app
import sangsang_shard_plan as planner


COURSE = {
    "title": "Shard test",
    "sections": [
        {
            "title": "Subject",
            "items": [
                {
                    "type": "video",
                    "title": f"Video {index}",
                    "lessonId": index,
                    "fileId": index + 1000,
                    "hls": f"https://example.test/{index}.m3u8",
                }
                for index in range(1, 9)
            ],
            "children": [],
        }
    ],
}


class ShardPlanTests(unittest.TestCase):
    def test_build_plan_assigns_every_lesson_once_and_balances_weight(self):
        weights = {str(index): float(index) for index in range(1, 9)}

        def fake_probe(lesson):
            return lesson.key, weights[lesson.key], True

        with patch.object(planner, "_probe_lesson", side_effect=fake_probe):
            plan = planner.build_plan(COURSE, "demo", 4)

        keys = [key for shard in plan["shards"] for key in shard["lesson_keys"]]
        self.assertEqual(sorted(keys), [str(index) for index in range(1, 9)])
        totals = [shard["estimated_bytes"] for shard in plan["shards"]]
        self.assertLessEqual(max(totals) - min(totals), 1.0)
        self.assertEqual(plan["lessons_total"], 8)
        self.assertEqual(plan["probed"], 8)

    def test_load_shard_keys_rejects_stale_signature(self):
        import json
        import pathlib
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "plan.json"
            path.write_text(
                json.dumps({"signature": "old", "shards": [{"lesson_keys": ["1"]}]}),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                app.load_shard_keys(path, "new", 0)

    def test_limit_is_applied_before_sharding(self):
        with patch.object(
            planner,
            "_probe_lesson",
            side_effect=lambda lesson: (lesson.key, 1.0, True),
        ):
            plan = planner.build_plan(COURSE, "demo", 4, limit=3)
        keys = [key for shard in plan["shards"] for key in shard["lesson_keys"]]
        self.assertEqual(sorted(keys), ["1", "2", "3"])
        self.assertEqual(plan["lessons_total"], 3)


if __name__ == "__main__":
    unittest.main()
