"""Daily export of Centra D2C web sales to Precis Digital (Alvie).

One row per date (Stockholm time), brand, delivery country and currency.
Each run processes every day from the brand's start_date up to yesterday that is
not yet in the destination's load log, so missed runs are caught up automatically.

Examples:
    python3 centra_export.py                                    # normal daily run
    python3 centra_export.py --from 2026-10-14 --to 2026-10-15 --output-dir test_output
    python3 centra_export.py --from 2026-10-14 --to 2026-10-14 --dry-run
    python3 centra_export.py --from 2026-10-12 --to 2026-10-12 --force   # redo a logged day
    python3 centra_export.py --writers bigquery --from 2026-10-14 --to 2026-10-15
"""
import argparse
import json
import logging
import os
import sys
import time
import tomllib
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from writers import make_writers

log = logging.getLogger("centra_export")

ORDERS_QUERY = """
query Orders($from: DateTimeTz!, $to: DateTimeTz!, $storeId: Int!, $limit: Int!, $page: Int!) {
  orders(
    where: { orderDate: { from: $from, to: $to }, storeId: [$storeId], storeType: DIRECT_TO_CONSUMER }
    sort: [orderDate_ASC, number_ASC]
    limit: $limit
    page: $page
  ) {
    number
    status
    orderDate
    shippingAddress { country { code } }
    grandTotal { value currency { code } }
    totals {
      lineValues { value }
      discounts { value }
      shipping { value }
      handling { value }
      taxIncluded { value }
      taxAdded { value }
      shippingTaxIncluded { value }
      shippingTaxAdded { value }
    }
  }
}
"""


def load_env(path=".env"):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


class CentraClient:
    def __init__(self, url, token, retries=4):
        self.url = url
        self.token = token
        self.retries = retries

    def query(self, query, variables):
        payload = json.dumps({"query": query, "variables": variables}).encode()
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(self.url, data=payload, headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            })
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    body = json.load(r)
                break
            except urllib.error.HTTPError as e:
                if e.code not in (429, 500, 502, 503, 504) or attempt == self.retries:
                    raise RuntimeError(f"Centra HTTP {e.code}: {e.read().decode()[:500]}") from e
            except urllib.error.URLError as e:
                if attempt == self.retries:
                    raise RuntimeError(f"Centra unreachable: {e}") from e
            wait = 2 ** attempt * 5
            log.warning("Centra request failed, retrying in %ss", wait)
            time.sleep(wait)
        # Fail hard on any GraphQL error: a partial result must never be logged as a delivered day.
        if body.get("errors"):
            raise RuntimeError(f"Centra GraphQL errors: {[e.get('message') for e in body['errors']]}")
        return body["data"]


def money(m):
    return (m or {}).get("value") or 0.0


def fetch_day(client, store_id, day, tz, page_size):
    """All orders for one store whose order date (in tz) is `day`."""
    start = datetime.combine(day, dtime.min, tz)
    end = datetime.combine(day + timedelta(days=1), dtime.min, tz)
    orders, seen, page = [], set(), 1
    while True:
        rows = client.query(ORDERS_QUERY, {
            "from": start.isoformat(), "to": end.isoformat(),
            "storeId": store_id, "limit": page_size, "page": page,
        })["orders"]
        for o in rows:
            # The range end may be inclusive, so re-check the local date; dedupe across pages.
            if o["number"] not in seen and datetime.fromisoformat(o["orderDate"]).astimezone(tz).date() == day:
                seen.add(o["number"])
                orders.append(o)
        if len(rows) < page_size:
            return orders
        page += 1


def product_value(o):
    t = o["totals"]
    return money(t["lineValues"]) + money(t["discounts"])


def order_value(o, include_vat, include_shipping):
    """grandTotal = lineValues + discounts + shipping + handling + taxAdded.
    taxIncluded holds all included VAT, shipping VAT (shippingTaxIncluded) among it."""
    t = o["totals"]
    value = money(o["grandTotal"])
    ship_tax = money(t["shippingTaxIncluded"]) + money(t["shippingTaxAdded"])
    if not include_shipping:
        value -= money(t["shipping"]) + money(t["shippingTaxAdded"])
    if not include_vat:
        value -= money(t["taxIncluded"]) + money(t["taxAdded"])
        if not include_shipping:
            value += ship_tax  # already removed together with shipping
    return value


