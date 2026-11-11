"""Phase 6: ingest conductor owns process_invoices."""

from __future__ import annotations

import inspect
import unittest


class IngestConductorTests(unittest.TestCase):
    def test_main_process_invoices_still_callable_and_syncs_excel_path(self):
        import main

        source = inspect.getsource(main.process_invoices)
        self.assertIn("_sync_to_impl", source)
        self.assertIn("_ingest_process_invoices", source)
        self.assertTrue(callable(main.process_invoices))

    def test_workers_thread_target_goes_through_main(self):
        from backend.app.modules.ingest.workers.tasks import run_process_invoices

        source = inspect.getsource(run_process_invoices)
        self.assertIn("from main import process_invoices", source)
        self.assertIn("cancel_event", source)


class Step7TriggerStillOnConductorTests(unittest.TestCase):
    def test_process_invoices_source_still_has_step7_batch(self):
        from backend.app.modules.ingest.service import process_invoices

        source = inspect.getsource(process_invoices)
        self.assertIn("STEP7_EVERY_N_EMAILS", source)
        self.assertIn("from backend.app.modules.matching.service import run", source)
        self.assertIn("push=True", source)
        self.assertIn("immediate_push=True", source)


class ApiProcessUsesWorkerTests(unittest.TestCase):
    def test_run_task_in_thread_calls_run_process_invoices(self):
        import api_server

        source = inspect.getsource(api_server.run_task_in_thread)
        self.assertIn("run_process_invoices", source)
        self.assertIn("cancel_event=cancel_ev", source)


if __name__ == "__main__":
    unittest.main()
