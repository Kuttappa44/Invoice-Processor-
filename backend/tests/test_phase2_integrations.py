"""Phase 2: integrations own IMAP, blob, booking API, and push HTTP."""

from __future__ import annotations

import inspect
import unittest


class GmailImapTests(unittest.TestCase):
    def test_peek_fetch_still_in_integration_source(self):
        from backend.app.integrations.gmail_imap import fetch_rfc822

        self.assertIn("BODY.PEEK[]", inspect.getsource(fetch_rfc822))


class BlobTests(unittest.TestCase):
    def test_sanitize_strips_sas_query(self):
        from backend.app.integrations.azure_blob import sanitize_blob_url

        url = "https://acct.blob.core.windows.net/c/folder/a.pdf?sv=2024&sig=secret"
        self.assertEqual(
            sanitize_blob_url(url),
            "https://acct.blob.core.windows.net/c/folder/a.pdf",
        )


class OpenAIClientTests(unittest.TestCase):
    def test_client_is_exported_from_integration(self):
        from backend.app.integrations.azure_openai import OPENAI_AVAILABLE, client

        self.assertTrue(isinstance(OPENAI_AVAILABLE, bool))
        self.assertTrue(client is None or hasattr(client, "chat") or hasattr(client, "responses"))


class BookingApiTests(unittest.TestCase):
    def test_booking_api_exposes_main_and_fetch(self):
        from backend.app.integrations import booking_api

        self.assertTrue(callable(booking_api.main))
        self.assertTrue(callable(booking_api.fetch_api_data))


class InvoicePushTests(unittest.TestCase):
    def test_token_and_endpoint_come_from_integration(self):
        from backend.app.integrations import invoice_push_api

        self.assertTrue(callable(invoice_push_api.get_api_bearer_token))
        self.assertTrue(callable(invoice_push_api.refresh_api_token))
        self.assertTrue(invoice_push_api.API_PUSH_ENDPOINT)

    def test_push_row_calls_integration_post(self):
        from backend.app.modules.push.service import push_row_to_api

        self.assertIn("post_invoice_payload", inspect.getsource(push_row_to_api))


if __name__ == "__main__":
    unittest.main()
