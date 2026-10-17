#!/usr/bin/env bash
# Build and deploy the export as a Cloud Run Job, and schedule it daily.
# Run after setup/gcp_setup.sh, and again after every code change. Idempotent.
#
# Usage:
#   PROJECT=centra-export-dev setup/deploy.sh
set -euo pipefail

PROJECT="${PROJECT:?Set PROJECT to the Google Cloud project ID}"
REGION="${REGION:-europe-north1}"
SCHEDULER_REGION="${SCHEDULER_REGION:-europe-west1}"   # Cloud Scheduler isn't available in europe-north1
DATASET="${DATASET:-alvie_export}"
SA_NAME="${SA_NAME:-centra-export}"
SECRET="${SECRET:-centra-api-token}"
JOB="${JOB:-centra-export}"
SCHEDULE="${SCHEDULE:-0 3 * * *}"
TIME_ZONE="${TIME_ZONE:-Europe/Stockholm}"
CENTRA_API_URL="${CENTRA_API_URL:-$(grep '^CENTRA_API_URL=' .env | cut -d= -f2-)}"

SA_EMAIL="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
cd "$(dirname "$0")/.."

echo "== Deploying Cloud Run Job ${JOB} (${REGION})"
gcloud run jobs deploy "${JOB}" --quiet \
  --project "${PROJECT}" --region "${REGION}" --source . \
  --service-account "${SA_EMAIL}" \
  --set-secrets "CENTRA_API_TOKEN=${SECRET}:latest" \
  --set-env-vars "CENTRA_API_URL=${CENTRA_API_URL},BQ_PROJECT=${PROJECT},BQ_DATASET=${DATASET}" \
  --args="--writers=bigquery" \
  --tasks 1 --max-retries 1 --task-timeout 3600 --memory 1Gi

echo "== Allowing ${SA_EMAIL} to start the job"
gcloud run jobs add-iam-policy-binding "${JOB}" --project "${PROJECT}" --region "${REGION}" \
  --member "serviceAccount:${SA_EMAIL}" --role roles/run.invoker >/dev/null

echo "== Schedule '${SCHEDULE}' ${TIME_ZONE} (${SCHEDULER_REGION})"
SCHEDULER_ARGS=(
  --project "${PROJECT}" --location "${SCHEDULER_REGION}"
  --schedule "${SCHEDULE}" --time-zone "${TIME_ZONE}"
  --uri "https://run.googleapis.com/v2/projects/${PROJECT}/locations/${REGION}/jobs/${JOB}:run"
  --http-method POST --oauth-service-account-email "${SA_EMAIL}"
)
if gcloud scheduler jobs describe "${JOB}-daily" --project "${PROJECT}" --location "${SCHEDULER_REGION}" >/dev/null 2>&1; then
  gcloud scheduler jobs update http "${JOB}-daily" "${SCHEDULER_ARGS[@]}" >/dev/null
else
  gcloud scheduler jobs create http "${JOB}-daily" "${SCHEDULER_ARGS[@]}" \
    --description "Daily Centra -> Alvie export" >/dev/null
fi

echo "== Done. Run now with: gcloud run jobs execute ${JOB} --region ${REGION} --project ${PROJECT}"
