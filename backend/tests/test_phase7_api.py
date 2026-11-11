"""Phase 7: FastAPI app lives in backend.app.main; api_server re-exports it."""

from __future__ import annotations

import inspect
import unittest


class AppReexportTests(unittest.TestCase):
    def test_api_server_app_is_backend_app_main(self):
        import api_server
        from backend.app.main import app as composed

        self.assertIs(api_server.app, composed)

    def test_uvicorn_entrypoint_still_api_server_app(self):
        import api_server

        source = inspect.getsource(api_server)
        self.assertIn('uvicorn.run(', source)
        self.assertIn('"api_server:app"', source)

    def test_run_task_in_thread_is_tasks_worker(self):
        import api_server
        from backend.app.modules.tasks.workers.tasks import run_task_in_thread

        self.assertIs(api_server.run_task_in_thread, run_task_in_thread)
        source = inspect.getsource(run_task_in_thread)
        self.assertIn("run_process_invoices", source)
        self.assertIn("cancel_event=cancel_ev", source)


class HttpContractTests(unittest.TestCase):
    def test_health_and_process_validation(self):
        from fastapi.testclient import TestClient
        import api_server

        client = TestClient(api_server.app)
        health = client.get("/api/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["status"], "healthy")

        running = client.get("/api/tasks/running-process")
        self.assertEqual(running.status_code, 200)
        self.assertEqual(running.json(), {"running": False, "task": None})

        missing = client.post("/api/process", json={})
        self.assertEqual(missing.status_code, 400)
        self.assertIn("from_date", missing.json()["detail"])

        inverted = client.post(
            "/api/process",
            json={"from_date": "2026-01-02", "to_date": "2026-01-01"},
        )
        self.assertEqual(inverted.status_code, 400)


class RouteSplitTests(unittest.TestCase):
    def test_process_route_still_has_409_and_force_replace(self):
        from backend.app.modules.tasks.router import start_process_task

        source = inspect.getsource(start_process_task)
        self.assertIn("status_code=409", source)
        self.assertIn("force_replace", source)
        self.assertIn("cancel_event", source)
        self.assertIn("run_task_in_thread", source)

    def test_routers_own_their_paths(self):
        from backend.app.modules.invoices.router import router as invoices
        from backend.app.modules.mailbox.router import router as mailbox
        from backend.app.modules.stats.router import router as stats
        from backend.app.modules.tasks.router import router as tasks

        task_paths = {getattr(r, "path", None) for r in tasks.routes}
        invoice_paths = {getattr(r, "path", None) for r in invoices.routes}
        stats_paths = {getattr(r, "path", None) for r in stats.routes}
        mailbox_paths = {getattr(r, "path", None) for r in mailbox.routes}

        self.assertIn("/api/process", task_paths)
        self.assertIn("/api/match-master", task_paths)
        self.assertIn("/api/tasks/{task_id}", task_paths)
        self.assertIn("/api/tasks/{task_id}/cancel", task_paths)
        self.assertIn("/api/mysql/rows", invoice_paths)
        self.assertIn("/api/mysql/stats", stats_paths)
        self.assertIn("/api/mysql/match-rate-trend", stats_paths)
        self.assertIn("/api/mysql/mailbox-counts", mailbox_paths)


if __name__ == "__main__":
    unittest.main()
