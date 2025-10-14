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
	@$(RUN) -c $$'import datetime, zipfile\nfrom services.compliance import CSV_HEADERS, resolve_csv_path\n\ncsv_path = resolve_csv_path(None)\ncsv_path.parent.mkdir(parents=True, exist_ok=True)\nif not csv_path.exists():\n    csv_path.write_text(",".join(CSV_HEADERS) + "\\n", encoding="utf-8")\ntimestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")\narchive_path = csv_path.parent / f"export_{timestamp}.zip"\nwith zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:\n    archive.write(csv_path, arcname=csv_path.name)\nprint(archive_path)'
