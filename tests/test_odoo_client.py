"""Tests for the Odoo XML-RPC client abstraction."""
from __future__ import annotations

import os
from unittest import TestCase
from unittest.mock import MagicMock, patch

import xmlrpc.client

from packages.odoo_client import OdooClient, OdooClientError


class TestOdooClient(TestCase):
    def setUp(self) -> None:
        self.env_backup = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.env_backup)

    def _set_env(self) -> None:
        os.environ.update(
            {
                "ODOO_URL": "https://odoo.example.com",
                "ODOO_DB": "foodflow",
                "ODOO_USERNAME": "admin",
                "ODOO_PASSWORD": "secret",
            }
        )

    def test_missing_env_variables_raise(self) -> None:
        os.environ.pop("ODOO_URL", None)
        os.environ.pop("ODOO_DB", None)
        os.environ.pop("ODOO_USERNAME", None)
        os.environ.pop("ODOO_PASSWORD", None)
        with self.assertRaises(OdooClientError):
            OdooClient()

    def test_authenticate_success(self) -> None:
        self._set_env()
        with patch.object(xmlrpc.client, "ServerProxy") as proxy_cls:
            common_proxy = MagicMock()
            object_proxy = MagicMock()
            proxy_cls.side_effect = [common_proxy, object_proxy]
            common_proxy.authenticate.return_value = 42

            client = OdooClient()
            uid = client.authenticate()

            self.assertEqual(uid, 42)
            common_proxy.authenticate.assert_called_once_with("foodflow", "admin", "secret", {})

    def test_search_read_uses_execute_kw(self) -> None:
        self._set_env()
        with patch.object(xmlrpc.client, "ServerProxy") as proxy_cls:
            common_proxy = MagicMock()
            object_proxy = MagicMock()
            proxy_cls.side_effect = [common_proxy, object_proxy]
            common_proxy.authenticate.return_value = 7
            object_proxy.execute_kw.return_value = [{"id": 10}]

            client = OdooClient()
            client.authenticate()
            result = client.search_read("res.partner", [("name", "=", "Demo")], fields=["name"], limit=1)

            self.assertEqual(result, [{"id": 10}])
            object_proxy.execute_kw.assert_called_once()
            call_args = object_proxy.execute_kw.call_args[0]
            self.assertEqual(call_args[0], "foodflow")
            self.assertEqual(call_args[3], "res.partner")
            self.assertEqual(call_args[4], "search_read")

    def test_write_wraps_single_id(self) -> None:
        self._set_env()
        with patch.object(xmlrpc.client, "ServerProxy") as proxy_cls:
            common_proxy = MagicMock()
            object_proxy = MagicMock()
            proxy_cls.side_effect = [common_proxy, object_proxy]
            common_proxy.authenticate.return_value = 5
            object_proxy.execute_kw.return_value = True

            client = OdooClient()
            client.authenticate()
            updated = client.write("res.partner", 12, {"name": "Updated"})

            self.assertTrue(updated)
            object_proxy.execute_kw.assert_called_once()
            args, kwargs = object_proxy.execute_kw.call_args
            self.assertEqual(args[4], "write")
            self.assertEqual(args[5][0], [12])
