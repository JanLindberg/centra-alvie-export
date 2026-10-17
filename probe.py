"""Probe the Centra Integration API: connection, stores and an order overview.

Prints only aggregates and order numbers, no customer data.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Stockholm")


def load_env(path=".env"):
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def gql(query, variables=None):
    req = urllib.request.Request(
        os.environ["CENTRA_API_URL"],
        data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers={
            "Authorization": f"Bearer {os.environ['CENTRA_API_TOKEN']}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode()[:500]}")
    if body.get("errors"):
        for err in body["errors"]:
            print("  GraphQL error:", err.get("message"))
    return body


ORDER_FIELDS = """
  number status createdAt isInternal
  store { id name }
  country { code }
  shippingAddress { country { code } }
  grandTotal { value currency { code } }
  totals {
    lineValues { value } discounts { value } shipping { value } handling { value }
    taxIncluded { value } taxAdded { value } taxDeducted { value }
    shippingTaxIncluded { value } quantity cancelledQuantity
  }
"""


def fetch_orders(frm, to, limit=100, max_pages=50):
    q = f"""query($from: DateTimeTz!, $to: DateTimeTz!, $page: Int) {{
      orders(where: {{ orderDate: {{ from: $from, to: $to }}, storeType: DIRECT_TO_CONSUMER }},
             sort: [orderDate_ASC], limit: {limit}, page: $page) {{ {ORDER_FIELDS} }}
    }}"""
    out = []
    for page in range(1, max_pages + 1):
        body = gql(q, {"from": frm.isoformat(), "to": to.isoformat(), "page": page})
        rows = (body.get("data") or {}).get("orders") or []
        out += rows
        if len(rows) < limit:
            break
    return out, body.get("extensions", {})


def main():
    load_env()

    print("== Stores")
    body = gql("{ stores { id name type } }")
    for s in body["data"]["stores"]:
        print(f"  {s['id']:>3}  {s['type']:<20} {s['name']}")

    print("\n== Earliest DTC orders")
    body = gql(f"""{{ orders(where: {{ storeType: DIRECT_TO_CONSUMER }}, sort: [orderDate_ASC], limit: 10)
                     {{ number status createdAt isInternal store {{ name }} grandTotal {{ value currency {{ code }} }} }} }}""")
    for o in body["data"]["orders"]:
        print(f"  #{o['number']} {o['createdAt']} {o['store']['name']:<12} {o['status']:<10} "
              f"internal={o['isInternal']} {o['grandTotal']['value']} {o['grandTotal']['currency']['code']}")

    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    to = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    frm = to - timedelta(days=days)
    print(f"\n== Orders {frm.date()} .. {to.date()} (Stockholm time)")
    orders, ext = fetch_orders(frm, to)
    print(f"  fetched {len(orders)} orders; permissionsUsed={ext.get('permissionsUsed')}")

    c = lambda f: Counter(f(o) for o in orders)
    print("  per store/status:", dict(c(lambda o: (o["store"]["name"], o["status"]))))
    print("  isInternal:", dict(c(lambda o: o["isInternal"])))
    print("  currency:", dict(c(lambda o: o["grandTotal"]["currency"]["code"])))
    print("  shipping country:", dict(c(lambda o: (o["shippingAddress"] or {}).get("country", {}).get("code"))))
    mism = [o["number"] for o in orders
            if (o["country"] or {}).get("code") != ((o["shippingAddress"] or {}).get("country") or {}).get("code")]
    print(f"  order.country != shippingAddress.country: {len(mism)} {mism[:10]}")
    zero = [o["number"] for o in orders if o["grandTotal"]["value"] == 0]
    print(f"  zero-value orders: {len(zero)} {zero[:20]}")
    internal = [(o["number"], o["status"], o["grandTotal"]["value"]) for o in orders if o["isInternal"]]
    print(f"  internal orders: {internal[:20]}")
    partial = [o["number"] for o in orders if o["totals"]["cancelledQuantity"] > 0]
    print(f"  orders with cancelled units: {len(partial)} {partial[:10]}")

    print("\n  totals sample (first 5 non-zero):")
    for o in [o for o in orders if o["grandTotal"]["value"] > 0][:5]:
        t = {k: (v["value"] if isinstance(v, dict) else v) for k, v in o["totals"].items()}
        calc = t["lineValues"] + t["discounts"] + t["shipping"] + t["handling"] + t["taxAdded"]
        print(f"  #{o['number']} grandTotal={o['grandTotal']['value']} calc(lines+disc+ship+hand+taxAdded)={round(calc, 2)} {t}")


if __name__ == "__main__":
    main()
