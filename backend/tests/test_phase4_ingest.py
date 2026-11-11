"""Phase 4: ingest internals live in modules/ingest."""

from __future__ import annotations

import os
import unittest


class PromptParseTests(unittest.TestCase):
    def test_prompt_txt_still_loaded_from_prompts(self):
        from backend.app.modules.ingest.extractors.prompt_parse import get_extraction_prompt

        prompt = get_extraction_prompt()
        self.assertTrue(prompt)
        self.assertIn("Guest", prompt)

    def test_parse_output_to_dict_reads_bold_equals_fields(self):
        from backend.app.modules.ingest.extractors.prompt_parse import parse_output_to_dict

        raw = (
            "**Guest Name** = Ada Lovelace\n"
            "**Hotel Name** = Test Hotel\n"
            "**Bill Number** = INV-1 / Page : 1 / 1\n"
            "**Amount** = 1,234.50\n"
            "**HBD GST** = 27AABCH5874R1ZR\n"
            "**Client GST** = 29ABCDE1234F1Z5\n"
        )
        data = parse_output_to_dict(raw, "sample.pdf")
        self.assertEqual(data["guest_name"], "Ada Lovelace")
        self.assertEqual(data["hotel_name"], "Test Hotel")
        self.assertEqual(data["bill_number"], "INV-1")
        self.assertEqual(data["amount"], "1,234.50")
        self.assertEqual(data["filename"], "sample.pdf")


class VisionAndPdfPipelineTests(unittest.TestCase):
    def test_vision_and_pipeline_are_callable(self):
        from backend.app.modules.ingest import pdf_pipeline
        from backend.app.modules.ingest.extractors import vision

        self.assertTrue(callable(vision.convert_file_to_jpegs))
        self.assertTrue(callable(vision.extract_structured_with_gpt_pdf_images))
        self.assertTrue(callable(pdf_pipeline.process_pdf_bytes))


class ClassifierTests(unittest.TestCase):
    def test_classifier_helpers_are_ingest_classifiers(self):
        from backend.app.modules.ingest.classifiers import hummingbird_logo, multi_invoice_pdf

        self.assertTrue(callable(multi_invoice_pdf.normalize_classification))
        self.assertTrue(callable(multi_invoice_pdf.classify_single_invoice))
        self.assertTrue(callable(hummingbird_logo.invoice_contains_hummingbird_logo))

    def test_classify_prompts_still_resolve_next_to_prompts(self):
        from backend.app.modules.ingest.classifiers.multi_invoice_pdf import (
            _package_detection_prompt_path,
        )

        path = _package_detection_prompt_path()
        self.assertTrue(
            str(path).endswith(os.path.join("prompts", "invoice_detection_prompt.txt"))
        )
        self.assertTrue(path.is_file())


class EmailParseTests(unittest.TestCase):
    def test_invoice_keyword_detection_unchanged(self):
        from backend.app.modules.ingest.email_parse import is_invoice_email

        self.assertTrue(is_invoice_email("Hotel invoice attached", "please find the bill"))
        self.assertFalse(is_invoice_email("Out of office", "I am away"))


class PersistTests(unittest.TestCase):
    def test_match_suffix_inserts_before_extension(self):
        from backend.app.modules.ingest.persist import pdf_filename_with_match_suffix

        self.assertEqual(pdf_filename_with_match_suffix("a.pdf", 1), "a_match1.pdf")


class Main1RemovedTests(unittest.TestCase):
    def test_main1_is_gone(self):
        from pathlib import Path

        self.assertFalse(
            (Path(__file__).resolve().parents[2] / "invoice_processor").exists()
        )


if __name__ == "__main__":
    unittest.main()
