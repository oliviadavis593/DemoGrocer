"""Seed demo staff accounts with predefined roles and permissions."""
from __future__ import annotations

import json
import pathlib
import secrets
import sys
from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence

from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))

from packages.odoo_client import OdooClient, OdooClientError  # noqa: E402


OUTPUT_PATH = ROOT / ".out" / "staff_credentials.json"


@dataclass(frozen=True)
class StaffUser:
    login: str
    name: str
    role: str
    group_xmlids: Sequence[str]


STAFF_USERS: Sequence[StaffUser] = (
    StaffUser(
        login="cashier_1",
        name="Cashier 1",
        role="Cashier",
        group_xmlids=("base.group_user",),
    ),
    StaffUser(
        login="cashier_2",
        name="Cashier 2",
        role="Cashier",
        group_xmlids=("base.group_user",),
    ),
    StaffUser(
        login="dept_mgr_produce",
        name="Produce Department Manager",
        role="Inventory Manager",
        group_xmlids=("base.group_user", "stock.group_stock_user", "stock.group_stock_manager"),
    ),
    StaffUser(
        login="store_mgr",
        name="Store Manager",
        role="Administrator-lite",
        group_xmlids=("base.group_user", "stock.group_stock_manager", "base.group_erp_manager"),
    ),
)


def _load_existing_credentials(path: pathlib.Path) -> Dict[str, Dict[str, str]]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return {str(k): dict(v) for k, v in data.items()}
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise RuntimeError(f"Credentials file at {path} is not valid JSON.") from exc
    raise RuntimeError(f"Credentials file at {path} must contain a JSON object.")


def _resolve_group_ids(client: OdooClient, xmlids: Iterable[str]) -> List[int]:
    group_ids: List[int] = []
    for xmlid in xmlids:
        module, _, name = xmlid.partition(".")
        if not module or not name:
            raise RuntimeError(f"Invalid XML ID '{xmlid}'. Expected format 'module.record'.")
        matches = client.search_read(
            "ir.model.data",
            [("module", "=", module), ("name", "=", name)],
            fields=["model", "res_id"],
            limit=1,
        )
        if not matches:
            raise RuntimeError(f"Could not find XML ID '{xmlid}' in ir.model.data.")
        record = matches[0]
        if record.get("model") != "res.groups":
            raise RuntimeError(f"XML ID '{xmlid}' does not point to a res.groups record.")
        group_ids.append(int(record["res_id"]))
    return group_ids


def _ensure_groups(client: OdooClient, user_id: int, desired_groups: Sequence[int]) -> bool:
    existing = client.search_read(
        "res.users",
        [("id", "=", user_id)],
        fields=["groups_id"],
        limit=1,
    )
    if not existing:
        raise RuntimeError(f"User id {user_id} no longer exists.")
    current = set(existing[0].get("groups_id", []))
    desired = set(desired_groups)
    if current == desired:
        return False
    client.write("res.users", user_id, {"groups_id": [(6, 0, sorted(desired_groups))]})
    return True


def seed_staff() -> None:
    credentials = _load_existing_credentials(OUTPUT_PATH)

    client = OdooClient()
    client.authenticate()

    results: Dict[str, str] = {}
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    for staff in STAFF_USERS:
        group_ids = _resolve_group_ids(client, staff.group_xmlids)
        entry = credentials.get(staff.login, {})
        password = entry.get("password") or secrets.token_urlsafe(16)

        existing = client.search_read(
            "res.users",
            [("login", "=", staff.login)],
            fields=["id"],
            limit=1,
        )

        if existing:
            user_id = int(existing[0]["id"])
            updated_groups = _ensure_groups(client, user_id, group_ids)
            state = "updated groups" if updated_groups else "exists"
            print(f"{staff.login}: {state}")
        else:
            client.create(
                "res.users",
                {
                    "name": staff.name,
                    "login": staff.login,
                    "password": password,
                    "groups_id": [(6, 0, sorted(group_ids))],
                },
            )
            print(f"{staff.login}: created")

        results[staff.login] = {"password": password, "role": staff.role}

    with OUTPUT_PATH.open("w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=2, sort_keys=True)
        handle.write("\n")


def main() -> None:
    try:
        seed_staff()
    except (OdooClientError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
