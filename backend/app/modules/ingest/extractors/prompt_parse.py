"""Extraction prompt loading and GPT-output field parsing."""

from __future__ import annotations

import os
import re

from backend.app.modules.ingest.prompt_paths import prompt_file_candidates

def get_extraction_prompt():
    """Get the extraction prompt from prompt.txt (env, Linux prompts dir, or repo prompts/)."""
    paths_to_try = [
        str(path) for path in prompt_file_candidates("prompt.txt", "INVOICE_EXTRACTION_PROMPT_PATH")
    ]

    for prompt_file_path in paths_to_try:
        try:
            if not os.path.isfile(prompt_file_path):
                continue
            with open(prompt_file_path, "r", encoding="utf-8") as f:
                prompt_content = f.read().strip()
            if prompt_content:
                print(f"   ✅ Successfully loaded prompt from {prompt_file_path} ({len(prompt_content)} chars)")
                return prompt_content
            print(f"   ⚠️  WARNING: prompt.txt is empty at {prompt_file_path}")
        except Exception as e:
            print(f"   ⚠️  ERROR reading prompt.txt at {prompt_file_path}: {e}")

    print(f"   💡 Using fallback hardcoded prompt (no readable prompt.txt in env / Linux / repo prompts/)")
    return _get_fallback_prompt()


