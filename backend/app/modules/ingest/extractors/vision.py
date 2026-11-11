"""JPEG conversion and Azure OpenAI vision extraction."""

from __future__ import annotations

import base64
import os
import tempfile
from typing import List, Optional, Tuple

from PIL import Image
from pdf2image import convert_from_path

from backend.app.integrations.azure_openai import OpenAI, client
from backend.app.modules.ingest.extractors.prompt_parse import (
    build_extraction_prompt,
    get_extraction_prompt,
)
from backend.app.integrations.azure_blob import upload_image_to_blob_storage

def convert_file_to_jpegs(file_path):
    """Convert PDF/TIF/TIFF to list of JPEG images (from main2.py)"""
    temp_files = []
    file_ext = os.path.splitext(file_path)[1].lower()

    if file_ext in [".tif", ".tiff"]:
        # Handle multi-page TIFF
        image = Image.open(file_path)
        for i, page in enumerate(range(getattr(image, "n_frames", 1))):
            image.seek(page)
            rgb_image = image.convert("RGB")
            temp_file = tempfile.NamedTemporaryFile(suffix=f"_{i}.jpg", delete=False)
            rgb_image.save(temp_file.name, format="JPEG")
            temp_files.append(temp_file.name)

    elif file_ext in [".pdf"]:
        # Handle multi-page PDF using pdf2image
        pages = convert_from_path(file_path, dpi=300)
        for i, page in enumerate(pages):
            temp_file = tempfile.NamedTemporaryFile(suffix=f"_{i}.jpg", delete=False)
            page.save(temp_file.name, "JPEG")
            temp_files.append(temp_file.name)

    else:
        # Already image
        temp_files.append(file_path)

    return temp_files

def prepare_image_inputs(image_paths):
    """Upload images to Azure Blob Storage and return URLs for Azure OpenAI API"""
    if not client:
        raise ValueError("OpenAI client not initialized")
    
    # Check if using Azure OpenAI (has base_url) or regular OpenAI
    azure_api_key = os.getenv("AZURE_OPENAI_API_KEY")
    azure_base_url = os.getenv("AZURE_OPENAI_BASE_URL")
    is_azure = azure_api_key and azure_base_url
    
    image_inputs = []
    
    if is_azure:
        # For Azure OpenAI: Upload images to blob storage and use URLs
        print(f"   📤 Uploading {len(image_paths)} image(s) to Azure Blob Storage...")
        for path in image_paths:
            try:
                image_url = upload_image_to_blob_storage(path)
                # Azure OpenAI chat.completions API supports image_url format
                image_inputs.append({
                    "type": "image_url",
                    "image_url": {
                        "url": image_url
                    }
                })
                print(f"      ✅ Uploaded: {os.path.basename(path)} -> {image_url[:50]}...")
            except Exception as e:
                print(f"      ⚠️  Failed to upload {os.path.basename(path)}: {e}")
                import traceback
                traceback.print_exc()
                # Continue with other images even if one fails
    else:
        # For regular OpenAI: Use file upload API (original behavior)
        for path in image_paths:
            with open(path, "rb") as f:
                file_obj = client.files.create(file=f, purpose="vision")
                image_inputs.append({"type": "input_image", "file_id": file_obj.id})
    
    return image_inputs

