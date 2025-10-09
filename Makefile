.PHONY: seed seed-staff diagnose simulate simulate-start web labels-demo migrate

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

web:
	@$(RUN) -m apps.web.main

labels-demo:
	@$(RUN) scripts/labels_demo.py

migrate:
	@$(RUN) scripts/db_migrate.py >/dev/null
