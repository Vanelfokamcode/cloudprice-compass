#!/bin/bash
set -e

echo "=== CloudPrice Compass startup ==="

if [ ! -f "data/compass.duckdb" ]; then
  echo "DB absente — ingestion en cours..."
  mkdir -p data

  python ingest/aws_pricing.py
  python ingest/gcp_pricing.py
  python ingest/azure_pricing.py

  cd transforms && dbt run --profiles-dir . && cd ..
  echo "DB prête."
else
  echo "DB existante — skip ingestion."
fi

echo "Démarrage API..."
uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000}
