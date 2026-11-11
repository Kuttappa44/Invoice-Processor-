"""Live modules no longer depend on leftover_impl1 re-exports."""

from __future__ import annotations

import inspect
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class VendorAndAmountTests(unittest.TestCase):
    def test_vendor_helpers_are_ingest_vendor(self):
        from backend.app.modules.ingest import vendor

        self.assertTrue(callable(vendor.find_vendor_column))
        self.assertTrue(callable(vendor.upload_pdf_to_blob_storage))
        self.assertTrue(callable(vendor.get_drive_folder_name))

    def test_amounts_live_on_email_parse(self):
        from backend.app.modules.ingest import email_parse

        amounts = email_parse.extract_amounts("Total Rs. 1,234.50 urgent")
        self.assertIn(1234.5, amounts)
        self.assertEqual(email_parse.extract_urgency_level("please send ASAP"), "URGENT")

    def test_match_master_lives_in_matching_master(self):
        from backend.app.modules.matching import master

        self.assertTrue(callable(master.match_master_sheet_with_excel))
        self.assertTrue(callable(master.match_invoice_data_with_excel))


class ExcelMatchTests(unittest.TestCase):
    def test_excel_match_source_does_not_import_legacy_impl1(self):
        text = (ROOT / "backend" / "app" / "modules" / "matching" / "excel.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("legacy_impl1", text)
        self.assertIn("EXCEL_FILE_PATH", text)

    def test_find_matching_column_is_matching_excel(self):
        from backend.app.modules.matching import excel

        self.assertTrue(callable(excel.find_matching_column))
        src = inspect.getsource(excel)
        self.assertNotIn("legacy_impl1", src)


class IngestNoLegacyImportTests(unittest.TestCase):
    def test_ingest_service_does_not_import_legacy_impl1(self):
        from backend.app.modules.ingest import service

        src = inspect.getsource(service)
        self.assertNotIn("legacy_impl1", src)
        self.assertIn("from backend.app.modules.ingest.vendor import", src)

    def test_process_invoices_still_runs_step7(self):
        from backend.app.modules.ingest.service import process_invoices

        source = inspect.getsource(process_invoices)
        self.assertIn("STEP7_EVERY_N_EMAILS", source)
        self.assertIn("from backend.app.modules.matching.service import run", source)
        self.assertIn("push=True", source)


class CronShimTests(unittest.TestCase):
    def test_main_still_exports_cron_entrypoints(self):
        import main

        self.assertTrue(callable(main.process_invoices))
        self.assertTrue(callable(main.count_unread_emails_for_date_range))
        self.assertTrue(callable(main.match_master_sheet_with_excel))
        src = inspect.getsource(main.process_invoices)
        self.assertIn("_sync_to_impl", src)
        self.assertIn("_ingest_process_invoices", src)


if __name__ == "__main__":
    unittest.main()