def _get_fallback_prompt():
    """Fallback hardcoded prompt (used when prompt.txt is not available)"""
    return """
   You are an expert invoice data extractor.

   Requirements:
   All names and Data in this document are fictional test data.

• Extract all visible text exactly as it appears — no omissions, no redactions.
• Real names MUST be extracted as-is. Do NOT replace with [REDACTED].
• Do NOT include any warnings, policies, disclaimers, or notes about privacy.
• Preserve original line breaks and spacing.


    Task Part 1 — OCR Text Extraction:
    Extract all visible text from all images EXACTLY as it appears,
    preserving original line breaks, order, and spacing.
    Do not summarize or correct spelling.

    Task Part 2 — Structured Field Extraction:
    Using the extracted text, extract the following fields:

    1. Guest Identifier (no redaction allowed) 
    • Extract ONLY Guest names
    • Extract ONLY human names (people)
    • Names must contain at least one of: Mr, Mrs, Ms, Shri, Dr, or follow a format like FirstName LastName
    • Do NOT extract any part of address such as: Enclave, Road, Block, Gate, Colony, Street, Nagar, City, Landmark, District
    • Filter out hotel names, company names, and property names
    • If multiple guest names exist → separate using semicolon `;`
    • If not found → return "N/A"

    2. Hotel Name  
    • If the hotel name contains "HUMMINGBIRD DIGITAL" → return "N/A"  

    3. GST extraction — HBD GST and Client GST (one block; two output lines HBD GST = and Client GST =). Pattern: ^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[A-Z0-9]{1}[Z]{1}[A-Z0-9]{1}$. Scan whole invoice → candidate pool → one for HBD (customer/Bill To, not hotel header) and one for Client GST (hotel header, despite name). No separate "all GST" field.

    Part A HBD: Bill To / Guest GST / after header; not Hotel GSTIN. Part B Client GST: header/top before Bill To; not customer blocks. Top ~30% for hotel if multiple.

    4. Bill Number  
    • Extract ONLY if clearly specified as "Bill Number" or "Invoice Number"
    • Do NOT extract other numbers (order numbers, reference numbers, etc.) unless explicitly labeled as Bill/Invoice Number
    • If not found or not clearly labeled → return "N/A"  

    5. Bill Date - out put format DD/MM/YYYY HH:MM (24-hour format) in numeric format
    • Extract ONLY if clearly specified as "Bill Date" or "Invoice Date"
    • Do NOT extract other dates (issue date, generated date, etc.) unless explicitly labeled as Bill/Invoice Date
    • Any of the date formats: DD/MM/YYYY | DD-MM-YYYY | DD-MMM-YYYY | DD/MM/YYYY
    • Detect time if present: HH:MM (AM/PM allowed)
    • If time is present → convert output → DD/MM/YYYY HH:MM (24-hour format) 
    • If no time present → extract date only 
    • If not found or not clearly labeled → return "N/A" 

    6. Check In Date - out put format DD/MM/YYYY HH:MM (24-hour format) in numeric format
    • Often labeled "Arrival"
    • Any of the date formats: DD/MM/YYYY | DD-MM-YYYY | DD-MMM-YYYY | DD/MM/YYYY
    • Detect time if present: HH:MM (AM/PM allowed)
    • If time is present → convert output → DD/MM/YYYY HH:MM (24-hour format)
    • If no time present → extract date only 
    • If not found → return "N/A" 

    7. Check Out Date  
    • Often labeled "Departure"
    • Any of the date formats: DD/MM/YYYY | DD-MM-YYYY | DD-MMM-YYYY | DD/MM/YYYY
    • Detect time if present: HH:MM (AM/PM allowed)
    • If time is present → convert output → DD/MM/YYYY HH:MM (24-hour format)
    • If no time present → extract date only 
    • If not found → return "N/A"  

    8. Total Amount  
    • Extract final payable amount  
    • If not found → return "N/A" 

    9. CGST Number
    • Extract CGST (Central GST) number if present
    • Look for labels like "CGST", "Central GST", "CGST Number"
    • Extract the GST number associated with CGST
    • If not found → return "N/A"

    10. SGST Number
    • Extract SGST (State GST) number if present
    • Look for labels like "SGST", "State GST", "SGST Number"
    • Extract the GST number associated with SGST
    • If not found → return "N/A"

    11. Total CGST Amount
    • FIRST: Try to find explicit total CGST amount
    • Look for labels like "Total CGST", "CGST Amount", "CGST Total", "CGST"
    • Extract the numeric amount value
    • If explicit total is found → return that value
    • If explicit total is NOT found → look for individual CGST line items in the charges/items table
      - Find all line items that mention "CGST" (e.g., "CGST Charges", "CGST 2.5%", "- CGST")
      - Extract the numeric amount from each CGST line item
      - Sum all individual CGST amounts to get the total
      - Return the sum as Total CGST
    • If no CGST amounts found at all → return "N/A"

    12. Total SGST Amount
    • FIRST: Try to find explicit total SGST amount
    • Look for labels like "Total SGST", "SGST Amount", "SGST Total", "SGST"
    • Extract the numeric amount value
    • If explicit total is found → return that value
    • If explicit total is NOT found → look for individual SGST line items in the charges/items table
      - Find all line items that mention "SGST" (e.g., "SGST Charges", "SGST 2.5%", "- SGST")
      - Extract the numeric amount from each SGST line item
      - Sum all individual SGST amounts to get the total
      - Return the sum as Total SGST
    • If no SGST amounts found at all → return "N/A"

    13. Room Charge
    • Extract ONLY if explicitly labeled as "Room Charge", "Room Rent", "Room Tariff", "Accommodation Charges", "Room Rate"
    • Extract the numeric amount value
    • Do NOT guess or calculate this value from other fields
    • If not clearly labeled or not found → return "N/A"

    14. Total Tariff
    • Extract ONLY if explicitly labeled as "Total Tariff", "Tariff Total", "Total Room Tariff", "Tariff Amount"
    • Extract the numeric amount value
    • Do NOT confuse with "Total Amount" — they are different fields
    • Do NOT guess or calculate this value from other fields
    • If not clearly labeled or not found → return "N/A"

    15. Other Charges
    • Extract ONLY if explicitly labeled as "Other Charges", "Miscellaneous Charges", "Extra Charges", "Additional Charges", "Sundry Charges"
    • Extract the numeric amount value
    • Do NOT guess or calculate this value from other fields
    • If not clearly labeled or not found → return "N/A"

    16. Total IGST Amount
    • Extract ONLY if explicitly labeled as "IGST", "Total IGST", "IGST Amount", "Integrated GST"
    • IGST applies to inter-state transactions (used instead of CGST+SGST)
    • Extract the numeric amount value
    • Do NOT guess or calculate this value from other fields
    • If not clearly labeled or not found → return "N/A"

    17. Total Taxes
    • Extract ONLY if explicitly labeled as "Total Tax", "Total Taxes", "Tax Total", "Total GST", "Tax Amount"
    • This is the combined sum of all taxes shown on the invoice
    • Extract the numeric amount value
    • Do NOT calculate by adding CGST+SGST+IGST yourself — only extract if the invoice shows an explicit total tax figure
    • If not clearly labeled or not found → return "N/A"

    18. Discount
    • Extract ONLY if explicitly labeled as "Discount", "Less: Discount", "Discount Amount", "Less Discount"
    • Extract the numeric amount value
    • Do NOT guess or calculate this value from other fields
    • If not clearly labeled or not found → return "N/A"

    19. Advance Received
    • Extract ONLY if explicitly labeled as "Advance", "Advance Received", "Advance Paid", "Advance Amount", "Advance Payment", "Less: Advance"
    • Extract the numeric amount value
    • Do NOT guess or calculate this value from other fields
    • If not clearly labeled or not found → return "N/A"

    ⚠ Rules:
    • Return information AS-IS from source text (no assumptions).
    • If field exists multiple places — choose the most detailed.
    • If any field is definitely missing → return "N/A".
    • For fields 13-19 (Room Charge through Advance Received): Extract ONLY if the label is explicitly visible on the invoice. Do NOT derive, calculate, or infer these values. If unsure whether a label matches, return "N/A".

    📌 Final Response Format:
    [Extracted Text]
    <all raw OCR text here>

    Guest Identifier = <value>
    Hotel Name = <value>
    HBD GST = <value>
    Client GST = <value>
    Bill Number = <value>
    Bill Date = <value>
    Check IN = <value>
    Check OUT = <value>
    Amount = <value>
    CGST Number = <value>
    SGST Number = <value>
    Total CGST = <value>
    Total SGST = <value>
    Room Charge = <value>
    Total Tariff = <value>
    Other Charges = <value>
    Total IGST = <value>
    Total Taxes = <value>
    Discount = <value>
    Advance Received = <value>

    ⚠️ IMPORTANT FOR CGST/SGST TOTALS:
    • If you find explicit "Total CGST" or "Total SGST" labels → use those values
    • If explicit totals are NOT found → look in the charges/items table for individual CGST/SGST line items
    • For split-up extraction:
      - Find all rows/line items containing "CGST" (e.g., "CGST Charges 2.5%", "- CGST", "CGST 112.50")
      - Find all rows/line items containing "SGST" (e.g., "SGST Charges 2.5%", "- SGST", "SGST 112.50")
      - Extract the numeric amount from each line item
      - Sum all CGST amounts → this becomes Total CGST
      - Sum all SGST amounts → this becomes Total SGST
      - Example: If you find "CGST 112.50" three times, Total CGST = 337.50
    • Only extract split-up if explicit totals are missing (N/A)

    ⚠️ IMPORTANT FOR FIELDS 13-19 (Room Charge, Total Tariff, Other Charges, Total IGST, Total Taxes, Discount, Advance Received):
    • These fields must ONLY be extracted when explicitly labeled on the invoice
    • Do NOT calculate or derive values — extract only what is clearly printed
    • Do NOT confuse "Total Amount" with "Total Tariff" — they are different fields
    • Do NOT confuse "Total Taxes" with individual tax items (CGST/SGST/IGST)
    • If a label is ambiguous or you are unsure, return "N/A"

    """

