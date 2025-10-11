.PHONY: seed seed-staff diagnose simulate simulate-start integration-sync web labels-demo migrate compliance-migrate compliance-export

PYTHON ?= python3
RUN := PYTHONPATH=. $(PYTHON)

seed:
	@$(RUN) scripts/seed_inventory.py

seed-staff:
	@$(RUN) scripts/seed_staff.py

diagnose:
	@$(RUN) scripts/diagnose_odoo.py

simulate: migrate
	@$(RUN) -m services.simulator.run once

simulate-start: migrate
	@$(RUN) -m services.simulator.run start

integration-sync:
	@$(RUN) services/integration/runner.py sync

web:
	@$(RUN) -m apps.web.main

labels-demo:
	@$(RUN) scripts/labels_demo.py

migrate:
	@$(RUN) scripts/db_migrate.py >/dev/null

compliance-migrate:
	@$(RUN) scripts/db_migrate.py >/dev/null

compliance-export:
	@$(RUN) - <<'PY'
import datetime
import zipfile
from pathlib import Path
from services.compliance import CSV_HEADERS, resolve_csv_path

csv_path = resolve_csv_path(None)
csv_path.parent.mkdir(parents=True, exist_ok=True)
if not csv_path.exists():
    header = ",".join(CSV_HEADERS) + "\n"
    csv_path.write_text(header, encoding="utf-8")
timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
archive_path = csv_path.parent / f"export_{timestamp}.zip"
with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    archive.write(csv_path, arcname=csv_path.name)
print(archive_path)
PY
