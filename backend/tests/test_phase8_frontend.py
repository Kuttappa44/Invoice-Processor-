"""Phase 8: Vite SPA split; screens call lib/api, same endpoint strings."""

from __future__ import annotations

from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "frontend" / "src"


class FrontendSplitTests(unittest.TestCase):
    def test_app_is_thin_shell_over_dashboard_page(self):
        text = (SRC / "App.tsx").read_text(encoding="utf-8")
        self.assertIn('from "./app/dashboard/page"', text)
        self.assertIn("DashboardPage", text)
        self.assertNotIn("/api/mysql/stats", text)
        self.assertNotIn("Run Full Flow", text)

    def test_client_keeps_live_api_paths(self):
        text = (SRC / "lib" / "api" / "client.ts").read_text(encoding="utf-8")
        for path in (
            "/api/mysql/stats",
            "/api/mysql/mailbox-counts",
            "/api/mysql/match-rate-trend",
            "/api/tasks/running-process",
            "/api/mysql/rows",
            "/api/mysql/export-by-date",
            "/api/tasks/${taskId}",
            "/api/process",
            "/api/match-master",
            "/api/logo",
        ):
            self.assertIn(path, text, f"missing {path}")

    def test_screens_do_not_hardcode_mysql_urls(self):
        dashboard = SRC / "app" / "dashboard"
        for path in dashboard.glob("*.tsx"):
            if path.name == "page.tsx":
                continue
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(
                "/api/mysql/",
                text,
                f"{path.name} should call lib/api, not /api/mysql/",
            )

    def test_run_full_flow_stays_disabled(self):
        text = (SRC / "app" / "dashboard" / "dashboard-header.tsx").read_text(
            encoding="utf-8"
        )
        self.assertIn("Run Full Flow", text)
        self.assertIn("disabled={true}", text)

    def test_task_runner_posts_through_client(self):
        text = (SRC / "hooks" / "useTaskRunner.ts").read_text(encoding="utf-8")
        self.assertIn("postProcessTask", text)
        self.assertIn("postMatchMasterTask", text)
        self.assertIn("getTask", text)
        self.assertNotIn("API_BASE_URL", text)


if __name__ == "__main__":
    unittest.main()
