.PHONY: seed seed-staff diagnose simulate simulate-start integration-sync web labels-demo migrate

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