def build_extraction_prompt_for_pdf(file_name: str, file_path: str) -> str:
    """Legacy function - kept for compatibility but uses hardcoded prompt from main2.py"""
    return get_extraction_prompt()

def build_extraction_prompt_for_vision(file_name: str, file_path: str) -> str:
    """Build the extraction prompt for Vision API (no OCR text, images are provided directly) - legacy function"""
    prompt_file_path = "prompt_invoice.txt"
    
    # Try to read the prompt file
    try:
        if not os.path.exists(prompt_file_path):
            print(f"   ⚠️  WARNING: prompt_invoice.txt not found at {os.path.abspath(prompt_file_path)}")
            print(f"   💡 Using fallback prompt (may not have all rules)")
        else:
            print(f"   ✅ Reading prompt from: {os.path.abspath(prompt_file_path)}")
        
        with open(prompt_file_path, "r", encoding="utf-8") as f:
            template = f.read().strip()
        
        if not template:
            raise ValueError("prompt_invoice.txt is empty")
        
        # Remove OCR text placeholder and adapt for vision
        # Replace {ocr_text} with instruction to read from images
        vision_instruction = "Please read and extract all visible text from the invoice images provided above. Analyze the entire document including headers, footers, tables, and all text visible in the images."
        formatted_prompt = template.replace("{ocr_text}", vision_instruction).format(file_name=file_name, file_path=file_path)
        
        # Verify placeholders were replaced
        if '{file_name}' in formatted_prompt or '{file_path}' in formatted_prompt:
            print(f"   ⚠️  WARNING: Some placeholders not replaced in prompt template")
        
        print(f"   ✅ Successfully loaded prompt from prompt_invoice.txt ({len(template)} chars)")
        return formatted_prompt
        
    except FileNotFoundError:
        print(f"   ❌ ERROR: prompt_invoice.txt not found!")
        print(f"   💡 Expected at: {os.path.abspath(prompt_file_path)}")
        print(f"   ⚠️  Using fallback prompt")
    except Exception as e:
        print(f"   ⚠️  Error reading prompt_invoice.txt: {e}")
        import traceback
        traceback.print_exc()
        print(f"   ⚠️  Using fallback prompt")
    
    # Fallback prompt adapted for vision (no OCR text reference)
    return f"""You are an expert in reading invoice bills. Analyze the invoice images provided above and extract all visible information.

Then, perform the following structured extraction:

1. EXTRACT "Guest Name" extract multiple names if found separated by semicolon ;
2. Extract Hotel Name - if it is HUMMINGBIRD DIGITAL neglect it
3. EXTRACT ALL GST NUMBERS found anywhere in the document
   
   ⚠️ CRITICAL ANTI-HALLUCINATION RULES FOR GST EXTRACTION:
   
   - Search the ENTIRE document for GST numbers (headers, footers, body, tables, anywhere)
   - GST numbers are ALWAYS exactly 15 characters (2 digits + 13 alphanumeric characters)
   - Format: [2 digits][13 alphanumeric characters] - Example: 27AABCH5874R1ZR
   
   🚫 ABSOLUTELY FORBIDDEN - DO NOT DO THIS:
   - DO NOT add extra characters (e.g., if you see "27AABCH5874R1ZR", DO NOT write "27AAABCH5874R1ZR")
   - DO NOT remove characters
   - DO NOT modify any characters
   - DO NOT "correct" or "fix" what you think looks wrong
   - DO NOT infer or guess missing characters
   - DO NOT hallucinate characters that are not clearly visible
   - DO NOT assume patterns or add characters to make it "look better"
   - DO NOT complete incomplete numbers - extract EXACTLY as written
   
   ✅ CORRECT EXTRACTION METHOD:
   - Read the GST number character-by-character from the images
   - Copy each character EXACTLY as it appears - no modifications
   - If you see "27AABCH5874R1ZR" in the image, extract "27AABCH5874R1ZR" (NOT "27AAABCH5874R1ZR")
   - If you see "27AABCH5874R1Z" (14 chars), extract "27AABCH5874R1Z" (do NOT add the missing character)
   - Extract EVERY GST number you find, regardless of location (header, footer, body, table, etc.)
   - If multiple GST numbers are found, list them all separated by semicolon (;)
   
   📋 EXAMPLES OF CORRECT vs INCORRECT:
   - Document shows: "27AABCH5874R1ZR" → Extract: "27AABCH5874R1ZR" ✅
   - Document shows: "27AABCH5874R1ZR" → Extract: "27AAABCH5874R1ZR" ❌ WRONG (added extra A)
   - Document shows: "27AABCH5874R1ZR" → Extract: "27AABCH5874R1Z" ❌ WRONG (removed last character)
   - Document shows: "27AABCH5874R1Z" → Extract: "27AABCH5874R1Z" ✅ (extract as-is, even if incomplete)

4. After extracting ALL GST numbers, categorize them:
   - HBD GST: All GST numbers that contain "5874" in the number (these belong to Hummingbird Digital)
   - Client GST: All GST numbers that do NOT contain "5874" in the number
   - If multiple numbers in a category, separate them with semicolon (;)
   - Example: If you found "27AABCH5874R1ZR" and "29ABCDE1234F1GH", then:
     * HBD GST = 27AABCH5874R1ZR (contains 5874)
     * Client GST = 29ABCDE1234F1GH (does not contain 5874)
   
5. Bill Number 
6. Bill Date formate (dd/mm/yy) If none are found, return an empty string.
7. Check In date formate (dd/mm/yy) - may be in name of Arrival. DO NOT extract time, only the date in dd/mm/yy format (2-digit year).
8. Check Out date formate (dd/mm/yy) - may be in the name of Departure. DO NOT extract time, only the date in dd/mm/yy format (2-digit year).
9. Total Amount
10. CGST Number - Extract CGST (Central GST) number if present. Look for labels like "CGST", "Central GST", "CGST Number". If not found, return empty.
11. SGST Number - Extract SGST (State GST) number if present. Look for labels like "SGST", "State GST", "SGST Number". If not found, return empty.
12. Total CGST - FIRST: Try to find explicit total CGST amount. Look for labels like "Total CGST", "CGST Amount", "CGST Total", "CGST". If explicit total is found, return that value. If explicit total is NOT found, look for individual CGST line items in the charges/items table (e.g., "CGST Charges 2.5%", "- CGST", "CGST 112.50"). Extract the numeric amount from each CGST line item and sum all individual CGST amounts to get the total. Return the sum as Total CGST. If no CGST amounts found at all, return empty.
13. Total SGST - FIRST: Try to find explicit total SGST amount. Look for labels like "Total SGST", "SGST Amount", "SGST Total", "SGST". If explicit total is found, return that value. If explicit total is NOT found, look for individual SGST line items in the charges/items table (e.g., "SGST Charges 2.5%", "- SGST", "SGST 112.50"). Extract the numeric amount from each SGST line item and sum all individual SGST amounts to get the total. Return the sum as Total SGST. If no SGST amounts found at all, return empty.


OUTPUT FORMAT (use EXACTLY this format, one field per line):

Guest Name = <value>
Hotel Name = <value>
HBD GST = <value>
Client GST = <value>
Bill Number = <value>
Bill Date = <value>
Check IN = <value>
Check OUT = <value>
Amount = <value>
CGST Number = <value>
SGST Number = <value>
Total CGST = <value>
Total SGST = <value>

IMPORTANT: 
- Use the EXACT format above (field name followed by "=" followed by value)
- Do NOT use numbered lists (1., 2., etc.)
- Do NOT use markdown bold (**text**)
- If a field is empty or not found, leave it empty (e.g., "HBD GST = " or "Client GST = ")
- Each field should be on a separate line
- For Check IN and Check OUT dates: Use ONLY dd/mm/yy format (2-digit year), NO time
- For "HBD GST" and "Client GST": 
  * ⚠️ CRITICAL: Extract GST numbers EXACTLY as they appear - character-by-character, NO modifications
  * 🚫 NEVER add extra characters (e.g., "27AABCH5874R1ZR" should NOT become "27AAABCH5874R1ZR")
  * 🚫 NEVER remove characters
  * 🚫 NEVER "correct" or "fix" the number - extract it EXACTLY as written
  * ✅ Read each character from the images and copy it exactly
  * ✅ If the number is 15 characters, extract all 15 exactly as shown
  * ✅ If the number is incomplete (less than 15 chars), extract it as-is (do NOT complete it)
  * ✅ REMEMBER: Your job is to COPY what you see, NOT to improve or correct it
  * ✅ FIRST: Find ALL GST numbers in the document (search everywhere: headers, footers, body, tables)
  * ✅ THEN: Categorize them - numbers with "5874" go to HBD GST, others go to Client GST
- For "Total CGST" and "Total SGST":
  * If explicit totals are found, use those values
  * If explicit totals are NOT found, find all individual CGST/SGST line items in the charges table and sum them
  * Example: If you find "CGST 112.50" three times, Total CGST = 337.50
  * Only extract split-up if explicit totals are missing"""

