"""Performance evidence: import throughput, search latency and circulation latency on 20,000 synthetic rows."""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.bootstrap import build_runtime  # noqa: E402
from app.config import Settings  # noqa: E402
from app.models import Item
from app.seed import seed_demo  # noqa: E402
from app.services import catalog, circulation, migration  # noqa: E402
from scripts.generate_synthetic_data import COLUMNS, build  # noqa: E402


def pct(values: list[float], p: float) -> float:
    values = sorted(values)
    return round(values[min(len(values) - 1, int(len(values) * p))] * 1000, 2)


def main() -> dict:
    tmp = Path(tempfile.mkdtemp())
    data, _truth = build(20000, 42)
    src = tmp / "source.csv"
    import csv

    with src.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        w.writerows(data)
    rt = build_runtime(Settings(data_dir=tmp / "d", jwt_secret="perf-secret-0123456789abcdef0123456789"))
    out: dict = {"rows": len(data)}
    with rt.factory() as s:
        seed_demo(s)
        s.commit()
        t = time.perf_counter()
        rep = migration.run_import(s, rt.settings, src, dry_run=False, actor="perf")
        s.commit()
        out["import_seconds"] = round(time.perf_counter() - t, 2)
        out["loaded"] = rep["loaded"]
        out["import_rows_per_second"] = round(len(data) / out["import_seconds"])
        words = ["Physics", "Geology", "Sharma", "Advanced", "Practical", "Deccan", "978", "Vol 5"]
        lat = []
        for i in range(200):
            t = time.perf_counter()
            catalog.search_catalog(s, words[i % len(words)], limit=20)
            lat.append(time.perf_counter() - t)
        out["search_ms"] = {"p50": pct(lat, 0.5), "p95": pct(lat, 0.95), "max": pct(lat, 1.0), "samples": len(lat)}
        accs = [r[0] for r in s.execute(select(Item.accession_no).where(Item.is_reference.is_(False)).limit(300))]
        out_lat, in_lat = [], []
        for a in accs:
            t = time.perf_counter()
            circulation.checkout(s, rt.settings, member_key="M0001", item_key=a, actor="perf")
            s.commit()
            out_lat.append(time.perf_counter() - t)
            t = time.perf_counter()
            circulation.checkin(s, rt.settings, item_key=a, actor="perf")
            s.commit()
            in_lat.append(time.perf_counter() - t)
        out["checkout_ms"] = {"p50": pct(out_lat, 0.5), "p95": pct(out_lat, 0.95), "samples": len(out_lat)}
        out["checkin_ms"] = {"p50": pct(in_lat, 0.5), "p95": pct(in_lat, 0.95), "samples": len(in_lat)}
    rt.engine.dispose()
    return out


if __name__ == "__main__":
    print(json.dumps(main(), indent=2))
