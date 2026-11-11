"""Lock current matching, GST, classification, and row-conversion behavior.

These tests import the live modules. They must stay green after every move.
Do not change thresholds or flags to make a test pass — the production function is the spec.
"""

from __future__ import annotations

import os
import unittest
from datetime import date
from unittest.mock import patch

import pandas as pd


class BookingCodeNormalizeTests(unittest.TestCase):
    def test_integer_float_string_becomes_int(self):
        from backend.app.modules.matching.excel import normalize_booking_code_value

        self.assertEqual(normalize_booking_code_value("123.0"), "123")

    def test_strips_quotes_spaces_commas_and_lowercases(self):
        from backend.app.modules.matching.excel import normalize_booking_code_value

        self.assertEqual(normalize_booking_code_value(' "AB C,1" '), "abc1")

    def test_empty_and_nan(self):
        from backend.app.modules.matching.excel import normalize_booking_code_value

        self.assertEqual(normalize_booking_code_value(""), "")
        self.assertEqual(normalize_booking_code_value(float("nan")), "")


class FieldTextNormalizeTests(unittest.TestCase):
    def test_collapses_whitespace_and_nbsp(self):
        from backend.app.shared.text import normalize_field_text

        self.assertEqual(normalize_field_text("  A\xa0 B\nC  "), "A B C")

    def test_none_and_empty(self):
        from backend.app.shared.text import normalize_field_text

        self.assertEqual(normalize_field_text(None), "")
        self.assertEqual(normalize_field_text(""), "")


class GuestNameMatchTests(unittest.TestCase):
    def test_identical_names_match(self):
        from backend.app.shared.guest_names import guest_names_match_for_merge

        self.assertTrue(guest_names_match_for_merge("Varun Kuttappa", "Varun Kuttappa"))

    def test_title_is_stripped(self):
        from backend.app.shared.guest_names import guest_names_match_for_merge

        self.assertTrue(guest_names_match_for_merge("Mr Varun Kuttappa", "Varun Kuttappa"))

    def test_empty_does_not_match(self):
        from backend.app.shared.guest_names import guest_names_match_for_merge

        self.assertFalse(guest_names_match_for_merge("", "Varun"))
        self.assertFalse(guest_names_match_for_merge("Varun", ""))

    def test_unrelated_names_do_not_match(self):
        from backend.app.shared.guest_names import guest_names_match_for_merge

        self.assertFalse(guest_names_match_for_merge("Alpha Beta", "Gamma Delta"))


class GstinValidationTests(unittest.TestCase):
    VALID = "27AABCH5874R1ZR"

    def test_valid_gstin(self):
        from backend.app.shared.gstin import is_valid_gstin

        self.assertTrue(is_valid_gstin(self.VALID))

    def test_rejects_short_and_none(self):
        from backend.app.shared.gstin import is_valid_gstin

        self.assertFalse(is_valid_gstin(None))
        self.assertFalse(is_valid_gstin("27AABCH5874R1Z"))
        self.assertFalse(is_valid_gstin(""))

    def test_rejects_bad_state_code(self):
        from backend.app.shared.gstin import is_valid_gstin

        self.assertFalse(is_valid_gstin("00AABCH5874R1ZR"))

    def test_mysql_field_keeps_invalid_segment_and_flags(self):
        from backend.app.shared.gstin import gst_field_value_for_mysql_with_invalid

        stored, invalid = gst_field_value_for_mysql_with_invalid("NOTAGSTIN")
        self.assertEqual(stored, "NOTAGSTIN")
        self.assertTrue(invalid)

    def test_not_found_is_empty_not_invalid(self):
        from backend.app.shared.gstin import gst_field_value_for_mysql_with_invalid

        stored, invalid = gst_field_value_for_mysql_with_invalid("Not Found")
        self.assertIsNone(stored)
        self.assertFalse(invalid)


class ClassificationNormalizeTests(unittest.TestCase):
    def test_canonical_labels(self):
        from backend.app.modules.ingest.classifiers.multi_invoice_pdf import (
            normalize_classification,
        )

        self.assertEqual(normalize_classification("B2B"), "b2b")
        self.assertEqual(normalize_classification("food_invoice"), "food")
        self.assertEqual(normalize_classification("MakeMyTrip"), "mmt")
        self.assertEqual(normalize_classification("hummingbird_mail"), "hummingbird")
        self.assertEqual(normalize_classification("not an invoice"), "not_invoice")
        self.assertIsNone(normalize_classification("other"))
        self.assertIsNone(normalize_classification("hotel_stay"))


class ClassificationFlagEarlyExitTests(unittest.TestCase):
    def test_missing_id_or_unknown_label_does_not_need_db(self):
        from backend.app.modules.invoices.mysql import (
            update_classification_flags_in_mysql,
        )

        self.assertFalse(update_classification_flags_in_mysql(None, "b2b"))
        self.assertFalse(update_classification_flags_in_mysql(1, None))
        self.assertFalse(update_classification_flags_in_mysql(1, "other"))
        self.assertFalse(update_classification_flags_in_mysql(1, "hotel_stay"))


class PropertyMatchTests(unittest.TestCase):
    def test_subset_keywords_match(self):
        from backend.app.modules.matching.property import match_properties

        self.assertTrue(match_properties("The Hotel Taj", "Taj Hotel"))

    def test_unrelated_names_do_not_match(self):
        from backend.app.modules.matching.property import match_properties

        self.assertFalse(match_properties("Alpha Lodge", "Beta Residency"))

    def test_both_empty_match(self):
        from backend.app.modules.matching.property import match_properties

        self.assertTrue(match_properties("", ""))


