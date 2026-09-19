"""Freeze one completed favorites cycle into an auditable cancellation scope."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


RESULT_RE = re.compile(r"^\[[^]]+\] \[(ok|skip|warn|fail)\] (\d+) \|")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--start-marker", required=True)
    parser.add_argument("--finish-marker", required=True)
    parser.add_argument("--expected", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    lines = args.log.read_text(encoding="utf-8").splitlines()
    starts = [i for i, line in enumerate(lines) if args.start_marker in line]
    finishes = [i for i, line in enumerate(lines) if args.finish_marker in line]
    if len(starts) != 1 or len(finishes) != 1 or finishes[0] <= starts[0]:
        raise SystemExit(
            f"Campaign markers are not unique/ordered: starts={starts}, finishes={finishes}"
        )

    results: dict[str, str] = {}
    for line in lines[starts[0] + 1 : finishes[0]]:
        match = RESULT_RE.match(line)
        if not match:
            continue
        status, video_id = match.groups()
        if video_id in results:
            raise SystemExit(f"Duplicate campaign result: {video_id}")
        results[video_id] = status
    if len(results) != args.expected:
        raise SystemExit(f"Expected {args.expected} unique results, found {len(results)}")

    index: dict[str, dict] = {}
    for line in args.index.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        video_id = str(row.get("video_id") or "")
        if video_id:
            index[video_id] = row

    output_rows = []
    for video_id, status in results.items():
        source = index.get(video_id, {})
        raw_path = str(source.get("path") or "")
        path_ok = bool(raw_path and Path(raw_path).is_file())
        output_rows.append(
            {
                "aweme_id": video_id,
                "campaign_status": status,
                "url": source.get("url") or f"https://www.douyin.com/video/{video_id}",
                "path": raw_path,
                "path_is_file": path_ok,
                "safe_candidate": status in {"ok", "skip"} and path_ok,
            }
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.output.with_suffix(args.output.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        for row in output_rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(args.output)

    counts = {name: list(results.values()).count(name) for name in ("ok", "skip", "warn", "fail")}
    safe = sum(bool(row["safe_candidate"]) for row in output_rows)
    print(
        f"manifest={args.output} total={len(output_rows)} safe_candidates={safe} "
        f"ok={counts['ok']} skip={counts['skip']} warn={counts['warn']} fail={counts['fail']}"
    )


if __name__ == "__main__":
    main()
