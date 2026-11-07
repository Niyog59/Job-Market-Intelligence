#!/usr/bin/env python3
"""
Refresh the live Job Observatory numbers in this repo's README.

How it works:
- Fetches one JSON file from the live site (the source of truth).
  Default: https://jobobservatory.dev/api/profile-stats.json
  Override with PROFILE_STATS_URL env var for testing.
- Rewrites ONLY the text between these markers in README.md:
    <!-- LIVE_STATS:START --> ... <!-- LIVE_STATS:END -->
  and the number inside any inline board-count marker:
    <!--PROFILE:boards-->51<!--/PROFILE:boards-->
  (the comments are invisible when the page renders; only the number
  between them is shown, and it always matches boards_enabled)
- Everything else in the README is dated history on purpose: the
  Measured results table, the Problems section, and the "What the
  data shows" snapshot keep the numbers they were written with.
- Safety: if the fetch fails, a field is missing, or a number looks
  impossible (negative, coverage over 100%), it exits non-zero and
  changes nothing. The last good numbers stay up.
- If nothing changed, it exits 0 without touching the file, so the
  GitHub Action only commits when the numbers actually moved.

Expected JSON shape (produced by the publish step on the server):
{
  "boards_enabled": 51,
  "open_jobs": 148,
  "employers_with_open_jobs": 29,
  "kept_jobs_total": 259,
  "extracted_jobs": 166,
  "extracted_total": 167,
  "junior_pct": 10,
  "senior_pct": 84,
  "genai_pct": 27,
  "last_published_at": "2026-11-05"
}
"""

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
DEFAULT_URL = "https://jobobservatory.dev/api/profile-stats.json"

REQUIRED = [
    "boards_enabled",
    "open_jobs",
    "employers_with_open_jobs",
    "kept_jobs_total",
    "extracted_jobs",
    "extracted_total",
    "junior_pct",
    "senior_pct",
    "genai_pct",
    "last_published_at",
]


def fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "readme-stats-bot"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def validate(d: dict) -> None:
    missing = [k for k in REQUIRED if k not in d]
    if missing:
        raise ValueError(f"missing fields: {missing}")
    if d["boards_enabled"] <= 0 or d["open_jobs"] < 0:
        raise ValueError("impossible board/open-job counts")
    if d["kept_jobs_total"] < d["open_jobs"]:
        raise ValueError("kept total below open jobs")
    if d["extracted_total"] > 0:
        cov = d["extracted_jobs"] / d["extracted_total"]
        if cov > 1:
            raise ValueError("extraction coverage over 100%")
    for k in ("junior_pct", "senior_pct", "genai_pct"):
        if not (0 <= d[k] <= 100):
            raise ValueError(f"{k} out of range: {d[k]}")


def live_block(d: dict) -> str:
    cov_pct = round(100 * d["extracted_jobs"] / d["extracted_total"]) if d["extracted_total"] else 0
    return (
        f"{d['boards_enabled']} employer boards checked three times a day · "
        f"{d['open_jobs']} open jobs tracked right now · "
        f"{d['kept_jobs_total']} jobs kept in total · "
        f"requirements extracted on {d['extracted_jobs']} of {d['extracted_total']} "
        f"kept postings ({cov_pct}%) · "
        f"updated {d['last_published_at']}"
    )


def replace_block(text: str, name: str, new_body: str) -> str:
    pattern = re.compile(rf"(<!-- {name}:START -->).*?(<!-- {name}:END -->)", re.DOTALL)
    if not pattern.search(text):
        print(f"note: no {name} markers in README yet, skipping that block")
        return text
    return pattern.sub(lambda m: f"{m.group(1)}\n{new_body}\n{m.group(2)}", text, count=1)


def fill_boards(text: str, boards_enabled: int) -> str:
    """Rewrite the number inside every inline board-count marker."""
    pattern = re.compile(r"(<!--PROFILE:boards-->)\d+(<!--/PROFILE:boards-->)")
    return pattern.sub(lambda m: f"{m.group(1)}{boards_enabled}{m.group(2)}", text)


def main() -> int:
    url = os.environ.get("PROFILE_STATS_URL", DEFAULT_URL)
    try:
        data = fetch(url)
        validate(data)
    except Exception as e:
        print(f"fetch/validate failed, leaving README unchanged: {e}", file=sys.stderr)
        return 1

    original = README.read_text(encoding="utf-8")
    updated = replace_block(original, "LIVE_STATS", live_block(data))
    updated = fill_boards(updated, data["boards_enabled"])

    if updated != original:
        README.write_text(updated, encoding="utf-8")
        print("updated")
    else:
        print("no change")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
