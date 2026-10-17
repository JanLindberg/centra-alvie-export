import json
import logging

from google.cloud import bigquery

from writers import slug

log = logging.getLogger("centra_export")

SALES_SCHEMA = [
    bigquery.SchemaField("date", "DATE", mode="REQUIRED", description="Order date, Europe/Stockholm"),
    bigquery.SchemaField("conversions", "FLOAT64", mode="REQUIRED", description="Number of orders"),
    bigquery.SchemaField("conversion_value", "FLOAT64", mode="REQUIRED", description="Sum of order value, local currency"),
    bigquery.SchemaField("currency_code", "STRING", mode="REQUIRED", description="ISO 4217"),
    bigquery.SchemaField("country_code", "STRING", mode="REQUIRED", description="Delivery country, ISO 3166-1 alpha-2"),
    bigquery.SchemaField("brand", "STRING", mode="REQUIRED", description="Centra store"),
]

LOG_SCHEMA = [
    bigquery.SchemaField("date", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("brand", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("run_at", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("orders_fetched", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("orders_kept", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("rows_written", "INT64", mode="REQUIRED"),
]

# Data and log for a batch of (date, brand) pairs are replaced in one transaction,
# so a day is either fully delivered and logged, or not at all.
FLUSH_SCRIPT = """
BEGIN TRANSACTION;
DELETE FROM `{sales}` WHERE CONCAT(CAST(date AS STRING), '|', brand) IN UNNEST(@keys);
INSERT INTO `{sales}` (date, conversions, conversion_value, currency_code, country_code, brand)
SELECT DATE(JSON_VALUE(r, '$.date')),
       CAST(JSON_VALUE(r, '$.conversions') AS FLOAT64),
       CAST(JSON_VALUE(r, '$.conversion_value') AS FLOAT64),
       JSON_VALUE(r, '$.currency_code'),
       JSON_VALUE(r, '$.country_code'),
       JSON_VALUE(r, '$.brand')
FROM UNNEST(JSON_QUERY_ARRAY(@rows)) AS r;
DELETE FROM `{log}` WHERE CONCAT(CAST(date AS STRING), '|', brand) IN UNNEST(@keys);
INSERT INTO `{log}` (date, brand, run_at, orders_fetched, orders_kept, rows_written)
SELECT DATE(JSON_VALUE(e, '$.date')),
       JSON_VALUE(e, '$.brand'),
       TIMESTAMP(JSON_VALUE(e, '$.run_at')),
       CAST(JSON_VALUE(e, '$.orders_fetched') AS INT64),
       CAST(JSON_VALUE(e, '$.orders_kept') AS INT64),
       CAST(JSON_VALUE(e, '$.rows_written') AS INT64)
FROM UNNEST(JSON_QUERY_ARRAY(@entries)) AS e;
COMMIT TRANSACTION;
"""

PD_VIEW = """
CREATE OR REPLACE VIEW `{view}`
OPTIONS (description = "Precis Digital (Alvie) input for {brand}: orders per day and currency")
AS
SELECT date,
       SUM(conversions) AS conversions,
       ROUND(SUM(conversion_value), 2) AS conversion_value,
       currency_code
FROM `{sales}`
WHERE brand = {brand_literal}
GROUP BY date, currency_code
"""


class BigQueryWriter:
    """sales_daily (partitioned by date), load_log and one pd_<brand> view per brand.

    Days are buffered and written in batches of `batch_days` (date, brand) pairs,
    since each write is a BigQuery job taking a few seconds.
    """

    def __init__(self, project, dataset, location, brands, batch_days=30):
        self.client = bigquery.Client(project=project, location=location)
        self.prefix = f"{project}.{dataset}"
        self.sales = f"{self.prefix}.sales_daily"
        self.log = f"{self.prefix}.load_log"
        self.brands = brands
        self.batch_days = batch_days
        self.pending_rows, self.pending_entries = [], []
        self._ensure_tables()

    def _ensure_tables(self):
        sales = bigquery.Table(self.sales, schema=SALES_SCHEMA)
        sales.time_partitioning = bigquery.TimePartitioning(field="date")
        sales.clustering_fields = ["brand"]
        sales.description = "Centra D2C web sales per day, brand, delivery country and currency"
        self.client.create_table(sales, exists_ok=True)
        load_log = bigquery.Table(self.log, schema=LOG_SCHEMA)
        load_log.description = "One row per (date, brand) delivered to sales_daily"
        self.client.create_table(load_log, exists_ok=True)

    def _query(self, sql, params):
        return self.client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params)).result()

    def logged_days(self, brand):
        rows = self._query(f"SELECT date FROM `{self.log}` WHERE brand = @brand",
                           [bigquery.ScalarQueryParameter("brand", "STRING", brand)])
        return {r.date for r in rows}

    def replace_day(self, day, brand, rows):
        self.pending_rows += rows

    def log_day(self, entry):
        self.pending_entries.append(entry)
        if len(self.pending_entries) >= self.batch_days:
            self.flush()

    def flush(self):
        if not self.pending_entries:
            return
        keys = [f"{e['date']}|{e['brand']}" for e in self.pending_entries]
        self._query(FLUSH_SCRIPT.format(sales=self.sales, log=self.log), [
            bigquery.ArrayQueryParameter("keys", "STRING", keys),
            bigquery.ScalarQueryParameter("rows", "STRING", json.dumps(self.pending_rows)),
            bigquery.ScalarQueryParameter("entries", "STRING", json.dumps(self.pending_entries)),
        ])
        log.info("BigQuery: wrote %d day(s), %d row(s)", len(keys), len(self.pending_rows))
        self.pending_rows, self.pending_entries = [], []

    def finalize(self):
        self.flush()
        for brand in self.brands:
            view = f"{self.prefix}.pd_{slug(brand)}"
            # Views can't take query parameters; json.dumps gives a valid quoted string literal.
            self._query(PD_VIEW.format(view=view, sales=self.sales, brand=brand,
                                       brand_literal=json.dumps(brand)), [])
