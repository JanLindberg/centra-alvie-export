import csv
import os
from collections import defaultdict
from datetime import date
from pathlib import Path

from writers import LOG_FIELDS, PD_FIELDS, ROW_FIELDS, slug


def _read(path):
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _write(path, fields, rows):
    # Write to a temp file and rename, so a crash never leaves a half-written file.
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


class CsvWriter:
    """sales_daily.csv (all rows), load_log.csv and pd_<brand>.csv (PD's four columns)."""

    def __init__(self, directory):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.sales_path = self.dir / "sales_daily.csv"
        self.log_path = self.dir / "load_log.csv"

    def logged_days(self, brand):
        return {date.fromisoformat(r["date"]) for r in _read(self.log_path) if r["brand"] == brand}

    def replace_day(self, day, brand, rows):
        d = day.isoformat()
        kept = [r for r in _read(self.sales_path) if not (r["date"] == d and r["brand"] == brand)]
        kept += rows
        kept.sort(key=lambda r: (r["date"], r["brand"], r["country_code"], r["currency_code"]))
        _write(self.sales_path, ROW_FIELDS, kept)

    def log_day(self, entry):
        kept = [r for r in _read(self.log_path)
                if not (r["date"] == entry["date"] and r["brand"] == entry["brand"])]
        kept.append(entry)
        kept.sort(key=lambda r: (r["date"], r["brand"]))
        _write(self.log_path, LOG_FIELDS, kept)

    def finalize(self):
        # Per-brand files: summed over countries per date and currency.
        totals = defaultdict(lambda: [0, 0.0])
        for r in _read(self.sales_path):
            t = totals[(r["brand"], r["date"], r["currency_code"])]
            t[0] += float(r["conversions"])
            t[1] += float(r["conversion_value"])
        per_brand = defaultdict(list)
        for (brand, d, cur), (n, value) in sorted(totals.items()):
            per_brand[brand].append({"date": d, "conversions": int(n),
                                     "conversion_value": round(value, 2), "currency_code": cur})
        for brand, rows in per_brand.items():
            _write(self.dir / f"pd_{slug(brand)}.csv", PD_FIELDS, rows)
