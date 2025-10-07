import os
import xmlrpc.client
from dotenv import load_dotenv

# Load credentials from .env file
load_dotenv()

ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USERNAME = os.getenv("ODOO_USERNAME")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

# Step 1: Authenticate
common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common", allow_none=True)
models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", allow_none=True)

uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})

if not uid:
    raise Exception("Authentication failed. Check your credentials.")

print(f"✅ Authenticated to Odoo as UID: {uid}")

# Step 2: Pull product names and on-hand quantities
products = models.execute_kw(
    ODOO_DB, uid, ODOO_PASSWORD,
    'product.product', 'search_read',
    [[]],
    {'fields': ['name', 'qty_available'], 'limit': 50}
)

print("\n🛒 Product Inventory:")
for p in products:
    print(f"- {p['name']} | On Hand: {p['qty_available']}")
