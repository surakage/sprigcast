"""Pull SprigCast reports from the report inbox (reports.slothbloom.com) with the read token.

    python integrations/report-inbox/pull_reports.py --since 2026-11-27        list them, newest first
    python integrations/report-inbox/pull_reports.py SC-7F3K2M --out reports   download one zip

The token comes from SPRIGCAST_REPORTS_TOKEN (the Worker's READ_TOKEN secret). Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

BASE = os.environ.get("SPRIGCAST_REPORTS_URL", "https://reports.slothbloom.com")


def _get(path: str, token: str) -> bytes:
    request = urllib.request.Request(f"{BASE}{path}", headers={"Authorization": f"Bearer {token}",
                                                               "User-Agent": "sprigcast-pull-reports"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("report", nargs="?", help="a report number, SC-XXXXXX, to download")
    parser.add_argument("--since", help="list reports from this day (YYYY-MM-DD); default today")
    parser.add_argument("--kind", help="only this kind: crash, problem, stream or import")
    parser.add_argument("--out", default=".", help="folder for a downloaded zip")
    args = parser.parse_args(argv)
    token = os.environ.get("SPRIGCAST_REPORTS_TOKEN", "")
    if not token:
        print("Set SPRIGCAST_REPORTS_TOKEN first (the report inbox's READ_TOKEN).", file=sys.stderr)
        return 2
    if args.report:
        target = Path(args.out) / f"{args.report}.zip"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_get(f"/v1/reports/{args.report}", token))
        print(target)
        return 0
    rows = json.loads(_get(f"/v1/reports?since={args.since or ''}", token))
    for row in rows:
        if args.kind and row["kind"] != args.kind:
            continue
        print(f"{row['id']}  {row['date']}  {row['kind']:<8} {row['version']:<40} {row['size'] // 1024:>5} KB  "
              f"{row['cause']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
