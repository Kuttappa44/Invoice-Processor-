"""Phase 3: shared helpers live in backend.app.shared."""

from __future__ import annotations

from datetime import date
import unittest


class TextHelperTests(unittest.TestCase):
    def test_excel_match_uses_shared_text(self):
        from backend.app.modules.matching import excel
        from backend.app.shared import text

        self.assertIs(excel.normalize_column_name, text.normalize_column_name)
        self.assertIs(excel.normalize_booking_code_value, text.normalize_booking_code_value)


class GstinTests(unittest.TestCase):
    def test_gstin_helpers_are_exported(self):
        from backend.app.shared import gstin

        self.assertTrue(callable(gstin.is_valid_gstin))
        self.assertTrue(callable(gstin.gst_field_value_for_mysql_with_invalid))
        self.assertTrue(callable(gstin.gst_field_value_for_mysql))


class DateHelperTests(unittest.TestCase):
    def test_mysql_uses_shared_format_date(self):
        from backend.app.modules.invoices import mysql
        from backend.app.shared import dates

        self.assertIs(mysql.format_date_to_dd_mm_yy, dates.format_date_to_dd_mm_yy)

    def test_cutover_is_1_sep_2026_and_shared(self):
        from backend.app.modules.invoices import queries
        from backend.app.shared.dates import EMAIL_DATE_CUTOVER

        self.assertEqual(EMAIL_DATE_CUTOVER, date(2026, 9, 1))
        self.assertIs(queries.EMAIL_DATE_CUTOVER, EMAIL_DATE_CUTOVER)


class MoneyAndMatchingTests(unittest.TestCase):
    def test_matching_service_aliases_shared_money_and_dates(self):
        from backend.app.modules.matching import service as matching_service
        from backend.app.shared import dates, money

        self.assertEqual(matching_service.AMOUNT_GATE_TOLERANCE_INR, 10.0)
        self.assertIs(matching_service.AMOUNT_GATE_TOLERANCE_INR, money.AMOUNT_GATE_TOLERANCE_INR)
        self.assertIs(matching_service._normalize_amount, money.normalize_amount)
        self.assertIs(matching_service._to_date, dates.to_date)
        self.assertIs(matching_service._date_delta, dates.date_delta)
        self.assertIs(matching_service._date_match_pm1, dates.date_match_pm1)
        self.assertIs(
            matching_service._date_passes_row_match_style,
            dates.date_passes_row_match_style,
        )


class GuestNameTests(unittest.TestCase):
    def test_guest_match_is_callable(self):
        from backend.app.shared.guest_names import guest_names_match_for_merge

        self.assertTrue(guest_names_match_for_merge("Ada Lovelace", "Ada Lovelace"))


class PdfPageTests(unittest.TestCase):
    def test_missing_bytes_are_not_multi_page(self):
        from backend.app.shared.pdf_pages import count_pdf_pages, is_multi_page_pdf

        self.assertIsNone(count_pdf_pages(None))
        self.assertIsNone(count_pdf_pages(b""))
        self.assertFalse(is_multi_page_pdf(None))
        self.assertFalse(is_multi_page_pdf(b"%PDF"))


if __name__ == "__main__":
    unittest.main()