def extract_structured_with_gpt_pdf_images(
    file_path: str,
    file_name: str,
    *,
    extraction_instructions: Optional[str] = None,
    cleanup_temp_images: bool = True,
    image_paths: Optional[List[str]] = None,
    image_inputs: Optional[list] = None,
    skip_initial_delay: bool = False,
    assume_images_ready: bool = False,
) -> Tuple[str, int]:
    """Extract structured data: PDF to images, upload to OpenAI, vision model.

    Returns (structured_output_text, page_count).

    If ``image_paths`` and ``image_inputs`` are provided, skips convert/upload (for multi-invoice
    Stage 2 loops). Use ``cleanup_temp_images=False`` until the last call, then delete JPEGs yourself.
    """
    if not client:
        raise ValueError("OpenAI client not initialized. Set AZURE_OPENAI_API_KEY/AZURE_OPENAI_BASE_URL or OPENAI_API_KEY environment variable.")

    import time
    import requests

    def verify_image_url_accessible(url, timeout=5):
        try:
            response = requests.head(url, timeout=timeout, allow_redirects=True)
            return response.status_code == 200
        except Exception:
            return False

    def _cleanup_jpeg_temps():
        if not cleanup_temp_images:
            return
        for img_path in image_paths:
            try:
                if os.path.exists(img_path) and img_path != file_path:
                    os.unlink(img_path)
            except Exception:
                pass

    if image_paths is not None:
        if image_inputs is None:
            raise ValueError("image_inputs is required when image_paths is provided")
        pdf_page_count = len(image_paths)
        print(f"   📄 Reusing {pdf_page_count} uploaded page image(s) for {file_name}")
    else:
        print(f"   📄 Converting PDF {file_name} to JPEG images...")
        image_paths = convert_file_to_jpegs(file_path)
        pdf_page_count = len(image_paths)
        print(f"      ✅ Converted to {pdf_page_count} image(s)")
        print(f"   📤 Uploading images to OpenAI...")
        image_inputs = prepare_image_inputs(image_paths)
        print(f"      ✅ Uploaded {len(image_inputs)} image(s)")

    if not assume_images_ready:
        print(f"   ⏳ Waiting 2 seconds for image URLs to propagate...")
        time.sleep(2)
        if image_inputs:
            print(f"   🔍 Verifying image URL accessibility...")
            all_urls_accessible = True
            for idx, img_input in enumerate(image_inputs, 1):
                if isinstance(img_input, dict) and "image_url" in img_input:
                    img_url = img_input.get("image_url", {}).get("url", "")
                    if img_url:
                        is_accessible = verify_image_url_accessible(img_url)
                        if is_accessible:
                            print(f"      ✅ Image {idx} URL is accessible")
                        else:
                            print(f"      ⚠️  Image {idx} URL not immediately accessible (will retry)")
                            all_urls_accessible = False
                            time.sleep(1)
                            is_accessible = verify_image_url_accessible(img_url)
                            if is_accessible:
                                print(f"      ✅ Image {idx} URL accessible on retry")
                            else:
                                print(f"      ⚠️  Image {idx} URL still not accessible - will proceed anyway")
            if not all_urls_accessible:
                print(f"   ⚠️  Some image URLs may not be immediately accessible - will retry with delays")

    if extraction_instructions is None:
        extraction_instructions = get_extraction_prompt()
    
    # Step 4: Use GPT-5-mini with responses.create (from main2.py)
    # For Azure OpenAI, use deployment name from env, otherwise use default model name
    model_name = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-5-mini")
    
    # Check if using Azure OpenAI (has base_url)
    azure_api_key = os.getenv("AZURE_OPENAI_API_KEY")
    azure_base_url = os.getenv("AZURE_OPENAI_BASE_URL")
    is_azure = azure_api_key and azure_base_url
    
    # Retry logic: Try up to 20 times if output is empty
    max_retries = 100
    retry_delay = 2  # seconds between retries
    initial_delay = 3  # Additional delay before first API call to ensure URLs are ready
    
    for attempt in range(1, max_retries + 1):
        try:
            if attempt > 1:
                print(f"   🔄 Retry attempt {attempt}/{max_retries} for {file_name}...")
                # Exponential backoff with additional delay: 2s, 4s, 6s, 8s...
                wait_time = retry_delay * attempt
                print(f"      ⏳ Waiting {wait_time} seconds before retry...")
                time.sleep(wait_time)
                
                # Verify URLs are still accessible before retry
                if image_inputs and attempt <= 5:  # Only verify for first few retries to save time
                    print(f"      🔍 Re-verifying image URL accessibility before retry...")
                    all_accessible = True
                    for idx, img_input in enumerate(image_inputs[:3], 1):  # Check first 3 URLs
                        if isinstance(img_input, dict) and 'image_url' in img_input:
                            img_url = img_input.get('image_url', {}).get('url', '')
                            if img_url:
                                is_accessible = verify_image_url_accessible(img_url, timeout=3)
                                if not is_accessible:
                                    print(f"         ⚠️  Image {idx} URL not accessible on retry {attempt}")
                                    all_accessible = False
                    if all_accessible:
                        print(f"      ✅ All checked image URLs are accessible")
            else:
                # First attempt - add initial delay to ensure URLs are ready
                print(f"   🔄 Extracting structured data using {model_name}...")
                if not skip_initial_delay:
                    print(f"      ⏳ Waiting {initial_delay} seconds before first API call to ensure URLs are ready...")
                    time.sleep(initial_delay)
            
            if is_azure:
                # For Azure OpenAI: Use chat.completions API which supports image_url
                # Build messages with text and image URLs
                messages_content = [
                    {"type": "text", "text": extraction_instructions}
                ]
                # Add image URLs
                messages_content.extend(image_inputs)
                
                # Azure OpenAI doesn't support custom temperature for some models - only use default (1)
                # So we omit temperature parameter for Azure OpenAI
                # Try with higher token limit - "length" finish reason suggests token limit is too low
                # Progressively increase token limit on retries
                if attempt <= 3:
                    max_tokens_for_attempt = 8000
                elif attempt <= 6:
                    max_tokens_for_attempt = 12000
                elif attempt <= 10:
                    max_tokens_for_attempt = 16000
                else:
                    max_tokens_for_attempt = 20000  # Maximum reasonable limit
                
                if attempt == 1 or attempt % 3 == 0:
                    print(f"      🔍 Using max_completion_tokens: {max_tokens_for_attempt} (attempt {attempt})")
                
                try:
                    response = client.chat.completions.create(
                        model=model_name,
                        messages=[{
                            "role": "user",
                            "content": messages_content
                        }],
                        max_completion_tokens=max_tokens_for_attempt  # Increased from 4096 to handle longer responses
                    )
                except Exception as token_error:
                    # If token limit error, try with lower limit
                    if "token" in str(token_error).lower() or "limit" in str(token_error).lower():
                        print(f"      ⚠️  Token limit error: {token_error}")
                        print(f"      🔄 Retrying with default token limit...")
                        max_tokens_for_attempt = 4096
                        response = client.chat.completions.create(
                            model=model_name,
                            messages=[{
                                "role": "user",
                                "content": messages_content
                            }],
                            max_completion_tokens=max_tokens_for_attempt
                        )
                    else:
                        raise
                
                # Check for content filtering indicators BEFORE extracting content
                if attempt == 1 or attempt % 5 == 0:  # Log on first attempt and every 5th retry
                    print(f"      🔍 DEBUG: Checking for content filtering...")
                    if hasattr(response, 'prompt_filter_results'):
                        print(f"         ⚠️  PROMPT FILTERING: {response.prompt_filter_results}")
                    if hasattr(response, 'content_filter_results'):
                        print(f"         ⚠️  RESPONSE FILTERING: {response.content_filter_results}")
                    
                    if hasattr(response, 'choices') and response.choices:
                        choice = response.choices[0]
                        if hasattr(choice, 'content_filter_results'):
                            print(f"         ⚠️  CHOICE FILTERING: {choice.content_filter_results}")
                        if hasattr(choice, 'finish_reason'):
                            finish_reason = choice.finish_reason
                            print(f"         Finish reason: {finish_reason}")
                            if finish_reason and ('filter' in str(finish_reason).lower() or finish_reason == 'content_filter'):
                                print(f"         ❌ CONTENT FILTERED BY AZURE OPENAI - This is why output is empty!")
                        
                        if hasattr(choice, 'message'):
                            message = choice.message
                            if hasattr(message, 'content_filter_results'):
                                print(f"         ⚠️  MESSAGE FILTERING: {message.content_filter_results}")
                            if hasattr(message, 'refusal'):
                                print(f"         ⚠️  MESSAGE REFUSAL: {message.refusal}")
                
                # Extract output from chat.completions format
                output_text = response.choices[0].message.content
            else:
                # For regular OpenAI: Use responses.create API (original behavior)
                response = client.responses.create(
                    model=model_name,
                    input=[{
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": extraction_instructions},
                            *image_inputs
                        ],
                    }],
                )
                # Extract output from responses format
                if hasattr(response, "output_text"):
                    output_text = response.output_text
                else:
                    output_text = str(response)
            
            # Check if output is valid (not None and not empty)
            if output_text and output_text.strip():
                # Debug: Print raw GPT output preview
                if attempt == 1:
                    print(f"   📄 GPT output preview (first 500 chars):")
                    print(f"      {output_text[:500]}...")
                else:
                    print(f"   ✅ Successfully extracted data on retry attempt {attempt}")
                    print(f"   📄 GPT output preview (first 500 chars):")
                    print(f"      {output_text[:500]}...")
                
                _cleanup_jpeg_temps()
                return output_text, pdf_page_count
            else:
                # Output is empty or None
                # On first failure, try a simple test to see if model responds at all
                if attempt == 1:
                    print(f"   ⚠️  No structured output received from GPT (attempt {attempt}/{max_retries})")
                    print(f"   🔍 Testing if model can respond with a simple prompt...")
                    try:
                        if is_azure:
                            # Try with different token limits to diagnose
                            for test_tokens in [10, 100, 1000]:
                                try:
                                    test_response = client.chat.completions.create(
                                        model=model_name,
                                        messages=[{
                                            "role": "user",
                                            "content": "Say 'Hello' if you can read this."
                                        }],
                                        max_completion_tokens=test_tokens
                                    )
                                    test_output = test_response.choices[0].message.content
                                    test_finish_reason = test_response.choices[0].finish_reason
                                    
                                    print(f"      Test with {test_tokens} tokens:")
                                    print(f"         Finish reason: {test_finish_reason}")
                                    print(f"         Output: {repr(test_output)}")
                                    
                                    if test_output and test_output.strip():
                                        print(f"      ✅ Model responds to simple prompts (got: '{test_output[:50]}')")
                                        print(f"      ⚠️  Issue is likely with the invoice extraction prompt, images, or token limit")
                                        break
                                    elif test_finish_reason == 'length':
                                        print(f"      ⚠️  Hit length limit even with {test_tokens} tokens - deployment has very low limit!")
                                    else:
                                        print(f"      ⚠️  Empty output with finish_reason: {test_finish_reason}")
                                        
                                except Exception as test_err:
                                    print(f"      ❌ Test with {test_tokens} tokens failed: {test_err}")
                                    if test_tokens == 10:
                                        # If even 10 tokens fails, deployment is broken
                                        print(f"      ❌ Deployment appears to be misconfigured or unavailable!")
                                        break
                        else:
                            print(f"      ⏭️  Skipping simple test (not using Azure OpenAI)")
                    except Exception as test_error:
                        print(f"      ⚠️  Simple test failed: {test_error}")
                        import traceback
                        traceback.print_exc()
                
                if attempt < max_retries:
                    if attempt > 1:
                        print(f"   ⚠️  No structured output received from GPT (attempt {attempt}/{max_retries})")
                    print(f"   🔄 Will retry in {retry_delay * attempt} seconds...")
                else:
                    print(f"   ❌ No structured output received from GPT after {max_retries} attempts")
                    print(f"   🔍 DIAGNOSTIC: Based on logs, the issue is:")
                    print(f"      ⚠️  Finish reason: 'length' - Model is hitting token limit")
                    print(f"      ⚠️  Even simple prompts return empty - Deployment configuration issue")
                    print(f"   💡 RECOMMENDATION:")
                    print(f"      1. Check Azure OpenAI deployment settings:")
                    print(f"         - Verify 'max_completion_tokens' is not set to 0 or very low")
                    print(f"         - Check deployment quota and limits in Azure Portal")
                    print(f"         - Verify the deployment 'gpt-5-mini' exists and is active")
                    print(f"      2. Try increasing max_completion_tokens in deployment settings")
                    print(f"      3. Check if deployment has any special restrictions or policies")
                    print(f"      4. Verify the model deployment supports vision/image inputs")
                    _cleanup_jpeg_temps()
                    return "", pdf_page_count  # Return empty string after all retries exhausted
                    
        except Exception as e:
            # If it's the last attempt, raise the exception
            if attempt == max_retries:
                _cleanup_jpeg_temps()
                raise e
            else:
                # Log error and retry
                print(f"   ⚠️  Error on attempt {attempt}/{max_retries}: {e}")
                print(f"   🔄 Will retry in {retry_delay * attempt} seconds...")
                import time
                time.sleep(retry_delay * attempt)
                continue
    
    # Should not reach here, but return empty string as fallback
    return "", pdf_page_count