class AmountAndDateGateTests(unittest.TestCase):
    def test_tolerance_is_ten_rupees(self):
        from backend.app.shared.money import AMOUNT_GATE_TOLERANCE_INR

        self.assertEqual(AMOUNT_GATE_TOLERANCE_INR, 10.0)

    def test_normalize_amount(self):
        from backend.app.shared.money import normalize_amount as _normalize_amount

        self.assertEqual(_normalize_amount("1,234.50"), 1234.50)
        self.assertIsNone(_normalize_amount("Not Found"))
        self.assertIsNone(_normalize_amount(None))

    def test_mmt_flag_skips_amount_gate(self):
        from backend.app.modules.matching.amount_gate import (
            _amount_gate_for_row,
            _is_mmt_invoice,
        )

        row = pd.Series({"mmt_flag": 1})
        self.assertTrue(_is_mmt_invoice(row))
        ok, reason, debug = _amount_gate_for_row(
            row,
            pd.DataFrame(),
            {},
            {},
            None,
            None,
            None,
            None,
            None,
            None,
        )
        self.assertTrue(ok)
        self.assertIsNone(reason)
        self.assertEqual(debug.get("reason"), "mmt_flag")

    def test_mmt_flag_zero_is_not_mmt(self):
        from backend.app.modules.matching.amount_gate import _is_mmt_invoice

        self.assertFalse(_is_mmt_invoice(pd.Series({"mmt_flag": 0})))

    def test_date_match_plus_minus_one_day(self):
        from backend.app.shared.dates import date_match_pm1 as _date_match_pm1

        self.assertTrue(_date_match_pm1("01/09/26", "01/09/26"))
        self.assertTrue(_date_match_pm1("01/09/26", "02/09/26"))
        self.assertFalse(_date_match_pm1("01/09/26", "03/09/26"))


class MysqlRowConversionTests(unittest.TestCase):
    def test_not_found_becomes_none_and_amount_parsed(self):
        from backend.app.modules.invoices.mysql import convert_row_for_mysql

        row = convert_row_for_mysql(
            {
                "Hotel Name": "Not Found",
                "Guest Name": "Ada Lovelace",
                "Amount Paid": "1,234.50",
                "Bill Date": "22/09/2025",
                "HBD GST Number": "Not Found",
                "Client GST Number": "Not Found",
            }
        )
        self.assertIsNone(row["hotel_name"])
        self.assertEqual(row["guest_name"], "Ada Lovelace")
        self.assertEqual(row["amount_paid"], 1234.50)
        self.assertEqual(row["bill_date"], date(2025, 9, 22))
        self.assertEqual(row["validation_flag"], 0)

    def test_timestamp_stripped_to_date(self):
        from backend.app.modules.invoices.mysql import convert_row_for_mysql

        row = convert_row_for_mysql({"Email Date": "2026-04-01 14:01:34"})
        self.assertEqual(row["email_date"], date(2026, 4, 1))


class InvoiceTableNameTests(unittest.TestCase):
    def test_default_and_invalid_names(self):
        from backend.app.modules.invoices.mysql import get_invoice_table_name

        with patch.dict(os.environ, {"INVOICE_TABLE_NAME": ""}, clear=False):
            self.assertEqual(get_invoice_table_name(), "invoice_data")
        with patch.dict(os.environ, {"INVOICE_TABLE_NAME": "invoice_data_b"}):
            self.assertEqual(get_invoice_table_name(), "invoice_data_b")
        with patch.dict(os.environ, {"INVOICE_TABLE_NAME": "drop table;"}):
            self.assertEqual(get_invoice_table_name(), "invoice_data")


class ImapSafetyTests(unittest.TestCase):
    def test_default_is_all_mail_not_unread_only(self):
        from backend.app.integrations.gmail_imap import only_unread_mode

        with patch.dict(os.environ, {"PROCESS_ONLY_UNREAD": "false"}):
            self.assertFalse(only_unread_mode())

    def test_fetch_uses_body_peek(self):
        from backend.app.integrations import gmail_imap
        import inspect

        source = inspect.getsource(gmail_imap.fetch_rfc822)
        self.assertIn("BODY.PEEK[]", source)

    def test_unread_followup_returns_zero_when_not_unread_mode(self):
        from backend.app.modules.ingest.unread import count_unread_emails_for_date_range

        with patch(
            "backend.app.modules.ingest.unread.only_unread_mode",
            return_value=False,
        ):
            self.assertEqual(
                count_unread_emails_for_date_range("2026-01-01", "2026-01-01"),
                0,
            )


class DateFormatTests(unittest.TestCase):
    def test_iso_to_dd_mm_yy(self):
        from backend.app.shared.dates import format_date_to_dd_mm_yy

        self.assertEqual(format_date_to_dd_mm_yy("2024-01-01"), "01/01/24")

    def test_not_found_passthrough(self):
        from backend.app.shared.dates import format_date_to_dd_mm_yy

        self.assertEqual(format_date_to_dd_mm_yy("Not Found"), "Not Found")
        self.assertEqual(format_date_to_dd_mm_yy(None), "Not Found")


if __name__ == "__main__":
    unittest.main()