def build_extraction_prompt(ocr_text: str, file_name: str, file_path: str) -> str:
    """Build the extraction prompt from prompt_invoice.txt (from invoice2.py)"""
    prompt_file_path = "prompt_invoice.txt"
    
    # Try to read the prompt file
    try:
        if not os.path.exists(prompt_file_path):
            print(f"   ⚠️  WARNING: prompt_invoice.txt not found at {os.path.abspath(prompt_file_path)}")
            print(f"   💡 Using fallback prompt (may not have all rules)")
        else:
            print(f"   ✅ Reading prompt from: {os.path.abspath(prompt_file_path)}")
        
        with open(prompt_file_path, "r", encoding="utf-8") as f:
            template = f.read().strip()
        
        if not template:
            raise ValueError("prompt_invoice.txt is empty")
        
        # Format the template with the OCR text
        formatted_prompt = template.format(ocr_text=ocr_text, file_name=file_name, file_path=file_path)
        
        # Verify all placeholders were replaced
        if '{ocr_text}' in formatted_prompt or '{file_name}' in formatted_prompt or '{file_path}' in formatted_prompt:
            print(f"   ⚠️  WARNING: Some placeholders not replaced in prompt template")
        
        print(f"   ✅ Successfully loaded prompt from prompt_invoice.txt ({len(template)} chars)")
        return formatted_prompt
        
    except FileNotFoundError:
        print(f"   ❌ ERROR: prompt_invoice.txt not found!")
        print(f"   💡 Expected at: {os.path.abspath(prompt_file_path)}")
        print(f"   ⚠️  Using fallback prompt")
    except Exception as e:
        print(f"   ⚠️  Error reading prompt_invoice.txt: {e}")
        import traceback
        traceback.print_exc()
        print(f"   ⚠️  Using fallback prompt")
    
    # Fallback prompt (same as prompt_invoice.txt but hardcoded)
        return f"""You are an expert in reading invoice bill.

Extract all visible text from the document:
{ocr_text}

Then, perform the following structured extraction:

1. EXTRACT "Guest Name" extract multiple names if found separated by semicolon ;
2. Extract Hotel Name - if it is HUMMINGBIRD DIGITAL neglect it
3. EXTRACT ALL GST NUMBERS found anywhere in the document
   
   ⚠️ CRITICAL ANTI-HALLUCINATION RULES FOR GST EXTRACTION:
   
   - Search the ENTIRE document for GST numbers (headers, footers, body, tables, anywhere)
   - GST numbers are ALWAYS exactly 15 characters (2 digits + 13 alphanumeric characters)
   - Format: [2 digits][13 alphanumeric characters] - Example: 27AABCH5874R1ZR
   
   🚫 ABSOLUTELY FORBIDDEN - DO NOT DO THIS:
   - DO NOT add extra characters (e.g., if you see "27AABCH5874R1ZR", DO NOT write "27AAABCH5874R1ZR")
   - DO NOT remove characters
   - DO NOT modify any characters
   - DO NOT "correct" or "fix" what you think looks wrong
   - DO NOT infer or guess missing characters
   - DO NOT hallucinate characters that are not clearly visible
   - DO NOT assume patterns or add characters to make it "look better"
   - DO NOT complete incomplete numbers - extract EXACTLY as written
   
   ✅ CORRECT EXTRACTION METHOD:
   - Read the GST number character-by-character from the OCR text
   - Copy each character EXACTLY as it appears - no modifications
   - If you see "27AABCH5874R1ZR" in the text, extract "27AABCH5874R1ZR" (NOT "27AAABCH5874R1ZR")
   - If you see "27AABCH5874R1Z" (14 chars), extract "27AABCH5874R1Z" (do NOT add the missing character)
   - Extract EVERY GST number you find, regardless of location (header, footer, body, table, etc.)
   - If multiple GST numbers are found, list them all separated by semicolon (;)
   
   📋 EXAMPLES OF CORRECT vs INCORRECT:
   - Document shows: "27AABCH5874R1ZR" → Extract: "27AABCH5874R1ZR" ✅
   - Document shows: "27AABCH5874R1ZR" → Extract: "27AAABCH5874R1ZR" ❌ WRONG (added extra A)
   - Document shows: "27AABCH5874R1ZR" → Extract: "27AABCH5874R1Z" ❌ WRONG (removed last character)
   - Document shows: "27AABCH5874R1Z" → Extract: "27AABCH5874R1Z" ✅ (extract as-is, even if incomplete)
   
4. After extracting ALL GST numbers, categorize them:
   - HBD GST: All GST numbers that contain "5874" in the number (these belong to Hummingbird Digital)
   - Client GST: All GST numbers that do NOT contain "5874" in the number
   - If multiple numbers in a category, separate them with semicolon (;)
   - Example: If you found "27AABCH5874R1ZR" and "29ABCDE1234F1GH", then:
     * HBD GST = 27AABCH5874R1ZR (contains 5874)
     * Client GST = 29ABCDE1234F1GH (does not contain 5874)
   
5. Bill Number 
6. Bill Date formate (dd/mm/yy) If none are found, return an empty string.
7. Check In date formate (dd/mm/yy) - may be in name of Arrival. DO NOT extract time, only the date in dd/mm/yy format (2-digit year).
8. Check Out date formate (dd/mm/yy) - may be in the name of Departure. DO NOT extract time, only the date in dd/mm/yy format (2-digit year).
9. Total Amount
10. CGST Number - Extract CGST (Central GST) number if present. Look for labels like "CGST", "Central GST", "CGST Number". If not found, return empty.
11. SGST Number - Extract SGST (State GST) number if present. Look for labels like "SGST", "State GST", "SGST Number". If not found, return empty.
12. Total CGST - FIRST: Try to find explicit total CGST amount. Look for labels like "Total CGST", "CGST Amount", "CGST Total", "CGST". If explicit total is found, return that value. If explicit total is NOT found, look for individual CGST line items in the charges/items table (e.g., "CGST Charges 2.5%", "- CGST", "CGST 112.50"). Extract the numeric amount from each CGST line item and sum all individual CGST amounts to get the total. Return the sum as Total CGST. If no CGST amounts found at all, return empty.
13. Total SGST - FIRST: Try to find explicit total SGST amount. Look for labels like "Total SGST", "SGST Amount", "SGST Total", "SGST". If explicit total is found, return that value. If explicit total is NOT found, look for individual SGST line items in the charges/items table (e.g., "SGST Charges 2.5%", "- SGST", "SGST 112.50"). Extract the numeric amount from each SGST line item and sum all individual SGST amounts to get the total. Return the sum as Total SGST. If no SGST amounts found at all, return empty.


OUTPUT FORMAT (use EXACTLY this format, one field per line):

Guest Name = <value>
Hotel Name = <value>
HBD GST = <value>
Client GST = <value>
Bill Number = <value>
Bill Date = <value>
Check IN = <value>
Check OUT = <value>
Amount = <value>
CGST Number = <value>
SGST Number = <value>
Total CGST = <value>
Total SGST = <value>

IMPORTANT: 
- Use the EXACT format above (field name followed by "=" followed by value)
- Do NOT use numbered lists (1., 2., etc.)
- Do NOT use markdown bold (**text**)
- If a field is empty or not found, leave it empty (e.g., "HBD GST = " or "Client GST = ")
- Each field should be on a separate line
- For Check IN and Check OUT dates: Use ONLY dd/mm/yy format (2-digit year), NO time
- For "HBD GST" and "Client GST": 
  * ⚠️ CRITICAL: Extract GST numbers EXACTLY as they appear - character-by-character, NO modifications
  * 🚫 NEVER add extra characters (e.g., "27AABCH5874R1ZR" should NOT become "27AAABCH5874R1ZR")
  * 🚫 NEVER remove characters
  * 🚫 NEVER "correct" or "fix" the number - extract it EXACTLY as written
  * ✅ Read each character from the OCR text and copy it exactly
  * ✅ If the number is 15 characters, extract all 15 exactly as shown
  * ✅ If the number is incomplete (less than 15 chars), extract it as-is (do NOT complete it)
  * ✅ REMEMBER: Your job is to COPY what you see, NOT to improve or correct it
  * ✅ FIRST: Find ALL GST numbers in the document (search everywhere: headers, footers, body, tables)
  * ✅ THEN: Categorize them - numbers with "5874" go to HBD GST, others go to Client GST"""


