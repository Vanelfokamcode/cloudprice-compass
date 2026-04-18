#!/bin/bash
set -e
echo "=== CloudPrice Compass ==="
echo "Démarrage API..."
uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000}
