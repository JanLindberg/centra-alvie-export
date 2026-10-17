#!/usr/bin/env bash
# One-time setup of a Google Cloud environment for the Centra -> Alvie export.
# Idempotent: safe to run again, existing resources are left as they are.
#
# Usage:
#   PROJECT=centra-export-dev ALERT_EMAIL=me@example.com setup/gcp_setup.sh
#   PROJECT=<everhunt-project> ALERT_EMAIL=... PD_READER=group:alvie@precisdigital.com setup/gcp_setup.sh
#
# The Centra token is read from .env (CENTRA_API_TOKEN) the first time the secret is created.
# Tables and views are not created here; the export job creates them on first run.
# The job itself and its schedule are deployed with setup/deploy.sh.
set -euo pipefail

PROJECT="${PROJECT:?Set PROJECT to the Google Cloud project ID}"
DATASET="${DATASET:-alvie_export}"
LOCATION="${LOCATION:-EU}"
SA_NAME="${SA_NAME:-centra-export}"
PD_READER="${PD_READER:-}"   # optional IAM member given read access to the dataset, e.g. group:x@y.com
ALERT_EMAIL="${ALERT_EMAIL:?Set ALERT_EMAIL to the address that gets failure alerts}"
SECRET="${SECRET:-centra-api-token}"
JOB="${JOB:-centra-export}"

SA_EMAIL="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

echo "== Project ${PROJECT}"
gcloud config set project "${PROJECT}" >/dev/null

echo "== Enabling APIs"
gcloud services enable bigquery.googleapis.com iam.googleapis.com run.googleapis.com \
  cloudscheduler.googleapis.com secretmanager.googleapis.com artifactregistry.googleapis.com \
  cloudbuild.googleapis.com monitoring.googleapis.com

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

echo "== Build permissions"
# deploy.sh builds with Cloud Build (gcloud run jobs deploy --source), which runs as the
# default compute service account. New projects don't give it build permissions.
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT}" --format 'value(projectNumber)')"
gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member "serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role roles/cloudbuild.builds.builder --condition None >/dev/null

echo "== Secret ${SECRET}"
if ! gcloud secrets describe "${SECRET}" >/dev/null 2>&1; then
  gcloud secrets create "${SECRET}" --replication-policy automatic >/dev/null
fi
if [[ -z "$(gcloud secrets versions list "${SECRET}" --filter state=ENABLED --format 'value(name)')" ]]; then
  TOKEN="$(grep '^CENTRA_API_TOKEN=' .env | cut -d= -f2-)"
  [[ -n "${TOKEN}" ]] || { echo "No CENTRA_API_TOKEN in .env" >&2; exit 1; }
  printf '%s' "${TOKEN}" | gcloud secrets versions add "${SECRET}" --data-file=- >/dev/null
  echo "   token added from .env"
else
  echo "   has a version (rotate with: gcloud secrets versions add ${SECRET} --data-file=-)"
fi
gcloud secrets add-iam-policy-binding "${SECRET}" \
  --member "serviceAccount:${SA_EMAIL}" --role roles/secretmanager.secretAccessor >/dev/null

echo "== Alerts to ${ALERT_EMAIL}"
# Notification channels are only in gcloud beta, so use the REST API.
API="https://monitoring.googleapis.com/v3/projects/${PROJECT}/notificationChannels"
AUTH="Authorization: Bearer $(gcloud auth print-access-token)"
CHANNEL="$(curl -sf -H "${AUTH}" "${API}" | python3 -c '
import json, sys
email = sys.argv[1]
for c in json.load(sys.stdin).get("notificationChannels", []):
    if c["type"] == "email" and c["labels"].get("email_address") == email:
        print(c["name"]); break' "${ALERT_EMAIL}")"
if [[ -z "${CHANNEL}" ]]; then
  CHANNEL="$(curl -sf -X POST -H "${AUTH}" -H "Content-Type: application/json" "${API}" \
    -d "{\"type\": \"email\", \"displayName\": \"${ALERT_EMAIL}\", \"labels\": {\"email_address\": \"${ALERT_EMAIL}\"}}" |
    python3 -c 'import json, sys; print(json.load(sys.stdin)["name"])')"