def parse_output_to_dict(output_text, filename):
    """Parse GPT output to dictionary (from invoice2.py)"""
    data = {}
    
    def extract(field_name):
        """Extract field value handling multiple GPT output formats:
        - Format 1: "1. **Guest Name** = value" (numbered list with bold)
        - Format 2: "**Guest Name** = value" (bold without numbering)
        - Format 3: "Guest Name = value" (simple format)
        """
        # Escape the field name but allow flexible whitespace
        escaped_field = re.escape(field_name)
        escaped_field = escaped_field.replace(r'\ ', r'\s+')
        
        # Pattern 1: Numbered list with bold: "1. **Guest Name** = value"
        pattern1 = r"(?:\d+\.\s*)?\*\*\s*" + escaped_field + r"\s*\*\*\s*=\s*([^\n]*)"
        match = re.search(pattern1, output_text, re.IGNORECASE | re.MULTILINE)
        if match:
            value = match.group(1).strip()
            if value and value.lower() not in {"", "value", "n/a", "na", "none"}:
                return value
        
        # Pattern 2: Bold only: "**Guest Name** = value"
        pattern2 = r"\*\*\s*" + escaped_field + r"\s*\*\*\s*=\s*([^\n]*)"
        match = re.search(pattern2, output_text, re.IGNORECASE | re.MULTILINE)
        if match:
            value = match.group(1).strip()
            if value and value.lower() not in {"", "value", "n/a", "na", "none"}:
                return value
        
        # Pattern 3: Simple format: "Guest Name = value"
        # Match the field name followed by = and capture everything until end of line or before next field
        # Use a more precise pattern that stops at newline
        pattern3 = escaped_field + r"\s*=\s*([^\n]*)"
        match = re.search(pattern3, output_text, re.IGNORECASE | re.MULTILINE)
        if match:
            value = match.group(1).strip()
            # Check if value is actually another field name (common GPT parsing error)
            # This happens when GPT puts fields on same line: "Bill Date = Check IN = 22/09/2025"
            if value:
                # Check if value contains "=" which suggests it's the next field
                if '=' in value:
                    # Split by = and take only first part if it looks like a value
                    parts = value.split('=', 1)
                    if len(parts) == 2:
                        # Check if first part looks like a field name
                        first_part = parts[0].strip()
                        field_name_pattern = r'^(?:Guest|Hotel|HBD|Client|Bill|Check|Amount)\s+(?:Name|Date|Number|GST|IN|OUT)'
                        if re.match(field_name_pattern, first_part, re.IGNORECASE):
                            return ""  # This is another field, return empty
                        # Otherwise, try to extract the actual value after the second =
                        # But in most cases, if there's a = in the value, it's corrupted
                        return ""
                # Check if value looks like another field name (e.g., "Check IN")
                field_name_patterns = [
                    r'^(?:Guest|Hotel|HBD|Client|Bill|Check)\s+(?:Name|Date|Number|GST|IN|OUT)\s*=',
                    r'^Check\s+(?:IN|OUT)\s*=',
                    r'^Amount\s*=',
                ]
                for pattern in field_name_patterns:
                    if re.match(pattern, value, re.IGNORECASE):
                        return ""  # This is another field name, return empty
            # Only return non-empty values that aren't placeholders
            if value and value.lower() not in {"", "value", "n/a", "na", "none"}:
                return value
        
        return ""
    
    # Extract all fields using flexible pattern matching
    # Handle both "Guest Identifier" (main2.py) and "Guest Name" (main.py legacy)
    guest_identifier = extract("Guest Identifier")
    guest_name = extract("Guest Name")
    data["guest_name"] = guest_identifier if guest_identifier else guest_name
    
    data["hotel_name"] = extract("Hotel Name")
    data["hbd_gst"] = extract("HBD GST") or extract("Hummingbird GST")
    data["client_gst"] = extract("Client GST")
    data["bill_number"] = extract("Bill Number")
    # Strip "Page : 1 / 1" artifact that sometimes gets appended to the invoice number
    if data.get("bill_number"):
        cleaned = re.sub(r'\s*/\s*Page\s*:\s*1\s*/\s*1\s*$', '', data["bill_number"], flags=re.IGNORECASE).strip()
        if cleaned != data["bill_number"]:
            print(f"      ✂️  Stripped page suffix from Bill Number: '{data['bill_number']}' -> '{cleaned}'")
        data["bill_number"] = cleaned
    data["bill_date"] = extract("Bill Date")
    data["check_in"] = extract("Check IN")
    data["check_out"] = extract("Check OUT")
    data["amount"] = extract("Amount")
    data["cgst_number"] = extract("CGST Number")
    data["sgst_number"] = extract("SGST Number")
    data["total_cgst"] = extract("Total CGST")
    data["total_sgst"] = extract("Total SGST")
    data["room_charge"] = extract("Room Charge")
    data["total_tariff"] = extract("Total Tariff")
    data["other_charges"] = extract("Other Charges")
    data["total_igst"] = extract("Total IGST")
    data["total_taxes"] = extract("Total Taxes")
    data["discount"] = extract("Discount")
    data["advance_received"] = extract("Advance Received")
    
    # Validate extracted CGST/SGST totals
    # GPT should have already calculated split-up sums if explicit totals weren't found
    # (per the prompt instructions)
    if data.get("total_cgst") and data.get("total_cgst", "").strip().upper() not in {"", "N/A", "NA", "NONE", "NOT FOUND"}:
        # Clean the value - remove any non-numeric characters except decimal point and commas
        cgst_value = re.sub(r'[^\d,.]', '', str(data["total_cgst"]))
        if cgst_value:
            data["total_cgst"] = cgst_value.replace(',', '')  # Remove commas
            print(f"      ✅ Total CGST extracted: {data['total_cgst']}")
    
    if data.get("total_sgst") and data.get("total_sgst", "").strip().upper() not in {"", "N/A", "NA", "NONE", "NOT FOUND"}:
        # Clean the value - remove any non-numeric characters except decimal point and commas
        sgst_value = re.sub(r'[^\d,.]', '', str(data["total_sgst"]))
        if sgst_value:
            data["total_sgst"] = sgst_value.replace(',', '')  # Remove commas
            print(f"      ✅ Total SGST extracted: {data['total_sgst']}")
    
    for _nf in ["room_charge", "total_tariff", "other_charges", "total_igst", "total_taxes", "discount", "advance_received"]:
        _nv = data.get(_nf)
        if _nv and str(_nv).strip().upper() not in {"", "N/A", "NA", "NONE", "NOT FOUND"}:
            _cleaned = re.sub(r'[^\d,.]', '', str(_nv))
            if _cleaned:
                data[_nf] = _cleaned.replace(',', '')
                print(f"      ✅ {_nf} extracted: {data[_nf]}")
    
    data["filename"] = filename
    
    if not data.get("bill_date") or data["bill_date"].strip().upper() in {"", "N/A", "NA", "NONE", "NOT FOUND"}:
        checkout = data.get("check_out", "")
        if checkout and checkout.strip().upper() not in {"", "N/A", "NA", "NONE", "NOT FOUND"}:
            data["bill_date"] = checkout.strip()
            print(f"      ℹ️  Bill Date not found — using Check OUT date as fallback: {data['bill_date']}")
    
    # Debug: Print extracted values
    print(f"   🔍 Parsed data from GPT output for {filename}:")
    print(f"      Guest Name: '{data.get('guest_name', 'NOT FOUND')}'")
    print(f"      Hotel Name: '{data.get('hotel_name', 'NOT FOUND')}'")
    print(f"      HBD GST: '{data.get('hbd_gst', 'NOT FOUND')}'")
    print(f"      Client GST: '{data.get('client_gst', 'NOT FOUND')}'")
    print(f"      Bill Number: '{data.get('bill_number', 'NOT FOUND')}'")
    print(f"      CGST Number: '{data.get('cgst_number', 'NOT FOUND')}'")
    print(f"      SGST Number: '{data.get('sgst_number', 'NOT FOUND')}'")
    print(f"      Total CGST: '{data.get('total_cgst', 'NOT FOUND')}'")
    print(f"      Total SGST: '{data.get('total_sgst', 'NOT FOUND')}'")
    print(f"      Room Charge: '{data.get('room_charge', 'NOT FOUND')}'")
    print(f"      Total Tariff: '{data.get('total_tariff', 'NOT FOUND')}'")
    print(f"      Other Charges: '{data.get('other_charges', 'NOT FOUND')}'")
    print(f"      Total IGST: '{data.get('total_igst', 'NOT FOUND')}'")
    print(f"      Total Taxes: '{data.get('total_taxes', 'NOT FOUND')}'")
    print(f"      Discount: '{data.get('discount', 'NOT FOUND')}'")
    print(f"      Advance Received: '{data.get('advance_received', 'NOT FOUND')}'")
    
    return data
