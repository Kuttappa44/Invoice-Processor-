"""Phase 1: settings defaults and a single engine factory."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch


class SettingsDefaultTests(unittest.TestCase):
    def test_mysql_defaults_match_previous_hardcoded_values(self):
        from backend.app.config import get_settings

        cleared = {
            "MYSQL_HOST": "",
            "MYSQL_USER": "",
            "MYSQL_PASSWORD": "",
            "MYSQL_DATABASE": "",
            "INVOICE_TABLE_NAME": "",
            "PROCESS_ONLY_UNREAD": "",
            "SKIP_ALREADY_PROCESSED_EMAILS": "",
            "STEP7_EVERY_N_EMAILS": "",
        }
        with patch.dict(os.environ, cleared, clear=False):
            settings = get_settings()
        self.assertEqual(settings.mysql_host, "localhost")
        self.assertEqual(settings.mysql_user, "root")
        self.assertEqual(settings.mysql_password, "Irahirs11@ittak!")
        self.assertEqual(settings.mysql_database, "bsr")
        self.assertEqual(settings.invoice_table_name, "invoice_data")
        self.assertEqual(settings.step7_every_n_emails, 100)
        self.assertFalse(settings.process_only_unread)
        self.assertTrue(settings.skip_already_processed_emails)

    def test_invoice_table_name_sanitizes_like_db_helper(self):
        from backend.app.config import get_settings
        from backend.app.modules.invoices.mysql import get_invoice_table_name

        with patch.dict(os.environ, {"INVOICE_TABLE_NAME": "invoice_data_b"}):
            self.assertEqual(get_settings().invoice_table_name, "invoice_data_b")
            self.assertEqual(get_invoice_table_name(), "invoice_data_b")
        with patch.dict(os.environ, {"INVOICE_TABLE_NAME": "not valid!"}):
            self.assertEqual(get_settings().invoice_table_name, "invoice_data")
            self.assertEqual(get_invoice_table_name(), "invoice_data")


class EngineFactoryTests(unittest.TestCase):
    def test_mysql_helpers_delegate_to_database(self):
        from backend.app.database import engine_url
        from backend.app.modules.invoices.export import get_db_engine as export_engine
        from backend.app.modules.invoices.mysql import get_db_engine as mysql_engine
        from backend.app.modules.invoices.queries import get_db_engine as query_engine

        url = engine_url()
        self.assertTrue(url.startswith("mysql+mysqlconnector://"))
        self.assertIn("@localhost/bsr", url)
        self.assertIn("root:", url)

        with patch(
            "backend.app.database.get_engine", return_value="sentinel-engine"
        ):
            self.assertEqual(mysql_engine(), "sentinel-engine")
            self.assertEqual(query_engine(), "sentinel-engine")
            self.assertEqual(export_engine(), "sentinel-engine")

    def test_process_cancelled_is_the_shared_exception(self):
        from backend.app.exceptions import ProcessCancelled

        self.assertTrue(issubclass(ProcessCancelled, Exception))


if __name__ == "__main__":
    unittest.main()
