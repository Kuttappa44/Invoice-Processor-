"""Public seams used by cron and the API must stay importable."""

from __future__ import annotations

import unittest


class ImportContractTests(unittest.TestCase):
    def test_main_exposes_process_invoices(self):
        import main

        self.assertTrue(callable(main.process_invoices))

    def test_main_exposes_match_master_sheet(self):
        import main

        self.assertTrue(callable(main.match_master_sheet_with_excel))

    def test_main_exposes_count_unread_for_scheduled_followup(self):
        import main

        fn = getattr(main, "count_unread_emails_for_date_range", None)
        self.assertTrue(
            callable(fn),
            "run_scheduled.sh does `from main import count_unread_emails_for_date_range`",
        )

    def test_process_cancelled_is_the_shared_exception(self):
        from backend.app.exceptions import ProcessCancelled

        self.assertTrue(issubclass(ProcessCancelled, Exception))

    def test_api_server_keeps_existing_routes(self):
        import api_server

        paths = {getattr(route, "path", None) for route in api_server.app.routes}
        required = {
            "/api/process",
            "/api/match-master",
            "/api/tasks/running-process",
            "/api/tasks/{task_id}",
            "/api/tasks/{task_id}/cancel",
            "/api/mysql/stats",
            "/api/mysql/rows",
            "/api/mysql/match-rate-trend",
            "/api/mysql/mailbox-counts",
            "/api/health",
            "/api/config",
        }
        missing = required - paths
        self.assertEqual(missing, set(), f"API routes missing: {sorted(missing)}")


if __name__ == "__main__":
    unittest.main()