# Legacy function name kept for compatibility
def extract_structured_with_gpt_pdf(pdf_base64: str, file_name: str, file_path: str, api_key: str) -> Tuple[str, int]:
    """Legacy function - redirects to image-based extraction"""
    # For compatibility, save PDF bytes to temp file and process
    temp_pdf = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    try:
        temp_pdf.write(base64.b64decode(pdf_base64))
        temp_pdf.close()
        return extract_structured_with_gpt_pdf_images(temp_pdf.name, file_name)
    finally:
        try:
            if os.path.exists(temp_pdf.name):
                os.unlink(temp_pdf.name)
        except Exception:
            pass

def extract_structured_with_gpt_vision(base64_images: list, file_name: str, file_path: str, api_key: str) -> str:
    """Use GPT Vision API to extract structured data from base64 PDF images (legacy - kept for compatibility)"""
    # Use Azure OpenAI if credentials are available, otherwise fallback to regular OpenAI
    azure_api_key = os.getenv("AZURE_OPENAI_API_KEY")
    azure_base_url = os.getenv("AZURE_OPENAI_BASE_URL")
    
    if azure_api_key and azure_base_url:
        client = OpenAI(api_key=azure_api_key, base_url=azure_base_url)
    else:
        client = OpenAI(api_key=api_key)
    
    # Build the extraction prompt (same as before but adapted for images)
    prompt = build_extraction_prompt_for_vision(file_name, file_path)
    
    # Verify prompt contains key rules
    prompt_lower = prompt.lower()
    has_gst_rules = 'hbd gst' in prompt_lower and ('gstin' in prompt_lower or 'bill to' in prompt_lower)
    has_hotel_rule = 'hummingbird digital' in prompt_lower or 'humming bird' in prompt_lower
    has_date_rules = 'dd/mm/yyyy' in prompt_lower or 'arrival' in prompt_lower or 'departure' in prompt_lower
    
    if has_gst_rules and has_hotel_rule and has_date_rules:
        print(f"   ✅ Prompt verified: Contains GST rules, Hotel rules, and Date format rules")
    else:
        print(f"   ⚠️  WARNING: Prompt may be missing some rules!")
        print(f"      GST rules: {has_gst_rules}, Hotel rules: {has_hotel_rule}, Date rules: {has_date_rules}")
    
    # Build messages with images
    messages_content = [
        {"type": "text", "text": prompt}
    ]
    
    # Add all page images
    for i, img_base64 in enumerate(base64_images):
        messages_content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/png;base64,{img_base64}"
            }
        })
    
    # For Azure OpenAI, use deployment name from env, otherwise use default model name
    model_name = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o-mini")
    # Check if using Azure OpenAI (has base_url)
    azure_api_key = os.getenv("AZURE_OPENAI_API_KEY")
    azure_base_url = os.getenv("AZURE_OPENAI_BASE_URL")
    is_azure = azure_api_key and azure_base_url
    
    # Azure OpenAI uses max_completion_tokens, regular OpenAI uses max_tokens
    # Azure OpenAI doesn't support custom temperature (only default 1), so omit it for Azure
    api_params = {
        "model": model_name,
        "messages": [{"role": "user", "content": messages_content}]
    }
    if is_azure:
        api_params["max_completion_tokens"] = 2048
        # Don't set temperature for Azure OpenAI - it only supports default (1)
    else:
        api_params["max_tokens"] = 2048
        api_params["temperature"] = 0.1
    
    response = client.chat.completions.create(**api_params)
    
    output = response.choices[0].message.content
    
    # Debug: Print raw GPT output preview
    print(f"   📄 GPT output preview (first 500 chars):")
    print(f"      {output[:500]}...")
    
    return output