fi
# create_policy NAME JSON-FILE: create an alert policy unless one with that name exists.
create_policy() {
  if [[ -z "$(gcloud monitoring policies list --filter "displayName=\"$1\"" --format 'value(name)' 2>/dev/null)" ]]; then
    gcloud monitoring policies create --policy-from-file "$2" >/dev/null
    echo "   created: $1"
  else
    echo "   exists: $1"
  fi
  rm -f "$2"
}
JOB_FILTER="resource.type = \\\"cloud_run_job\\\" AND resource.labels.job_name = \\\"${JOB}\\\""

# A. The job ran and failed (after Cloud Run's retry).
POLICY_FILE="$(mktemp)"
cat > "${POLICY_FILE}" <<EOF
{
  "displayName": "Centra export job failed",
  "combiner": "OR",
  "conditions": [{
    "displayName": "Cloud Run Job ${JOB} execution failed",
    "conditionThreshold": {
      "filter": "${JOB_FILTER} AND metric.type = \\"run.googleapis.com/job/completed_execution_count\\" AND metric.labels.result = \\"failed\\"",
      "aggregations": [{"alignmentPeriod": "300s", "perSeriesAligner": "ALIGN_SUM"}],
      "comparison": "COMPARISON_GT",
      "thresholdValue": 0,
      "duration": "0s"
    }
  }],
  "alertStrategy": {"autoClose": "86400s"},
  "notificationChannels": ["${CHANNEL}"],
  "documentation": {
    "mimeType": "text/markdown",
    "content": "The daily Centra -> Alvie export failed after retries. Check the job logs in Cloud Run. Missed days are caught up automatically by the next successful run. If every run fails with an authorization error, the Centra API token may have expired."
  }
}
EOF
create_policy "Centra export job failed" "${POLICY_FILE}"

# B. No successful run for 26 hours: the job didn't start at all (scheduler paused or
# failing, billing disabled). PromQL, since metric-absence conditions can't span a day.
POLICY_FILE="$(mktemp)"
cat > "${POLICY_FILE}" <<EOF
{
  "displayName": "Centra export no successful run",
  "combiner": "OR",
  "conditions": [{
    "displayName": "No successful ${JOB} execution in 26 hours",
    "conditionPrometheusQueryLanguage": {
      "query": "absent_over_time(run_googleapis_com:job_completed_execution_count{monitored_resource=\\"cloud_run_job\\",job_name=\\"${JOB}\\",result=\\"succeeded\\"}[26h])",
      "duration": "0s",
      "evaluationInterval": "300s"
    }
  }],
  "alertStrategy": {"autoClose": "86400s"},
  "notificationChannels": ["${CHANNEL}"],
  "documentation": {
    "mimeType": "text/markdown",
    "content": "The Centra -> Alvie export has not completed successfully for 26 hours. Check that Cloud Scheduler job ${JOB}-daily is enabled and its last attempt succeeded, and that billing is active. Missed days are caught up by the next successful run."
  }
}
EOF
create_policy "Centra export no successful run" "${POLICY_FILE}"

# C. Data check: a brand had no orders on a day. The day is logged, so this alerts once.
POLICY_FILE="$(mktemp)"
cat > "${POLICY_FILE}" <<EOF
{
  "displayName": "Centra export data check",
  "combiner": "OR",
  "conditions": [{
    "displayName": "Day with no orders",
    "conditionMatchedLog": {
      "filter": "${JOB_FILTER} AND textPayload:\\"DATA_CHECK\\""
    }
  }],
  "alertStrategy": {"notificationRateLimit": {"period": "3600s"}, "autoClose": "86400s"},
  "notificationChannels": ["${CHANNEL}"],
  "documentation": {
    "mimeType": "text/markdown",
    "content": "The export found a day where a brand had no orders (see the DATA_CHECK line in the job log). The day has been logged as empty and will not be retried. Verify in Centra; if orders exist, rerun that day: gcloud run jobs execute ${JOB} --region <region> --args=--writers=bigquery,--from=DATE,--to=DATE,--force"
  }
}
EOF
create_policy "Centra export data check" "${POLICY_FILE}"

echo "== Done"
