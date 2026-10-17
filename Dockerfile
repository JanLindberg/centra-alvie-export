FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY centra_export.py config.toml ./
COPY writers/ writers/

# Arguments (e.g. --writers bigquery) are set on the Cloud Run Job.
ENTRYPOINT ["python", "centra_export.py"]
