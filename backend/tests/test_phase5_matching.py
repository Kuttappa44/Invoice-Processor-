"""Phase 5: matching and push services."""

from __future__ import annotations

import inspect
import unittest


class MatchingServiceTests(unittest.TestCase):
    def test_matching_run_is_callable(self):
        from backend.app.modules.matching import service as matching_service

        self.assertTrue(callable(matching_service.run))

    def test_amount_gate_helpers_exist(self):
        from backend.app.modules.matching.amount_gate import (
            _amount_gate_for_row,
            _is_mmt_invoice,
        )

        self.assertTrue(callable(_amount_gate_for_row))
        self.assertTrue(callable(_is_mmt_invoice))

    def test_gst_and_property_helpers(self):
        from backend.app.modules.matching import gst, property as prop

        self.assertTrue(callable(gst.match_by_gst_numbers))
        self.assertTrue(callable(gst.normalize_gst_value))
        self.assertTrue(callable(prop.match_properties))

    def test_bill_duplicate_tolerance(self):
        from backend.app.modules.matching import bill_duplicate

        self.assertTrue(callable(bill_duplicate.compute_duplicate_bill_flag_for_row))
        self.assertEqual(bill_duplicate.DUPLICATE_AMOUNT_TOLERANCE_INR, 10.0)


class PushServiceTests(unittest.TestCase):
    def test_push_row_still_posts_via_integration(self):
        from backend.app.modules.push.service import push_row_to_api

        self.assertIn("post_invoice_payload", inspect.getsource(push_row_to_api))

    def test_remarks_come_from_matching_module(self):
        from backend.app.modules.matching.remarks import generate_matching_remarks

        text = generate_matching_remarks("booking_code", booking_code_matched=1)
        self.assertIn("booking code", text.lower())


class Step7TriggerTests(unittest.TestCase):
    def test_process_invoices_calls_matching_run(self):
        from backend.app.modules.ingest.service import process_invoices
        import inspect as ins

        source = ins.getsource(process_invoices)
        self.assertIn("from backend.app.modules.matching.service import run", source)
        self.assertIn("push=True", source)
        self.assertIn("immediate_push=True", source)

    def test_matching_run_still_reads_only_unpushed(self):
        from backend.app.modules.matching.service import run
        import inspect as ins

        source = ins.getsource(run)
        self.assertIn("only_unpushed=True", source)
        self.assertIn("booking_api.main()", source)


if __name__ == "__main__":
    unittest.main()
