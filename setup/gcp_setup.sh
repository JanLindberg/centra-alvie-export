#!/usr/bin/env bash
# One-time setup of a Google Cloud environment for the Centra -> Alvie export.
# Idempotent: safe to run again, existing resources are left as they are.
#
# Usage:
#   PROJECT=centra-export-dev setup/gcp_setup.sh
#   PROJECT=<everhunt-project> PD_READER=group:alvie@precisdigital.com setup/gcp_setup.sh
#
# Tables and views are not created here; the export job creates them on first run.
set -euo pipefail

PROJECT="${PROJECT:?Set PROJECT to the Google Cloud project ID}"
DATASET="${DATASET:-alvie_export}"
LOCATION="${LOCATION:-EU}"
SA_NAME="${SA_NAME:-centra-export}"
PD_READER="${PD_READER:-}"   # optional IAM member given read access to the dataset, e.g. group:x@y.com

SA_EMAIL="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

echo "== Project ${PROJECT}"
gcloud config set project "${PROJECT}" >/dev/null

echo "== Enabling APIs"
gcloud services enable bigquery.googleapis.com iam.googleapis.com

echo "== Dataset ${PROJECT}:${DATASET} (${LOCATION})"
if bq show --dataset "${PROJECT}:${DATASET}" >/dev/null 2>&1; then
  echo "   exists"
else
  bq --location="${LOCATION}" mk --dataset \
    --description "Centra D2C web sales for Precis Digital (Alvie)" \
    "${PROJECT}:${DATASET}"
fi
# No expiry: datasets created before billing is enabled (BigQuery sandbox) get 60-day
# defaults, which would silently delete the history.
bq update --default_table_expiration 0 --default_partition_expiration 0 "${PROJECT}:${DATASET}" >/dev/null

echo "== Service account ${SA_EMAIL}"
if gcloud iam service-accounts describe "${SA_EMAIL}" >/dev/null 2>&1; then
  echo "   exists"
else
  gcloud iam service-accounts create "${SA_NAME}" \
    --display-name "Centra export job" \
    --description "Runs the daily Centra -> Alvie export"
fi

echo "== Permissions for the service account"
# Run query jobs in the project (no data access by itself).
gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member "serviceAccount:${SA_EMAIL}" --role roles/bigquery.jobUser \
  --condition None >/dev/null
# Create tables/views and write data, in this dataset only.
# Dataset-level grants via SQL (bq add-iam-policy-binding on datasets needs allowlisting).
grant() {
  bq query --quiet --nouse_legacy_sql --location="${LOCATION}" \
    "GRANT \`$1\` ON SCHEMA \`${PROJECT}.${DATASET}\` TO \"$2\"" >/dev/null
}
grant roles/bigquery.dataEditor "serviceAccount:${SA_EMAIL}"

if [[ -n "${PD_READER}" ]]; then
  echo "== Read access for ${PD_READER}"
  grant roles/bigquery.dataViewer "${PD_READER}"
fi

# To be added with scheduling: Secret Manager (Centra token), Artifact Registry,
# Cloud Run Job and Cloud Scheduler.

echo "== Done"
