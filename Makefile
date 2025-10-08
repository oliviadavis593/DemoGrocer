.PHONY: seed diagnose
seed:
	@PYTHONPATH=. python3 scripts/seed_inventory.py
diagnose:
	@PYTHONPATH=. python3 scripts/diagnose_odoo.py
