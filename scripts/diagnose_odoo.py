from pathlib import Path
import sys

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))

from packages.odoo_client import OdooClient


def main() -> None:
    client = OdooClient()
    client.authenticate()
    print(f"DB name: {client.database}")
    stock_lot_exists = bool(
        client.search_read("ir.model", [("model", "=", "stock.lot")], ["id"], limit=1)
    )
    print(f"stock.lot present: {str(stock_lot_exists).lower()}")
    life_date_exists = bool(
        client.search_read(
            "ir.model.fields",
            [("model", "=", "stock.lot"), ("name", "=", "life_date")],
            ["id"],
            limit=1,
        )
    )
    print(f"life_date present: {str(life_date_exists).lower()}")


if __name__ == "__main__":
    main()
