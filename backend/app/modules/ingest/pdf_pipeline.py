"""PDF bytes -> vision extraction -> parsed invoice dict(s)."""

from __future__ import annotations

import os
import tempfile

from backend.app.modules.ingest.extractors.prompt_parse import (
    get_extraction_prompt,
    parse_output_to_dict,
)
from backend.app.modules.ingest.extractors.vision import (
    convert_file_to_jpegs,
    extract_structured_with_gpt_pdf_images,
    prepare_image_inputs,
)

def process_pdf_bytes(pdf_bytes, filename, api_key=None):
    """Process PDF bytes: image-based vision extraction; multi-page PDFs use Stage 1 detection + per-invoice extraction."""
    try:
        temp_pdf = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        temp_pdf.write(pdf_bytes)
        temp_pdf.close()
        temp_pdf_path = temp_pdf.name

        try:
            print(f"   📄 Processing PDF {filename} using image-based extraction ")

            image_paths = convert_file_to_jpegs(temp_pdf_path)
            pdf_page_count = len(image_paths)
            if pdf_page_count < 1:
                print(f"   ⚠️  No pages rendered from PDF")
                return "", None, 0

            def _cleanup_jpegs_only():
                for img_path in image_paths:
                    try:
                        if os.path.exists(img_path) and img_path != temp_pdf_path:
                            os.unlink(img_path)
                    except Exception:
                        pass

            if pdf_page_count <= 1:
                # Single-page PDF: Stage-1 skipped. Build image inputs once, reuse for
                # extraction and for the dedicated classification call (Prompt B).
                single_image_inputs = prepare_image_inputs(image_paths)
                structured_output, pdf_page_count = extract_structured_with_gpt_pdf_images(
                    temp_pdf_path,
                    filename,
                    image_paths=image_paths,
                    image_inputs=single_image_inputs,
                )
                if structured_output:
                    parsed_single = parse_output_to_dict(structured_output, filename)
                    try:
                        from backend.app.modules.ingest.classifiers.multi_invoice_pdf import classify_single_invoice
                        single_cls = classify_single_invoice(single_image_inputs, filename)
                        if isinstance(parsed_single, dict) and single_cls:
                            parsed_single["_classification"] = single_cls
                            print(f"   🏷️  Classification (single-page): {single_cls}")
                    except Exception as _cls_err:
                        print(f"   ⚠️  Single-page classification failed (non-fatal): {_cls_err}")
                    return "", parsed_single, pdf_page_count
                print(f"   ⚠️  No structured output received from GPT")
                return "", None, pdf_page_count

            from backend.app.modules.ingest.classifiers.multi_invoice_pdf import (
                build_scoped_extraction_instructions,
                detect_invoice_page_segments,
            )

            import time as _time
            import requests as _requests

            print(f"   📤 Multi-page PDF ({pdf_page_count} pages): upload + Stage 1 detection + Stage 2 extraction")
            image_inputs = prepare_image_inputs(image_paths)

            print(f"   ⏳ Waiting 2 seconds for image URLs to propagate...")
            _time.sleep(2)

            def _head_ok(url: str) -> bool:
                try:
                    r = _requests.head(url, timeout=5, allow_redirects=True)
                    return r.status_code == 200
                except Exception:
                    return False

            for idx, img_input in enumerate(image_inputs, 1):
                if isinstance(img_input, dict) and "image_url" in img_input:
                    u = img_input.get("image_url", {}).get("url", "")
                    if u and not _head_ok(u):
                        _time.sleep(1)
                        _head_ok(u)

            segments, segment_classifications = detect_invoice_page_segments(
                image_inputs, pdf_page_count, filename
            )
            base_prompt = get_extraction_prompt()
            parsed_list = []
            for seg_idx, (start_p, end_p) in enumerate(segments):
                scoped = build_scoped_extraction_instructions(
                    base_prompt, pdf_page_count, start_p, end_p
                )
                structured_output, _ = extract_structured_with_gpt_pdf_images(
                    temp_pdf_path,
                    filename,
                    extraction_instructions=scoped,
                    cleanup_temp_images=False,
                    image_paths=image_paths,
                    image_inputs=image_inputs,
                    skip_initial_delay=(seg_idx > 0),
                    assume_images_ready=True,
                )
                if structured_output:
                    if len(segments) > 1:
                        stem, ext = os.path.splitext(filename)
                        parse_name = f"{stem}_inv{seg_idx + 1}{ext}"
                    else:
                        parse_name = filename
                    inv = parse_output_to_dict(structured_output, parse_name)
                    inv["_invoice_pages"] = f"{start_p}-{end_p}"
                    # Stage-1 classification (Prompt A), aligned to this segment.
                    _seg_cls = (
                        segment_classifications[seg_idx]
                        if seg_idx < len(segment_classifications)
                        else None
                    )
                    if _seg_cls:
                        inv["_classification"] = _seg_cls
                        print(f"   🏷️  Classification (segment {seg_idx + 1}): {_seg_cls}")
                    parsed_list.append(inv)

            _cleanup_jpegs_only()

            if not parsed_list:
                print(f"   ⚠️  No structured output received from GPT (multi-page pipeline)")
                return "", None, pdf_page_count
            if len(parsed_list) == 1:
                return "", parsed_list[0], pdf_page_count
            return "", parsed_list, pdf_page_count

        finally:
            try:
                if os.path.exists(temp_pdf_path):
                    os.unlink(temp_pdf_path)
            except Exception:
                pass

    except Exception as e:
        print(f"   ❌ Error processing PDF {filename}: {e}")
        import traceback
        traceback.print_exc()
        return None, None, None
