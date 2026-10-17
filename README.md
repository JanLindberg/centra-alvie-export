# Centra → Alvie sales export

Daily export of Centra D2C web sales to BigQuery, as input for Precis Digital's
attribution model Alvie. One row per order date (Europe/Stockholm), brand,
delivery country and currency:

| column | type | |
|---|---|---|
| date | DATE | order date, Swedish time |
| conversions | FLOAT | number of orders |
| conversion_value | FLOAT | sum of order value, local currency |
| currency_code | STRING | ISO 4217 |
| country_code | STRING | delivery country, ISO 3166-1 alpha-2 |
| brand | STRING | Centra store |

Views `pd_<brand>` give PD's four columns (date, conversions, conversion_value,
currency_code) summed per day and currency.

## Rules

- Only orders from the D2C stores, from each brand's `start_date` (first real sales day after go-live).
- Cancelled orders (`DELETED` in Centra) are excluded as they are when the job runs; delivered days are not rewritten.
- Orders with product value below `min_product_value` (0 kr, replacement orders, test orders) and block-listed order numbers are excluded.
- Whether VAT and shipping are included in `conversion_value` is set in `config.toml`.

## Catch-up

Each writer keeps a `load_log` of delivered (date, brand) pairs. A run processes every
day from `start_date` to yesterday that is not in the log, so missed runs are caught up
and the first run loads the full history. In BigQuery, data and log for a batch of days
are written in one transaction.

## Usage

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env    # fill in CENTRA_API_TOKEN

.venv/bin/python centra_export.py                                        # daily run
.venv/bin/python centra_export.py --from 2026-10-13 --to 2026-10-15 --dry-run
.venv/bin/python centra_export.py --from 2026-10-13 --to 2026-10-15 --output-dir test_output
.venv/bin/python centra_export.py --writers bigquery --from 2026-10-15 --to 2026-10-15 --force
```

`probe.py` explores the Centra API (stores, order statuses, totals) without writing anything.

## Google Cloud setup

```
gcloud auth login && gcloud auth application-default login
PROJECT=<project-id> setup/gcp_setup.sh
```

Creates the dataset (no expiry), the `centra-export` service account and its permissions.
`PD_READER=group:...` grants Precis Digital read access to the dataset.
Tables and views are created by the export on its first run.
