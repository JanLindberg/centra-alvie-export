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
PROJECT=<project-id> ALERT_EMAIL=<email>[,<email>...] setup/gcp_setup.sh   # once per environment
PROJECT=<project-id> setup/deploy.sh                          # after every code change
```

`gcp_setup.sh` enables the APIs, creates the dataset (no expiry), the `centra-export`
service account and its permissions, stores the Centra token from `.env` in Secret Manager
(`centra-api-token`) and creates an e-mail alert on failed runs.
`PD_READER=group:...` grants Precis Digital read access to the dataset.

`deploy.sh` builds the image with Cloud Build and deploys the Cloud Run Job `centra-export`
(europe-north1), started daily at 03:00 Europe/Stockholm by Cloud Scheduler job
`centra-export-daily` (europe-west1; Scheduler isn't available in europe-north1).
Tables and views are created by the export on its first run, which also loads the full history.

```
gcloud run jobs execute centra-export --region europe-north1                     # run now
gcloud run jobs execute centra-export --region europe-north1 \
  --args="--writers=bigquery,--from=2026-10-12,--to=2026-10-12,--force"         # redo a day
printf '%s' "<new token>" | gcloud secrets versions add centra-api-token --data-file=-   # rotate token
```

The Centra token expires in 2035.

## Alerts

E-mail to each address in `ALERT_EMAIL` (comma-separated) via Cloud Monitoring:

| Alert | Fires when |
|---|---|
| Centra export job failed | A run fails after Cloud Run's retry (Centra down, expired token, BigQuery error, bug). Missed days are caught up by the next successful run. |
| Centra export no successful run | No successful run for 26 hours, e.g. the scheduler is paused or billing is disabled. |
| Centra export data check | A brand had no orders on a day (`DATA_CHECK` in the log). The day is logged as empty and not retried, so this fires once; rerun the day with `--force` if Centra was wrong. |
