"""Generate a synthetic library spreadsheet (default 20,000 rows) with controlled defects.

All data is invented. Defects are injected at known rates so the migration report can be checked
against ground truth: the script also writes <output>.truth.json with the exact expected counts.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from pathlib import Path

ADJ = ["Applied", "Advanced", "Modern", "Practical", "Essential", "Fundamental", "Comparative", "Digital"]
TOPIC = [
    "Physics",
    "Algorithms",
    "History",
    "Economics",
    "Botany",
    "Poetry",
    "Statistics",
    "Law",
    "Chemistry",
    "Networks",
    "Ethics",
    "Geology",
    "Architecture",
    "Linguistics",
    "Databases",
]
FIRST = ["Asha", "Ravi", "Meera", "Imran", "Lakshmi", "Kiran", "Sana", "Vikram", "Nandini", "Arjun"]
LAST = ["Rao", "Khan", "Reddy", "Sharma", "Iyer", "Naidu", "Qureshi", "Das", "Menon", "Verma"]
PUB = ["Orient Press", "Deccan Books", "Charminar Publishing", "Godavari House", "Krishna Academic"]
LOCS = ["MAIN-A1", "MAIN-A2", "MAIN-B1", "MAIN-B2", "REF-1", "JUNIOR-1", "PERIODICALS"]
COLUMNS = ["Accession No", "Title", "Author", "ISBN", "Publisher", "Year", "Location", "Category", "Barcode"]


def isbn13(rng: random.Random) -> str:
    body = "978" + "".join(str(rng.randint(0, 9)) for _ in range(9))
    check = (10 - sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(body)) % 10) % 10
    return body + str(check)


def build(rows: int, seed: int) -> tuple[list[list[str]], dict]:
    rng = random.Random(seed)
    data: list[list[str]] = []
    titles: list[tuple[str, str, str, str, str]] = []
    for i in range(rows):
        if titles and rng.random() < 0.35:  # extra copy of an existing title
            title, author, isbn, pub, year = rng.choice(titles)
        else:
            title = f"{rng.choice(ADJ)} {rng.choice(TOPIC)} Vol {rng.randint(1, 9)} Ed {i}"
            author = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
            isbn, pub, year = isbn13(rng), rng.choice(PUB), str(rng.randint(1950, 2025))
            titles.append((title, author, isbn, pub, year))
        loc = rng.choice(LOCS)
        cat = "Reference" if loc == "REF-1" else "Lending"
        data.append([f"ACC{i + 1:06d}", title, author, isbn, pub, year, loc, cat, f"BC{i + 1:07d}"])

    truth = {"rejected": 0, "duplicates": 0, "reasons": {}}

    def bump(reason: str, kind: str) -> None:
        truth[kind] += 1
        truth["reasons"][reason] = truth["reasons"].get(reason, 0) + 1

    # Inject defects on distinct rows, deterministic by seed.
    idx = list(range(rows))
    rng.shuffle(idx)
    cursor = 0

    def take(n: int) -> list[int]:
        nonlocal cursor
        chunk = idx[cursor : cursor + n]
        cursor += n
        return chunk

    n = max(1, rows // 200)  # 0.5% of each defect type
    for r in take(n):
        data[r][1] = ""
        bump("missing_title", "rejected")
    for r in take(n):
        data[r][3] = "9781234567890"  # bad checksum
        bump("invalid_isbn", "rejected")
    for r in take(n):
        data[r][5] = "20X5"
        bump("invalid_year", "rejected")
    for r in take(n):
        data[r][6] = ""
        bump("missing_location", "rejected")
    for r in take(n):
        data[r][8] = ""
        bump("missing_barcode", "rejected")
    for r in take(n):
        data[r][7] = "Mystery"
        bump("invalid_category", "rejected")
    # Exact duplicate rows appended at the end (same accession as an earlier clean row)
    clean_rows = [r for r in idx[cursor:]]
    for src in rng.sample(clean_rows, n):
        dup = list(data[src])
        data.append(dup)
        bump("duplicate_in_file", "duplicates")
    # Whitespace noise on clean rows must NOT cause rejection
    for r in take(n):
        data[r][1] = "   " + data[r][1] + "   "
    truth["source_rows"] = len(data)
    truth["valid"] = len(data) - truth["rejected"] - truth["duplicates"]
    return data, truth


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=Path, default=Path("data/synthetic_library.csv"))
    ap.add_argument("--xlsx", action="store_true", help="also write an .xlsx copy")
    args = ap.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    data, truth = build(args.rows, args.seed)
    with args.out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        w.writerows(data)
    if args.xlsx:
        from openpyxl import Workbook

        wb = Workbook(write_only=True)
        ws = wb.create_sheet()
        ws.append(COLUMNS)
        for row in data:
            ws.append(row)
        wb.save(args.out.with_suffix(".xlsx"))
    args.out.with_suffix(".truth.json").write_text(json.dumps(truth, indent=2), encoding="utf-8")
    print(
        f"wrote {len(data)} rows to {args.out}; expected valid={truth['valid']} "
        f"rejected={truth['rejected']} duplicates={truth['duplicates']}"
    )


if __name__ == "__main__":
    main()
