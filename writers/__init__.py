"""Output writers.

Every writer stores the same daily rows and keeps its own load log, so the
destination itself knows which (date, brand) pairs have been delivered.

Interface:
    logged_days(brand) -> set[date]
    replace_day(day, brand, rows)   delete existing rows for (day, brand), then insert rows
    log_day(entry)                  record (day, brand) as delivered; replaces an existing entry
    finalize()                      flush buffered data, refresh per-brand PD outputs
"""
import os
import re

ROW_FIELDS = ["date", "conversions", "conversion_value", "currency_code", "country_code", "brand"]
PD_FIELDS = ["date", "conversions", "conversion_value", "currency_code"]
LOG_FIELDS = ["date", "brand", "run_at", "orders_fetched", "orders_kept", "rows_written"]


def slug(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def make_writers(cfg, names, output_dir=None):
    out = cfg["output"]
    brands = [b["name"] for b in cfg["brands"]]
    writers = []
    for name in names:
        if name == "csv":
            from writers.csv_writer import CsvWriter

            writers.append(CsvWriter(output_dir or out["csv"]["dir"]))
        elif name == "bigquery":
            from writers.bigquery_writer import BigQueryWriter

            # Environment variables select the target per environment (dev, Everhunt).
            bq = out["bigquery"]
            writers.append(BigQueryWriter(os.environ.get("BQ_PROJECT", bq["project"]),
                                          os.environ.get("BQ_DATASET", bq["dataset"]),
                                          os.environ.get("BQ_LOCATION", bq["location"]),
                                          brands, bq.get("batch_days", 30)))
        else:
            raise ValueError(f"Unknown writer: {name}")
    return writers
