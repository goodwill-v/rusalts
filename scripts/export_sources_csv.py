#!/usr/bin/env python3
"""
Export parser/sources.json into a CSV that can be opened in Excel.

Usage:
  python scripts/export_sources_csv.py --out docs/parser/sources_excel.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="parser/sources.json", help="Input JSON (default: parser/sources.json)")
    ap.add_argument("--out", dest="out", default="docs/parser/sources_excel.csv", help="Output CSV path")
    args = ap.parse_args()

    inp = Path(args.inp).resolve()
    out = Path(args.out).resolve()
    data = json.loads(inp.read_text(encoding="utf-8"))
    sources = data.get("sources") or []
    if not isinstance(sources, list):
        print("ERROR: sources is not a list", file=sys.stderr)
        return 2

    out.parent.mkdir(parents=True, exist_ok=True)
    cols = ["id", "title", "url", "priority", "level", "content_type", "frequency", "tech_notes", "status"]
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, delimiter=";")
        w.writeheader()
        for s in sources:
            if not isinstance(s, dict):
                continue
            row = {k: s.get(k, "") for k in cols}
            # normalize multi-line cells for Excel
            for k, v in list(row.items()):
                if v is None:
                    row[k] = ""
                else:
                    row[k] = str(v).replace("\r", " ").replace("\n", " ").strip()
            w.writerow(row)

    print(str(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