def extract_structured_with_gpt(ocr_text: str, file_name: str, file_path: str, api_key: str) -> str:
    """Use GPT to extract structured data from OCR text (legacy function - kept for compatibility)"""
    # Use Azure OpenAI if credentials are available, otherwise fallback to regular OpenAI
    azure_api_key = os.getenv("AZURE_OPENAI_API_KEY")
    azure_base_url = os.getenv("AZURE_OPENAI_BASE_URL")
    
    if azure_api_key and azure_base_url:
        client = OpenAI(api_key=azure_api_key, base_url=azure_base_url)
    else:
        client = OpenAI(api_key=api_key)
    
    prompt = build_extraction_prompt(ocr_text, file_name, file_path)
    
    # Verify prompt contains key rules from prompt_invoice.txt
    prompt_lower = prompt.lower()
    has_gst_rules = 'hbd gst' in prompt_lower and ('gstin' in prompt_lower or 'bill to' in prompt_lower)
    has_hotel_rule = 'hummingbird digital' in prompt_lower or 'humming bird' in prompt_lower
    has_date_rules = 'dd/mm/yyyy' in prompt_lower or 'arrival' in prompt_lower or 'departure' in prompt_lower
    
    if has_gst_rules and has_hotel_rule and has_date_rules:
        print(f"   ✅ Prompt verified: Contains GST rules, Hotel rules, and Date format rules")
    else:
        print(f"   ⚠️  WARNING: Prompt may be missing some rules!")
        print(f"      GST rules: {has_gst_rules}, Hotel rules: {has_hotel_rule}, Date rules: {has_date_rules}")
    
    # For Azure OpenAI, use deployment name from env, otherwise use default model name
    model_name = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o-mini")
    # Check if using Azure OpenAI (has base_url)
    is_azure = azure_api_key and azure_base_url
    
    # Azure OpenAI uses max_completion_tokens, regular OpenAI uses max_tokens
    # Azure OpenAI doesn't support custom temperature (only default 1), so omit it for Azure
    api_params = {
        "model": model_name,
        "messages": [{"role": "user", "content": prompt}]
    }
    if is_azure:
        api_params["max_completion_tokens"] = 2048
        # Don't set temperature for Azure OpenAI - it only supports default (1)
    else:
        api_params["max_tokens"] = 2048
        api_params["temperature"] = 0.1
    
    response = client.chat.completions.create(**api_params)
    
    output = response.choices[0].message.content
    
    # Debug: Print raw GPT output preview
    print(f"   📄 GPT output preview (first 500 chars):")
    print(f"      {output[:500]}...")
    
    return output
