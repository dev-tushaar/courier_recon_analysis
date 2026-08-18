#!/bin/sh
set -e

echo "Waiting for database..."
python <<'PY'
import os
import sys
import time

import dj_database_url
import psycopg2

url = os.environ["DATABASE_URL"]
cfg = dj_database_url.parse(url)
host, port = cfg["HOST"], cfg.get("PORT") or 5432

for attempt in range(30):
    try:
        conn = psycopg2.connect(
            dbname=cfg["NAME"],
            user=cfg["USER"],
            password=cfg["PASSWORD"],
            host=host,
            port=port,
            sslmode=os.getenv("PGSSLMODE", "prefer"),
        )
        conn.close()
        break
    except psycopg2.OperationalError:
        time.sleep(1)
else:
    sys.exit("Database is not reachable.")
PY

python manage.py migrate --noinput
python manage.py collectstatic --noinput

# Public demo bootstrap. Both steps are idempotent, so a redeploy or a free-tier
# container restart will not duplicate data or reset the password.
if [ "${SEED_DEMO}" = "true" ]; then
    python <<'PY'
import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.core.management import call_command

from reconciliation.models import Courier

# Seed only on an empty database; otherwise a restart would stack demo data.
if not Courier.objects.exists():
    print("Seeding demo data...")
    call_command("seed_demo", shipments=600)
else:
    print("Demo data already present, skipping seed.")
PY
fi

# Render supplies $PORT and routes to it; default keeps local Docker on 8000.
exec gunicorn config.wsgi:application \
    --bind "0.0.0.0:${PORT:-8000}" \
    --workers "${WEB_CONCURRENCY:-2}" \
    --timeout "${GUNICORN_TIMEOUT:-120}"
