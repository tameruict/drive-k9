"""Build duration-weighted Sangsang upload shards.

The old workflow split by top-level subject.  Subjects have very different
video counts and durations, which leaves the largest subject as the tail of
the run.  This planner probes only playlist metadata, then distributes lessons
with a longest-processing-time-first pass so every runner receives a similar
amount of work.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests

from sangsang_reupload import (
    DEFAULT_BASE_URL,
    DEFAULT_COURSE_SLUG,
    _variant_urls,
    course_json_url,
    fetch_course,
    flatten_course,
    manifest_signature,
    sanitize_error,
)


def _probe_lesson(lesson: Any) -> tuple[str, float, bool]:
    """Return a weight proportional to expected source bytes.

    A failed metadata probe is not fatal: the deterministic fallback weight of
    one still gives a much better distribution than subject-level sharding.
    """
    if not lesson.source_hls:
        return lesson.key, 0.0, False
    last_error: BaseException | None = None
    for attempt in range(3):
        try:
            with requests.Session() as session:
                master = session.get(lesson.source_hls, timeout=25)
                master.raise_for_status()
                variants = _variant_urls(lesson.source_hls, master.text)
                bandwidth, media_url = max(variants) if variants else (0, lesson.source_hls)
                media = master if not variants else session.get(media_url, timeout=25)
                media.raise_for_status()
                duration = sum(float(item) for item in re.findall(r"#EXTINF:([0-9.]+)", media.text))
                # bytes ~= duration * bits/s / 8.  Keep a non-zero floor for short
                # or malformed playlists so every lesson is assigned.
                return lesson.key, max(duration * max(bandwidth, 1) / 8, 1.0), True
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    print(f"PLAN_PROBE_FAILED lesson={lesson.key} error={sanitize_error(last_error or 'unknown')}", flush=True)
    return lesson.key, 1.0, False


def build_plan(course: dict[str, Any], slug: str, shard_count: int, limit: int = 0) -> dict[str, Any]:
    if shard_count < 1:
        raise ValueError("shard_count must be positive")
    lessons = [lesson for lesson in flatten_course(course) if lesson.playback_hls]
    if limit > 0:
        lessons = lessons[:limit]
    weights: dict[str, float] = {}
    probed = 0
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(lessons)))) as pool:
        futures = [pool.submit(_probe_lesson, lesson) for lesson in lessons]
        for future in as_completed(futures):
            key, weight, ok = future.result()
            weights[key] = weight
            probed += int(ok)

    buckets: list[dict[str, Any]] = [
        {"index": index, "lesson_keys": [], "estimated_bytes": 0.0}
        for index in range(shard_count)
    ]
    # Longest-processing-time-first scheduling minimizes the largest bucket.
    for lesson in sorted(lessons, key=lambda item: (-weights.get(item.key, 1.0), item.key)):
        bucket = min(buckets, key=lambda item: (item["estimated_bytes"], item["index"]))
        bucket["lesson_keys"].append(lesson.key)
        bucket["estimated_bytes"] += weights.get(lesson.key, 1.0)

    return {
        "version": 1,
        "course_slug": slug,
        "signature": manifest_signature(course, slug),
        "lessons_total": len(lessons),
        "probed": probed,
        "shard_count": shard_count,
        "shards": buckets,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan balanced Sangsang upload shards")
    parser.add_argument("--course-slug", default=DEFAULT_COURSE_SLUG)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--shards", type=int, default=40)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    with requests.Session() as session:
        course = fetch_course(course_json_url(args.course_slug, args.base_url), session)
    plan = build_plan(course, args.course_slug, args.shards, args.limit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"PLAN_READY lessons={plan['lessons_total']} probed={plan['probed']} "
        f"shards={plan['shard_count']} output={args.output}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