def check_totals(o):
    t = o["totals"]
    expected = (money(t["lineValues"]) + money(t["discounts"]) + money(t["shipping"])
                + money(t["handling"]) + money(t["taxAdded"]))
    if abs(expected - money(o["grandTotal"])) > 0.01:
        log.warning("Order %s: grandTotal %.2f != components %.2f, value settings may be off",
                    o["number"], money(o["grandTotal"]), expected)


def keep_order(o, cfg):
    if o["status"] in cfg["exclude_statuses"]:
        return False
    if o["number"] in cfg["exclude_order_numbers"]:
        return False
    if product_value(o) < cfg["min_product_value"]:
        return False
    return True


def aggregate(orders, day, brand, cfg):
    sums = defaultdict(lambda: [0, 0.0])
    for o in orders:
        check_totals(o)
        country = ((o["shippingAddress"] or {}).get("country") or {}).get("code") or "XX"
        key = (country, o["grandTotal"]["currency"]["code"])
        sums[key][0] += 1
        sums[key][1] += order_value(o, cfg["value"]["include_vat"], cfg["value"]["include_shipping"])
    return [
        {"date": day.isoformat(), "conversions": n, "conversion_value": round(max(value, 0.0), 2),
         "currency_code": currency, "country_code": country, "brand": brand}
        for (country, currency), (n, value) in sorted(sums.items())
    ]


def pending_days(brand_cfg, logged, first, last, force):
    start = max(brand_cfg["start_date"], first) if first else brand_cfg["start_date"]
    days = [start + timedelta(days=i) for i in range((last - start).days + 1)]
    return days if force else [d for d in days if d not in logged]


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config.toml")
    p.add_argument("--from", dest="first", type=date.fromisoformat, help="first date to process (default: brand start_date)")
    p.add_argument("--to", dest="last", type=date.fromisoformat, help="last date to process (default: yesterday)")
    p.add_argument("--force", action="store_true", help="reprocess days in range even if already logged")
    p.add_argument("--dry-run", action="store_true", help="print rows, write nothing")
    p.add_argument("--output-dir", help="override output directory (csv writer)")
    p.add_argument("--writers", help="comma-separated writers, overrides config (e.g. csv,bigquery)")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with open(args.config, "rb") as f:
        cfg = tomllib.load(f)
    load_env()
    tz = ZoneInfo(cfg["timezone"])
    yesterday = datetime.now(tz).date() - timedelta(days=1)
    last = min(args.last, yesterday) if args.last else yesterday
    if args.force and not (args.first and args.last):
        p.error("--force requires --from and --to")

    client = CentraClient(os.environ["CENTRA_API_URL"], os.environ["CENTRA_API_TOKEN"])
    names = args.writers.split(",") if args.writers else cfg["output"]["writers"]
    writers = [] if args.dry_run else make_writers(cfg, names, args.output_dir)

    for brand_cfg in cfg["brands"]:
        brand = brand_cfg["name"]
        # A day counts as delivered only when every writer has logged it.
        logged = set.intersection(*(w.logged_days(brand) for w in writers)) if writers else set()
        days = pending_days(brand_cfg, logged, args.first, last, args.force)
        log.info("%s: %d day(s) to process", brand, len(days))
        for day in days:
            orders = fetch_day(client, brand_cfg["store_id"], day, tz, cfg["page_size"])
            kept = [o for o in orders if keep_order(o, cfg)]
            rows = aggregate(kept, day, brand, cfg)
            log.info("%s %s: %d orders fetched, %d kept, %d rows", brand, day, len(orders), len(kept), len(rows))
            if not kept:
                # The day is still logged, so this alerts once (log-based alert on DATA_CHECK)
                # instead of retrying every night. Rerun with --force if Centra was wrong.
                log.error("DATA_CHECK %s %s: %d orders fetched, 0 kept. Day logged as empty; "
                          "verify in Centra and rerun with --force if wrong.", brand, day, len(orders))
            if args.dry_run:
                for r in rows:
                    print(r)
                continue
            entry = {"date": day.isoformat(), "brand": brand, "run_at": datetime.now(tz).isoformat(timespec="seconds"),
                     "orders_fetched": len(orders), "orders_kept": len(kept), "rows_written": len(rows)}
            for w in writers:
                w.replace_day(day, brand, rows)
                w.log_day(entry)

    for w in writers:
        w.finalize()


if __name__ == "__main__":
    sys.exit(main())
