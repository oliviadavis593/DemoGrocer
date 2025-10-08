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
    print("DB:", client.database)
    print(
        "stock.lot present:",
        bool(
            client.search_read(
                "ir.model", [("model", "=", "stock.lot")], ["id"], limit=1
            )
        ),
    )
    print(
        "life_date field present:",
        bool(
            client.search_read(
                "ir.model.fields",
                [("model", "=", "stock.lot"), ("name", "=", "life_date")],
                ["id"],
                limit=1,
            )
        ),
    )


if __name__ == "__main__":
    main()
