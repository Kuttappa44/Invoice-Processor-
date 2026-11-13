"""Email tables, booking-code extraction, and email+PDF merge."""

from __future__ import annotations

import base64
import email
import hashlib
import json
import os
import re
from email.header import decode_header as _decode_header
from email.header import make_header as _make_header
from typing import List, Optional

import pandas as pd
from rapidfuzz import fuzz

from backend.app.exceptions import ProcessCancelled
from backend.app.integrations.azure_openai import OPENAI_AVAILABLE, OpenAI, client
from backend.app.modules.ingest.pdf_pipeline import process_pdf_bytes
from backend.app.shared.dates import format_date_to_dd_mm_yy, to_date
from backend.app.shared.gstin import is_valid_gstin
from backend.app.shared.text import normalize_field_text

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

# Invoice keywords
INVOICE_KEYWORDS = [
    'invoice', 'bill', 'receipt', 'payment', 'booking', 'reservation',
    'confirmation', 'voucher', 'ticket', 'statement', 'charge',
    'hotel', 'accommodation', 'travel', 'booking id', 'reservation id',
    'pending', 'required', 'urgent', 'bills'
]

# Heuristic keyword sets for evaluating email extraction quality
HOTEL_KEYWORDS = {
    'hotel', 'resort', 'inn', 'lodge', 'villa', 'suites', 'palace',
    'residency', 'regency', 'stay', 'retreat', 'homestay', 'hostel',
    'motel', 'club', 'apartments'
}
CORPORATE_KEYWORDS = {
    'pvt', 'private', 'ltd', 'llp', 'digital', 'services', 'solutions',
    'india', 'pvt.', 'limited', 'corporate', 'infrastructure', 'technologies'
}

def extract_amounts(text):
    """Extract comprehensive monetary amounts from text"""
    amount_patterns = [
        # Currency symbols with amounts
        r'[\$₹€£]\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)',
        r'(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)\s*[\$₹€£]',
        
        # Indian Rupee patterns
        r'₹\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)',
        r'Rs\.?\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)',
        r'INR\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)',
        
        # Generic number patterns that could be amounts
        r'(?<!\d)(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)(?!\d)',
    ]
    
    amounts = []
    for pattern in amount_patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        for match in matches:
            try:
                # Clean and convert to float
                clean_amount = match.replace(',', '')
                amount = float(clean_amount)
                # Filter out very small amounts that are likely not real prices
                if amount >= 1.0:
                    amounts.append(amount)
            except ValueError:
                continue
    
    return list(set(amounts))  # Remove duplicates

def extract_urgency_level(text):
    """Extract urgency level from text"""
    urgency_keywords = {
        'URGENT': ['urgent', 'asap', 'immediately', 'rush', 'priority'],
        'HIGH': ['high priority', 'important', 'critical'],
        'NORMAL': ['normal', 'standard', 'regular']
    }
    
    text_lower = text.lower()
    for level, keywords in urgency_keywords.items():
        if any(keyword in text_lower for keyword in keywords):
            return level
    
    return 'NORMAL'

def has_hotel_indicator(value: str) -> bool:
    lower = value.lower()
    return any(keyword in lower for keyword in HOTEL_KEYWORDS)

def looks_corporate(value: str) -> bool:
    lower = value.lower()
    return any(keyword in lower for keyword in CORPORATE_KEYWORDS)

def choose_preferred_value(field: str, pdf_value: str, email_value: str) -> str:
    pdf_clean = normalize_field_text(pdf_value)
    email_clean = normalize_field_text(email_value)

    if not pdf_clean and email_clean:
        return email_clean
    if not email_clean:
        return pdf_clean

    if field == 'hotel_name':
        pdf_has_hotel = has_hotel_indicator(pdf_clean)
        email_has_hotel = has_hotel_indicator(email_clean)
        pdf_corp = looks_corporate(pdf_clean)
        email_corp = looks_corporate(email_clean)
        if email_has_hotel and (not pdf_has_hotel or pdf_corp) and not email_corp:
            return email_clean
        if not pdf_clean or pdf_corp:
            return email_clean
        return pdf_clean

    if field == 'guest_name':
        pdf_corp = looks_corporate(pdf_clean)
        email_corp = looks_corporate(email_clean)
        if pdf_corp and not email_corp:
            return email_clean
        if not pdf_clean:
            return email_clean
        return pdf_clean

    if field in ('check_in_date', 'check_out_date'):
        if pdf_clean.lower() in ['not found', '']:
            return email_clean
        if email_clean.lower() in ['not found', '']:
            return pdf_clean
        if len(email_clean) >= len(pdf_clean) and any(char.isdigit() for char in email_clean):
            return email_clean
        return pdf_clean

    return pdf_clean or email_clean

BOOKING_LABELS = {
    'booking_code': [
        'booking code', 'booking number', 'booking no'
    ],
    'guest_name': [
        'guest name', 'client name', 'customer name', 'name', 'client'
    ],
    'property_name': [
        'property name', 'hotel name', 'venue name'
    ],
    'check_in_date': [
        'check-in date', 'check in date', 'arrival date', 'from date', 'start date'
    ],
    'check_out_date': [
        'check-out date', 'check out date', 'departure date', 'to date', 'end date'
    ],
    'stay_duration': [
        'stay duration', 'stay period', 'stay'
    ],
    'total_amount': [
        'total amount', 'amount', 'vendor amount', 'advance paid amount', 'paid amount'
    ],
    'advance_paid_amount': [
        'advance paid amount', 'advance amount paid', 'advance amount', 'advance paid'
    ],
}

HEADER_TOKEN_SET = {
    'booking code', 'guest name', 'property name', 'hotel name', 'check-in date',
    'check in date', 'check-out date', 'check out date', 'paid on', 'advance paid amount',
    'advance paid', 'vendor amount', 'stay days', 'hb remarks', 'remarks'
}

def strip_known_label(value: str, labels: List[str]) -> str:
    if not value:
        return value
    value_clean = value.strip()
    for label in labels:
        pattern = re.compile(rf'^{re.escape(label)}[:\-\s]*', re.IGNORECASE)
        value_clean = pattern.sub('', value_clean).strip()
    return value_clean

def looks_like_header_text(value: str) -> bool:
    if not value:
        return False
    lower = value.lower().strip()
    if lower in HEADER_TOKEN_SET:
        return True
    if any(token in lower for token in ['booking code guest name', 'guest name check-in date']):
        return True
    if len(lower.split()) <= 6 and all(token in HEADER_TOKEN_SET for token in lower.split()):
        return True
    return False

def extract_booking_code_candidate(value: str) -> str:
    if not value:
        return ''
    # Look for numeric booking codes (4-20 digits)
    digits = re.findall(r'\d{4,20}', value)
    if digits:
        return digits[0]
    compact = re.sub(r'[^A-Za-z0-9]', '', value or '')
    if not compact:
        return ''
    # Look for alphanumeric codes (4-20 chars) with at least 4 digits
    matches = re.findall(r'[A-Za-z0-9]{4,20}', compact)
    for match in matches:
        digit_count = sum(ch.isdigit() for ch in match)
        if digit_count >= 4:  # Require at least 4 digits
            return match
    return ''

def extract_date_candidate(value: str) -> str:
    if not value:
        return ''
    match = re.search(r'\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b', value)
    if match:
        return match.group(1)
    return ''


INVALID_PROPERTY_SUBSTRINGS = [
    'note', 'update', 'please', 'kindly', 'gst', 'soft copy', 'hard copy',
    'priority', 'attached', 'mail to', 'gst detail', 'accounts department'
]

INVALID_GUEST_SUBSTRINGS = [
    'note', 'update', 'please', 'kindly', 'gst', 'hotel', 'property', 'attached',
    'mail to', 'gst detail', 'accounts department'
]

def is_valid_property_name(value: str) -> bool:
    if not value:
        return False
    lower = value.lower()
    if 'http' in lower or '@' in lower:
        return False
    if len(value) > 80:
        return False
    if not re.search(r'[a-zA-Z]', value):
        return False
    if any(sub in lower for sub in INVALID_PROPERTY_SUBSTRINGS):
        return False
    if re.match(r'^\d+[/-]?\d*$', value.strip()):
        return False
    return True

def is_valid_guest_name(value: str) -> bool:
    if not value:
        return False
    lower = value.lower()
    if 'http' in lower or '@' in lower:
        return False
    if len(value) > 60:
        return False
    if re.match(r'^\d+(\.\d+)?$', value.strip()):
        return False
    if any(sub in lower for sub in INVALID_GUEST_SUBSTRINGS):
        return False
    if not re.search(r'[a-zA-Z]', value):
        return False
    if extract_date_candidate(value):
        return False
    return True

def sanitize_numeric(value: Optional[str], fallback: Optional[str] = None) -> str:
    candidates: List[Optional[str]] = [value, fallback]
    for candidate in candidates:
        if not candidate:
            continue
        candidate_str = normalize_field_text(candidate)
        if not candidate_str:
            continue
        match = re.search(r'\d[\d,\.]*', candidate_str)
        if not match:
            continue
        numeric = match.group(0).replace(',', '')
        if numeric in ('', '.'):
            continue
        if numeric.endswith('.'):
            numeric = numeric[:-1]
        return numeric
    return 'Not Found'

def entries_equal(entry_a: Optional[dict], entry_b: Optional[dict]) -> bool:
    if entry_a is entry_b:
        return True
    if not entry_a or not entry_b:
        return False
    keys = ['booking_code', 'guest_name', 'check_in_date', 'check_out_date']
    for key in keys:
        if normalize_field_text(entry_a.get(key)) != normalize_field_text(entry_b.get(key)):
            return False
    return True

def sanitize_table_row(entry: dict, fallback_booking_code: Optional[str] = None, fallback_property: Optional[str] = None) -> Optional[dict]:
    if not entry or not isinstance(entry, dict):
        return None
    cleaned: dict = {}

    booking_code_raw = entry.get('booking_code') or ''
    booking_code_from_entry = extract_booking_code_candidate(booking_code_raw)
    if not booking_code_from_entry and fallback_booking_code:
        booking_code_from_entry = extract_booking_code_candidate(fallback_booking_code)
    if not booking_code_from_entry:
        return None
    cleaned['booking_code'] = booking_code_from_entry

    for key, labels in BOOKING_LABELS.items():
        if key == 'booking_code':
            continue
        value = entry.get(key)
        if not value:
            continue
        value_norm = normalize_field_text(value)
        value_norm = strip_known_label(value_norm, labels)
        if looks_like_header_text(value_norm):
            continue
        if key in ('check_in_date', 'check_out_date'):
            date_candidate = extract_date_candidate(value_norm)
            if date_candidate:
                cleaned[key] = date_candidate
        else:
            cleaned[key] = value_norm

    if 'property_name' in cleaned and not is_valid_property_name(cleaned['property_name']):
        cleaned.pop('property_name', None)
    if 'property_name' not in cleaned and fallback_property and is_valid_property_name(fallback_property):
        cleaned['property_name'] = fallback_property
    if 'guest_name' in cleaned and not is_valid_guest_name(cleaned['guest_name']):
        cleaned.pop('guest_name', None)

    if 'total_amount' in cleaned:
        amount_match = re.search(r'\d[\d,\.]*', cleaned['total_amount'])
        if amount_match:
            cleaned['total_amount'] = amount_match.group(0).replace(',', '')
        else:
            cleaned.pop('total_amount', None)

    stay_value = entry.get('stay_duration') or entry.get('stay')
    if stay_value:
        stay_text = normalize_field_text(stay_value)
        stay_text = stay_text.replace('to', '-')
        stay_match = re.search(r'(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\s*[-–]\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})', stay_text)
        if stay_match:
            start_date, end_date = stay_match.group(1), stay_match.group(2)
            if 'check_in_date' not in cleaned:
                cleaned['check_in_date'] = start_date
            if 'check_out_date' not in cleaned:
                cleaned['check_out_date'] = end_date

    required_fields = ['booking_code', 'guest_name', 'check_in_date', 'check_out_date']
    if not all(cleaned.get(field) for field in required_fields):
        return None
    return cleaned

def load_vendor_reference():
    """Load the Excel reference sheet with vendor -> assignee mapping."""
    try:
        df = pd.read_excel('userlist.xlsx')
        vendor_reference = {}

        # New format: two-column mapping (e.g., "Row Labels" -> "User Name")
        column_map = {str(col).strip().lower(): col for col in df.columns}
        vendor_col = None
        user_col = None
        for key in ['row labels', 'row label', 'vendor', 'vendor name', 'hotel name', 'property name', 'property']:
            if key in column_map:
                vendor_col = column_map[key]
                break
        for key in ['user name', 'username', 'assignee', 'assigned to', 'owner']:
            if key in column_map:
                user_col = column_map[key]
                break

        def normalize_vendor_key(value: str) -> str:
            normalized = re.sub(r'[^a-z0-9]+', ' ', value.lower()).strip()
            return re.sub(r'\s+', ' ', normalized)

        if vendor_col and user_col:
            for _, row in df[[vendor_col, user_col]].iterrows():
                vendor = '' if pd.isna(row[vendor_col]) else str(row[vendor_col]).strip()
                assignee = '' if pd.isna(row[user_col]) else str(row[user_col]).strip()
                if vendor and assignee and vendor.lower() != 'nan':
                    vendor_reference[vendor.lower()] = assignee
                    vendor_reference[vendor] = assignee
                    normalized_key = normalize_vendor_key(vendor)
                    if normalized_key:
                        vendor_reference[normalized_key] = assignee
            print(f"✅ Loaded {len(vendor_reference)} vendor references (row mapping)")
            return vendor_reference

        print("❌ Vendor reference format not recognized. Expected columns like 'Row Labels' and 'User Name'.")
        return {}
    except Exception as e:
        print(f"❌ Error loading vendor reference: {e}")
        return {}

def is_invoice_email(subject, body):
    """Check if email is an invoice based on subject and body"""
    text = f"{subject} {body}".lower()
    return any(keyword in text for keyword in INVOICE_KEYWORDS)

def decode_email_subject(subject):
    """Decode email subject if it's encoded"""
    try:
        decoded = email.header.decode_header(subject)
        decoded_subject = ""
        for part, encoding in decoded:
            if isinstance(part, bytes):
                if encoding:
                    decoded_subject += part.decode(encoding)
                else:
                    decoded_subject += part.decode('utf-8', errors='ignore')
            else:
                decoded_subject += part
        return decoded_subject
    except:
        return subject

_PDF_MAGIC = b'%PDF-'
_DATA_URI_PDF_RE = re.compile(r'data:application/pdf;base64,([A-Za-z0-9+/=\s]+)', re.I)


def _is_pdf_bytes(data):
    """True if the bytes look like a PDF (header may appear anywhere in the first 1024 bytes)."""
    return bool(data) and _PDF_MAGIC in data[:1024]


def _clean_pdf_filename(raw_name, idx):
    """Decode RFC2047 names, sanitise, and make sure the name ends in .pdf."""
    name = None
    if raw_name:
        try:
            name = str(_make_header(_decode_header(raw_name)))
        except Exception:
            name = raw_name
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', '_', name or '').strip() or f'attachment_{idx}'
    stem = name[:-4] if name.lower().endswith('.pdf') else name
    return stem[:140] + '.pdf'


def _looks_like_email(data):
    head = data[:2000].lower()
    return head.startswith((b'from:', b'received:', b'return-path:', b'mime-version:',
                            b'subject:', b'date:')) or b'\nmime-version:' in head


def _collect_pdf_parts(msg, found, seen, depth=0):
    """Collect (raw_filename, pdf_bytes) for every PDF anywhere in the message (nested mail included)."""
    if depth > 8:
        return
    for part in msg.walk():
        if part.is_multipart():
            continue  # containers; walk() already descends into message/rfc822 children
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            continue
        if not payload:
            continue

        # 1) Real PDF bytes: any filename, content type or disposition
        if _is_pdf_bytes(payload):
            digest = hashlib.sha256(payload).hexdigest()
            if digest not in seen:
                seen.add(digest)
                found.append((part.get_filename(), payload))
            continue

        ctype = part.get_content_type()
        fname = (part.get_filename() or '').lower()

        # 2) PDF embedded in the HTML/text body as a data: URI
        if ctype in ('text/html', 'text/plain'):
            try:
                text = payload.decode(part.get_content_charset() or 'utf-8', errors='ignore')
            except Exception:
                text = payload.decode('utf-8', errors='ignore')
            if 'data:application/pdf' in text.lower():
                for m in _DATA_URI_PDF_RE.finditer(text):
                    try:
                        data = base64.b64decode(re.sub(r'\s+', '', m.group(1)))
                    except Exception:
                        continue
                    if _is_pdf_bytes(data):
                        digest = hashlib.sha256(data).hexdigest()
                        if digest not in seen:
                            seen.add(digest)
                            found.append((None, data))

        # 3) Forwarded/nested email that was NOT parsed as message/rfc822 (e.g. .eml attachment)
        elif ctype in ('message/rfc822', 'application/octet-stream') or fname.endswith('.eml'):
            if _looks_like_email(payload):
                try:
                    inner = email.message_from_bytes(payload)
                except Exception:
                    continue
                _collect_pdf_parts(inner, found, seen, depth + 1)


def extract_attachment_info(email_message):
    """Return every PDF found anywhere in the email (any name/type/location, nested mail included).

    PDFs are detected by their %PDF- header, not by filename, extension or content type.
    Links, zips, HTML and images are ignored.
    """
    found, seen = [], set()
    try:
        _collect_pdf_parts(email_message, found, seen)
    except Exception as e:
        print(f"   \u26a0\ufe0f  Error scanning email for PDFs: {e}")

    attachments = []
    for idx, (raw_name, payload) in enumerate(found, 1):
        attachments.append({
            'filename': _clean_pdf_filename(raw_name, idx),
            'content_type': 'application/pdf',  # downstream checks rely on this
            'size_bytes': len(payload),
            'payload': payload,  # Include payload for image-based PDF processing
        })
    if attachments:
        print(f"   \U0001f50e Detected {len(attachments)} PDF(s) by content")
    return attachments, []


def _part_text(part):
    """Decode a text part safely; never raises."""
    try:
        p = part.get_payload(decode=True)
        if not p:
            return ''
        try:
            return p.decode(part.get_content_charset() or 'utf-8', errors='ignore')
        except Exception:
            return p.decode('utf-8', errors='ignore')
    except Exception:
        return ''


def _log_pdf_failure(filename, subject, message_id, reason):
    """Single greppable log line for any PDF that could not be processed."""
    print(
        f"   \u274c PDF couldn't be processed: {filename} | subject: {subject} "
        f"| message-id: {message_id} | reason: {reason}"
    )


def extract_vendor_name_improved(subject, from_email, body, pdf_analysis=None, pdf_data=None, email_property_name=None):
    """Extract vendor name with improved logic
    
    Priority order:
    1. Property name from email (extracted by OpenAI) - highest priority
    2. Hotel name from PDF analysis
    3. Email text extraction (subject/body search)
    """
    # Priority 1: Use property name from email if provided (extracted by OpenAI)
    if email_property_name and isinstance(email_property_name, str):
        email_property_name_clean = email_property_name.strip()
        if email_property_name_clean and email_property_name_clean.lower() not in ['unknown', 'unknown vendor', 'not found', '']:
            print(f"   🏨 Using property name from email (OpenAI extracted): {email_property_name_clean}")
            return email_property_name_clean
    
    # Priority 2: Try to get hotel name from PDF analysis if available
    hotel_name = None
    if pdf_analysis and isinstance(pdf_analysis, dict):
        for pdf_key, pdf_data_item in pdf_analysis.items():
            # Ensure pdf_data_item is a dictionary
            if isinstance(pdf_data_item, dict) and pdf_data_item.get('hotel_name') and pdf_data_item['hotel_name'] != 'Not Found':
                hotel_name = pdf_data_item['hotel_name']
                break
    
    # If we have a hotel name from PDF, use it
    if hotel_name and hotel_name != 'Not Found':
        print(f"   🏨 Using hotel name from PDF: {hotel_name}")
        return hotel_name
    
    # Priority 3: Fallback to email-based extraction (search subject/body text)
    text_to_search = f"{subject} {from_email} {body}".lower()
    
    # Look for common hotel/property indicators
    hotel_indicators = [
        'hotel', 'resort', 'inn', 'chalet', 'executive', 'apartments',
        'suites', 'palace', 'tower', 'plaza', 'central', 'grand',
        'premier', 'luxury', 'boutique', 'international', 'airport',
        'city', 'garden', 'park', 'view', 'heights', 'manor', 'villa',
        'house', 'lodge', 'court', 'square', 'mall', 'center', 'centre',
        'complex', 'building', 'towers'
    ]
    
    for indicator in hotel_indicators:
        if indicator in text_to_search:
            # Extract the word before the indicator
            pattern = r'(\w+)\s+' + indicator
            match = re.search(pattern, text_to_search)
            if match:
                potential_name = match.group(1).title() + ' ' + indicator.title()
                return potential_name
    
    # If no hotel name found, return generic
    return 'Unknown Vendor'

def extract_email_body_full(email_message):
    """Extract plain text body and HTML body from email - returns both (similar to email_to_excel_mapper.py)"""
    body = ""
    html_body = ""
    
    if email_message.is_multipart():
        for part in email_message.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain":
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        plain_text = payload.decode('utf-8', errors='ignore')
                        body = body + "\n" + plain_text if body else plain_text
                except:
                    continue
            elif content_type == "text/html":
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        html_part = payload.decode('utf-8', errors='ignore')
                        html_body = html_body + "\n" + html_part if html_body else html_part
                except:
                    continue
    else:
        try:
            payload = email_message.get_payload(decode=True)
            if payload:
                content = payload.decode('utf-8', errors='ignore')
                if '<html' in content.lower() or '<table' in content.lower():
                    html_body = content
                    body = re.sub(r'<[^>]+>', '', content)
                else:
                    body = content
        except:
            body = str(email_message.get_payload())
    
    if html_body and not body:
        body = re.sub(r'<[^>]+>', '', html_body)
    
    return body, html_body


def normalize_table_header(header_text):
    """Normalize HTML table header text to standardized field keys"""
    if not header_text:
        return None
    normalized = re.sub(r"\s+", " ", str(header_text)).strip().lower()
    if len(normalized) > 60:
        return None
    normalized = normalized.replace("\xa0", " ")
    normalized = re.sub(r"[^a-z0-9\s_-]", "", normalized)
    normalized = normalized.replace("  ", " ")
    if not normalized:
        return None
    # Only map to booking_code when the header explicitly says:
    # "booking code", "booking number", or "booking no".
    if re.search(r"\bbooking[\s_\-]*(?:code|number|no)\b", normalized, flags=re.IGNORECASE):
        return "booking_code"
    if "guest" in normalized and "name" in normalized:
        return "guest_name"
    if "client" in normalized and "name" in normalized:
        return "guest_name"  # client_name is same as guest_name
    if "customer" in normalized and "name" in normalized:
        return "guest_name"
    if ("hotel" in normalized or "property" in normalized or "venue" in normalized) and "name" in normalized:
        return "property_name"
    if "check" in normalized and any(token in normalized for token in ["in", "arrival", "from", "start", "arr"]):
        return "check_in_date"
    if "check" in normalized and any(token in normalized for token in ["out", "departure", "to", "end", "dep"]):
        return "check_out_date"
    if "arrival" in normalized:
        return "check_in_date"
    if "departure" in normalized:
        return "check_out_date"
    if "stay" in normalized and "duration" in normalized:
        return "stay_duration"
    if any(token in normalized for token in ["amount", "total", "grand total", "balance"]):
        return "total_amount"
    if "advance" in normalized and "amount" in normalized:
        return "advance_paid_amount"
    return None


def parse_html_tables_for_booking_data(html_body):
    """Parse HTML tables to extract booking information from structured layouts"""
    if not BeautifulSoup or not html_body:
        if not BeautifulSoup:
            print("   🛈 BeautifulSoup unavailable: skipping HTML table parsing")
        return [], None
    try:
        soup = BeautifulSoup(html_body, 'html.parser')
    except Exception as e:
        print(f"   ⚠️  HTML parsing failed: {e}")
        return [], None
    extracted_entries = []
    property_context = None  # Initialize outside the loop (like main1.py structure)
    tables = soup.find_all('table')
    print(f"   🧾 Detected {len(tables)} HTML table(s) in email body")
    for table in tables:
        rows = []
        for row in table.find_all('tr'):
            cells = row.find_all(['th', 'td'])
            cell_texts = []
            for cell in cells:
                text = cell.get_text(separator=' ', strip=True)
                text = text.replace('\xa0', ' ').strip()
                if text:
                    cell_texts.append(text)
            if cell_texts:
                rows.append(cell_texts)
        if not rows:
            continue
        header_row = None
        header_map = None
        header_index = None
        
        # Enhanced property name detection
        # 1. Look for explicit "property name:" pattern
        for row_cells in rows:
            for cell_text in row_cells:
                match = re.search(r'property\s*name[:\-]\s*(.+)', cell_text, re.IGNORECASE)
                if match:
                    candidate = match.group(1)
                    for label_list in BOOKING_LABELS.values():
                        for label in label_list:
                            token = re.search(rf'\b{re.escape(label)}\b', candidate, re.IGNORECASE)
                            if token:
                                candidate = candidate[:token.start()]
                                break
                        else:
                            continue
                        break
                    candidate = normalize_field_text(candidate)
                    if is_valid_property_name(candidate):
                        property_context = candidate
                        break
            if property_context:
                break
        
        # 2. Look for property names in table headers (e.g., "Enrise by Sayaji Nagpur")
        # Check first row for potential property name (often appears as table title/header)
        if not property_context and rows:
            first_row_text = ' '.join(rows[0]).strip()
            # Check if first row looks like a property name (not a data header)
            # Property names often contain words like "by", "Hotel", "Resort", etc.
            property_indicators = ['hotel', 'resort', 'inn', 'palace', 'tower', 'plaza', 
                                  'by', 'apartments', 'suites', 'executive', 'grand']
            first_row_lower = first_row_text.lower()
            
            # If first row doesn't look like a data header (no booking-related keywords)
            header_keywords = ['booking', 'code', 'guest', 'name', 'check', 'date', 'amount', 'paid']
            is_likely_header = any(keyword in first_row_lower for keyword in header_keywords)
            
            if not is_likely_header:
                # Check if it contains property indicators
                if any(indicator in first_row_lower for indicator in property_indicators):
                    candidate = first_row_text.strip()
                    # Remove common prefixes/suffixes
                    candidate = re.sub(r'^(dear|hi|hello|greetings)[,\s]+', '', candidate, flags=re.IGNORECASE)
                    candidate = normalize_field_text(candidate)
                    if is_valid_property_name(candidate) and len(candidate) > 5:
                        property_context = candidate
                        print(f"        🏨 Detected property name in table header: {property_context}")
        
        # 3. Look for property names in cells that don't match standard headers
        if not property_context:
            for row_cells in rows[:3]:  # Check first 3 rows
                for cell_text in row_cells:
                    # Skip if it looks like a standard header
                    if normalize_table_header(cell_text):
                        continue
                    # Check if it looks like a property name
                    candidate = cell_text.strip()
                    if len(candidate) > 5 and len(candidate) < 100:
                        # Check for property indicators
                        candidate_lower = candidate.lower()
                        if any(indicator in candidate_lower for indicator in property_indicators):
                            candidate_clean = normalize_field_text(candidate)
                            if is_valid_property_name(candidate_clean):
                                property_context = candidate_clean
                                print(f"        🏨 Detected property name in table cell: {property_context}")
                                break
                if property_context:
                    break
        for idx, potential_header in enumerate(rows):
            candidate_map = [normalize_table_header(header) for header in potential_header]
            useful_count = sum(1 for item in candidate_map if item)
            if useful_count >= 2 and idx < len(rows) - 1:
                header_row = potential_header
                header_map = candidate_map
                header_index = idx
                break
        if not header_row:
            header_row = rows[0]
            header_map = [normalize_table_header(header) for header in header_row]
            header_index = 0
        useful_headers = [h for h in header_map if h]
        if useful_headers and len(rows) > header_index + 1:
            for data_row in rows[header_index + 1:]:
                entry = {}
                for idx, value in enumerate(data_row):
                    if idx >= len(header_map):
                        continue
                    key = header_map[idx]
                    if not key:
                        continue
                    if key not in entry or not entry[key]:
                        entry[key] = value.strip()
                cleaned_entry = sanitize_table_row(entry, fallback_property=property_context)
                if cleaned_entry:
                    print(f"        ✓ Table row parsed: {cleaned_entry}")
                    extracted_entries.append(cleaned_entry)
        elif useful_headers:
            pass
        else:
            kv_entry = {}
            for row in rows:
                if len(row) != 2:
                    continue
                key = normalize_table_header(row[0])
                if key:
                    value = row[1].strip()
                    if value:
                        kv_entry[key] = value
            if kv_entry:
                cleaned_entry = sanitize_table_row(kv_entry, fallback_property=property_context)
                if cleaned_entry:
                    print(f"        ✓ Key/value rows parsed: {cleaned_entry}")
                    extracted_entries.append(cleaned_entry)
    return extracted_entries, property_context

def extract_email_data_with_openai(subject, body, html_body, missing_fields=None, table_entry_context=None, extract_all_entries=False):
    """Extract booking details from email using OpenAI API as fallback
    
    Args:
        subject: Email subject
        body: Email body text
        html_body: Email HTML body
        missing_fields: List of field names to extract (if None, extracts all fields)
        table_entry_context: Dict with existing table entry data to fill missing fields for
        extract_all_entries: If True, extract all booking entries from email (returns array)
    
    Returns:
        Tuple of (booking_code, booking_details_dict) or (None, list_of_entries) if extract_all_entries=True
    """
    if not OPENAI_AVAILABLE:
        return None, {}
    
    # Check for Azure OpenAI credentials first, then fallback to regular OpenAI
    azure_api_key = os.getenv('AZURE_OPENAI_API_KEY')
    azure_base_url = os.getenv('AZURE_OPENAI_BASE_URL')
    openai_api_key = os.getenv('OPENAI_API_KEY')
    
    if not azure_api_key or not azure_base_url:
        if not openai_api_key:
            print("   ⚠️  Neither AZURE_OPENAI_API_KEY/AZURE_OPENAI_BASE_URL nor OPENAI_API_KEY found in .env - skipping OpenAI email extraction")
            return None, {}
        # Fallback to regular OpenAI
        api_key_to_use = openai_api_key
        base_url_to_use = None
    else:
        # Use Azure OpenAI
        api_key_to_use = azure_api_key
        base_url_to_use = azure_base_url
    
    try:
        # Initialize OpenAI client (Azure OpenAI or regular OpenAI)
        if base_url_to_use:
            client = OpenAI(api_key=api_key_to_use, base_url=base_url_to_use)
        else:
            client = OpenAI(api_key=api_key_to_use)
        
        # Prepare email content (remove HTML tags if present)
        email_content = body
        if html_body:
            # Try to extract text from HTML
            try:
                if BeautifulSoup:
                    soup = BeautifulSoup(html_body, 'html.parser')
                    html_text = soup.get_text(separator=' ', strip=True)
                    if html_text and len(html_text) > 100:
                        email_content = html_text
            except:
                pass
        
        # Combine subject and body
        full_email_text = f"Subject: {subject}\n\n{email_content}"
        
        # Limit content length to avoid token limits (keep last 8000 chars if too long)
        if len(full_email_text) > 8000:
            full_email_text = full_email_text[-8000:]

        def _booking_code_passes_guardrails(code: Optional[str], text: str) -> bool:
            """
            Guardrails for booking_code:
            - Only accept if the number appears next to one of these exact phrases (case-insensitive):
              "booking code", "booking number", "booking no".
            - Rejects e.g. "booking request", "booking confirmation number", bare "booking" + digits.
            """
            if not code or not text:
                return False
            digits = re.sub(r"\D", "", str(code))
            if not digits or len(digits) < 4 or len(digits) > 20:
                return False

            # Allow light separators between digits in the source text (spaces, hyphens, etc.)
            digits_pattern = r"\D{0,3}".join(map(re.escape, digits))
            t = str(text).lower()

            label_patterns = [
                rf"\bbooking\s+code\b[\s:#\-]*{digits_pattern}\b",
                rf"\bbooking\s+number\b[\s:#\-]*{digits_pattern}\b",
                rf"\bbooking\s+no\.?\b[\s:#\-]*{digits_pattern}\b",
            ]

            for pat in label_patterns:
                if re.search(pat, t, flags=re.IGNORECASE):
                    return True
            return False
        
        # Determine which fields to extract
        all_fields = ['booking_code', 'guest_name', 'property_name', 
                      'check_in_date', 'check_out_date', 'total_amount']
        
        # Build context string if we have table entry context
        context_string = ""
        if table_entry_context:
            context_parts = []
            for key, value in table_entry_context.items():
                if value and str(value).strip() and str(value).lower() not in ['not found', 'none', 'null', '']:
                    context_parts.append(f"{key}: {value}")
            if context_parts:
                context_string = f"\n\nEXISTING DATA (use this as context, but still extract missing fields):\n" + "\n".join(context_parts)
        
        if extract_all_entries:
            # Extract ALL booking entries from email
            fields_to_extract = all_fields
            print("   🤖 Using OpenAI to extract ALL booking entries from email...")
            
            prompt = f"""Analyze this email and extract ALL booking entries. The email may contain multiple bookings with different guest names, booking codes, dates, etc.

Return a JSON array where each object represents one booking entry with the following fields:
- booking_code: Only extract if explicitly labeled as "booking code", "booking number", or "booking no" next to the value. Do NOT use "booking request", "booking confirmation number", "booking reference", "reference", or any other label. Otherwise set it to null.
- guest_name: Guest name or client name
- property_name: Hotel name or property name (may appear without label)
- check_in_date: Check-in date or arrival date
- check_out_date: Check-out date or departure date
- total_amount: Total amount or payment amount

IMPORTANT:
- Extract ALL booking entries found in the email (there may be multiple)
- Each entry should have its own booking_code, guest_name, dates, etc.
- If a field is not found for an entry, use null
- Return an array even if there's only one booking

Return ONLY valid JSON array, no explanations.

Email content:
""" + full_email_text
        elif missing_fields:
            fields_to_extract = [f for f in all_fields if f in missing_fields]
            print(f"   🤖 Using OpenAI to extract missing fields: {', '.join(fields_to_extract)}")
            
            # Create detailed prompt for OpenAI with improved property name detection
            fields_description = {
                'booking_code': 'Booking code (numeric). ONLY extract if labeled exactly as "booking code", "booking number", or "booking no" next to the value. Do NOT use booking request, confirmation number, booking reference, reference, reservation code, etc. Otherwise return null.',
                'guest_name': 'Guest name or client name. Look in table rows, email body, or any mention of guest/client name.',
                'property_name': 'Hotel name or property name. IMPORTANT: The property name may NOT be explicitly labeled. Look for: (1) Hotel/property names in table headers (e.g., "Enrise by Sayaji Nagpur"), (2) Hotel names mentioned in the email subject or body without labels, (3) Any business/hotel name that appears to be the venue. Examples: "Enrise by Sayaji Nagpur", "Grand Hotel Mumbai", "Taj Palace". Extract the full property/hotel name even if it\'s not prefixed with "Property name:" or "Hotel:".',
                'check_in_date': 'Check-in date or arrival date. Look for dates labeled as "Check-In", "Arrival", "Check-InDate", etc.',
                'check_out_date': 'Check-out date or departure date. Look for dates labeled as "Check-Out", "Departure", "Check-OutDate", etc.',
                'total_amount': 'Total amount or payment amount. Look for "Total Amount", "Advance Paid Amount", "Amount Paid", "Total", etc.'
            }
            
            fields_list = "\n".join([f"- {field}: {fields_description.get(field, '')}" for field in fields_to_extract])
            
            prompt = f"""Analyze this email and extract booking-related information. Return a JSON object with ONLY the following fields:

{fields_list}

CRITICAL INSTRUCTIONS FOR PROPERTY NAME:
- The property/hotel name may appear WITHOUT any label (not as "Property name: XXX")
- Look for hotel names in table headers (e.g., "Enrise by Sayaji Nagpur" in a table header)
- Look for hotel names in the email subject line
- Look for hotel names mentioned anywhere in the email body
- Extract the FULL property name even if it's not explicitly labeled
- Examples: "Enrise by Sayaji Nagpur", "Grand Hotel Mumbai", "Taj Palace Delhi"

If a field is not found, use null. Return ONLY valid JSON, no explanations.
{context_string}

Email content:
""" + full_email_text
        else:
            fields_to_extract = all_fields
            print("   🤖 Using OpenAI to analyze entire email content...")
            
            # Create detailed prompt for OpenAI with improved property name detection
            fields_description = {
                'booking_code': 'Booking code (numeric). ONLY extract if labeled exactly as "booking code", "booking number", or "booking no" next to the value. Do NOT use booking request, confirmation number, booking reference, reference, reservation code, etc. Otherwise return null.',
                'guest_name': 'Guest name or client name. Look in table rows, email body, or any mention of guest/client name.',
                'property_name': 'Hotel name or property name. IMPORTANT: The property name may NOT be explicitly labeled. Look for: (1) Hotel/property names in table headers (e.g., "Enrise by Sayaji Nagpur"), (2) Hotel names mentioned in the email subject or body without labels, (3) Any business/hotel name that appears to be the venue. Examples: "Enrise by Sayaji Nagpur", "Grand Hotel Mumbai", "Taj Palace". Extract the full property/hotel name even if it\'s not prefixed with "Property name:" or "Hotel:".',
                'check_in_date': 'Check-in date or arrival date. Look for dates labeled as "Check-In", "Arrival", "Check-InDate", etc.',
                'check_out_date': 'Check-out date or departure date. Look for dates labeled as "Check-Out", "Departure", "Check-OutDate", etc.',
                'total_amount': 'Total amount or payment amount. Look for "Total Amount", "Advance Paid Amount", "Amount Paid", "Total", etc.'
            }
            
            fields_list = "\n".join([f"- {field}: {fields_description.get(field, '')}" for field in fields_to_extract])
            
            prompt = f"""Analyze this email and extract booking-related information. Return a JSON object with ONLY the following fields:

{fields_list}

CRITICAL INSTRUCTIONS FOR PROPERTY NAME:
- The property/hotel name may appear WITHOUT any label (not as "Property name: XXX")
- Look for hotel names in table headers (e.g., "Enrise by Sayaji Nagpur" in a table header)
- Look for hotel names in the email subject line
- Look for hotel names mentioned anywhere in the email body
- Extract the FULL property name even if it's not explicitly labeled
- Examples: "Enrise by Sayaji Nagpur", "Grand Hotel Mumbai", "Taj Palace Delhi"

If a field is not found, use null. Return ONLY valid JSON, no explanations.

Email content:
""" + full_email_text
        
        # Call OpenAI API
        # For Azure OpenAI, use deployment name from env, otherwise use default model name
        model_name = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o-mini")
        # Check if using Azure OpenAI (has base_url)
        is_azure = azure_api_key and azure_base_url
        
        # Azure OpenAI uses max_completion_tokens, regular OpenAI uses max_tokens
        # Azure OpenAI doesn't support custom temperature (only default 1), so omit it for Azure
        api_params = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": "You are a data extraction assistant. Extract booking information from emails and return only valid JSON."},
                {"role": "user", "content": prompt}
            ]
        }
        if is_azure:
            api_params["max_completion_tokens"] = 1000
            # Don't set temperature for Azure OpenAI - it only supports default (1)
        else:
            api_params["max_tokens"] = 1000
            api_params["temperature"] = 0.1
        
        response = client.chat.completions.create(**api_params)
        
        ai_response_text = response.choices[0].message.content.strip()
        
        # Try to parse JSON response
        try:
            # Remove markdown code blocks if present
            if ai_response_text.startswith('```'):
                # Extract JSON from code block
                lines = ai_response_text.split('\n')
                json_lines = []
                in_json = False
                for line in lines:
                    if line.strip().startswith('```'):
                        if not in_json:
                            in_json = True
                        else:
                            break
                    elif in_json:
                        json_lines.append(line)
                ai_response_text = '\n'.join(json_lines)
            
            extracted_data = json.loads(ai_response_text)
            
            # Handle array response (extract_all_entries mode)
            if extract_all_entries:
                if isinstance(extracted_data, list):
                    all_entries = []
                    for idx, entry in enumerate(extracted_data):
                        if not isinstance(entry, dict):
                            continue
                        
                        # Clean and validate booking code
                        booking_code = None
                        if entry.get('booking_code'):
                            booking_code_str = str(entry['booking_code']).strip()
                            digits = re.sub(r'\D', '', booking_code_str)
                            # Accept booking codes with 4-20 digits
                            if len(digits) >= 4 and len(digits) <= 20 and digits.isdigit():
                                booking_code = digits if _booking_code_passes_guardrails(digits, full_email_text) else None
                        
                        # Build entry dict
                        cleaned_entry = {
                            'booking_code': booking_code,
                            'guest_name': str(entry.get('guest_name', '')).strip() if entry.get('guest_name') else None,
                            'property_name': str(entry.get('property_name', '')).strip() if entry.get('property_name') else None,
                            'check_in_date': str(entry.get('check_in_date', '')).strip() if entry.get('check_in_date') else None,
                            'check_out_date': str(entry.get('check_out_date', '')).strip() if entry.get('check_out_date') else None,
                            'total_amount': str(entry.get('total_amount', '')).strip() if entry.get('total_amount') else None
                        }
                        
                        # Remove None values
                        cleaned_entry = {k: v for k, v in cleaned_entry.items() if v is not None and str(v).lower() not in ['null', 'none', '']}
                        
                        if cleaned_entry:
                            all_entries.append(cleaned_entry)
                    
                    print(f"   ✅ OpenAI extracted {len(all_entries)} booking entry/entries from email")
                    for idx, entry in enumerate(all_entries, 1):
                        print(f"      Entry {idx}:")
                        for field, value in entry.items():
                            print(f"         • {field}: {value}")
                    
                    return None, {'table_entries': all_entries, 'all_entries_extracted': True}
                else:
                    # Single entry returned as object, convert to array format
                    all_entries = []
                    # Clean and validate booking code
                    booking_code = None
                    if extracted_data.get('booking_code'):
                        booking_code_str = str(extracted_data['booking_code']).strip()
                        digits = re.sub(r'\D', '', booking_code_str)
                        # Accept booking codes with 4-20 digits
                        if len(digits) >= 4 and len(digits) <= 20 and digits.isdigit():
                            booking_code = digits if _booking_code_passes_guardrails(digits, full_email_text) else None
                    
                    # Build entry dict
                    cleaned_entry = {
                        'booking_code': booking_code,
                        'guest_name': str(extracted_data.get('guest_name', '')).strip() if extracted_data.get('guest_name') else None,
                        'property_name': str(extracted_data.get('property_name', '')).strip() if extracted_data.get('property_name') else None,
                        'check_in_date': str(extracted_data.get('check_in_date', '')).strip() if extracted_data.get('check_in_date') else None,
                        'check_out_date': str(extracted_data.get('check_out_date', '')).strip() if extracted_data.get('check_out_date') else None,
                        'total_amount': str(extracted_data.get('total_amount', '')).strip() if extracted_data.get('total_amount') else None
                    }
                    
                    # Remove None values
                    cleaned_entry = {k: v for k, v in cleaned_entry.items() if v is not None and str(v).lower() not in ['null', 'none', '']}
                    
                    if cleaned_entry:
                        all_entries.append(cleaned_entry)
                    
                    print(f"   ✅ OpenAI extracted {len(all_entries)} booking entry/entries from email")
                    for idx, entry in enumerate(all_entries, 1):
                        print(f"      Entry {idx}:")
                        for field, value in entry.items():
                            print(f"         • {field}: {value}")
                    
                    return None, {'table_entries': all_entries, 'all_entries_extracted': True}
            
            # Handle single entry response (missing fields mode)
            # Extract booking code
            booking_code = None
            if extracted_data.get('booking_code'):
                booking_code_str = str(extracted_data['booking_code']).strip()
                # Clean and validate booking code (4-20 digits)
                digits = re.sub(r'\D', '', booking_code_str)
                # Accept booking codes with 4-20 digits
                if len(digits) >= 4 and len(digits) <= 20 and digits.isdigit():
                    booking_code = digits if _booking_code_passes_guardrails(digits, full_email_text) else None
            
            # Build booking details dictionary (only include requested fields)
            booking_details = {}
            if missing_fields:
                # Only extract requested missing fields
                for field in missing_fields:
                    if extracted_data.get(field) and extracted_data[field] not in [None, 'null', 'None', '']:
                        booking_details[field] = str(extracted_data[field]).strip()
            else:
                # Extract all fields
                if extracted_data.get('guest_name'):
                    booking_details['guest_name'] = str(extracted_data['guest_name']).strip()
                if extracted_data.get('property_name'):
                    booking_details['property_name'] = str(extracted_data['property_name']).strip()
                if extracted_data.get('check_in_date'):
                    booking_details['check_in_date'] = format_date_to_dd_mm_yy(str(extracted_data['check_in_date']).strip())
                if extracted_data.get('check_out_date'):
                    booking_details['check_out_date'] = format_date_to_dd_mm_yy(str(extracted_data['check_out_date']).strip())
                if extracted_data.get('total_amount'):
                    booking_details['total_amount'] = str(extracted_data['total_amount']).strip()
            
            if booking_code:
                booking_details['booking_code'] = booking_code
                booking_details['booking_code_source'] = 'openai_fallback'
            
            # Create a table entry from extracted data
            if booking_details:
                table_entry = {
                    'booking_code': booking_code,
                    'guest_name': booking_details.get('guest_name'),
                    'property_name': booking_details.get('property_name'),
                    'check_in_date': booking_details.get('check_in_date'),
                    'check_out_date': booking_details.get('check_out_date'),
                    'total_amount': booking_details.get('total_amount')
                }
                booking_details['table_entries'] = [table_entry]
                booking_details['best_table_entry'] = table_entry
            
            # Log extracted fields with their values
            print(f"   ✅ OpenAI extracted email data: {len(booking_details)} fields found")
            for field, value in booking_details.items():
                if field not in ['booking_code_source', 'table_entries', 'best_table_entry']:
                    print(f"      • {field}: {value}")
            if booking_code:
                print(f"   ✅ Booking code extracted via OpenAI: {booking_code}")
            
            return booking_code, booking_details
            
        except json.JSONDecodeError as e:
            print(f"   ⚠️  OpenAI returned invalid JSON: {e}")
            print(f"   📋 Raw response: {ai_response_text[:200]}...")
            return None, {}
            
    except Exception as e:
        print(f"   ⚠️  Error extracting email data with OpenAI: {e}")
        return None, {}

def extract_booking_code_from_email(email_message, subject, body, html_body, attachment_texts=None):
    """Extract booking details (code, guest, dates, property) from email content"""

    booking_details = {}

    def clean_field_value(value: Optional[str]) -> Optional[str]:
        if not value or not isinstance(value, str):
            return None
        value_clean = value.strip()
        value_clean = re.sub(r'^[\-:>#~\s]+', '', value_clean)
        value_clean = re.sub(r'\s+', ' ', value_clean)
        value_clean = value_clean.strip('"\'`')
        if not value_clean:
            return None
        header_tokens = ['guest name', 'client name', 'hotel name', 'property name',
                         'check-in date', 'check out', 'check-out date', 'checkin date',
                         'checkout date', 'arrival date', 'departure date']
        lower_val = value_clean.lower()
        if any(token in lower_val for token in header_tokens):
            return None
        return value_clean.strip()

    def store_detail(key, value):
        if value and isinstance(value, str):
            value_clean = clean_field_value(value)
            if value_clean and key not in booking_details:
                booking_details[key] = value_clean

    def clean_booking_value(value: str):
        value = value.strip()
        value = re.sub(r'^[\-:>#~\s]+', '', value)
        value = re.sub(r'[\s,.;]+$', '', value)
        value = value.strip('"\'`')
        value = value.replace(' ', '')
        value = value.strip()
        # Extract any sequence of digits (4-20 digits for booking codes)
        digits_match = re.search(r'(\d{4,20})', value)
        if digits_match:
            return digits_match.group(1)
        return value

    def is_valid_booking_code(value: str, context_label: Optional[str] = None) -> bool:
        if not value:
            return False
        digits = re.sub(r'\D', '', value)
        if not digits:
            return False
        # Accept booking codes with 4-20 digits (flexible length)
        if len(digits) < 4 or len(digits) > 20:
            return False
        return digits.isdigit()

    def booking_code_has_allowed_context(code: str) -> bool:
        """
        Guardrails: only accept if the number appears next to exactly one of:
        "booking code", "booking number", "booking no" (case-insensitive).
        """
        if not code:
            return False
        digits = re.sub(r"\D", "", str(code))
        if not digits or len(digits) < 4 or len(digits) > 20:
            return False

        # Combine all available text sources for context validation
        parts = []
        if subject:
            parts.append(f"Subject: {subject}")
        if body:
            parts.append(str(body))
        if html_body:
            parts.append(str(html_body))
        if attachment_texts:
            try:
                parts.extend([str(t) for t in attachment_texts if t])
            except Exception:
                pass
        full_text = "\n".join(parts).lower()
        if not full_text:
            return False

        digits_pattern = r"\D{0,3}".join(map(re.escape, digits))
        patterns = [
            rf"\bbooking\s+code\b[\s:#\-]*{digits_pattern}\b",
            rf"\bbooking\s+number\b[\s:#\-]*{digits_pattern}\b",
            rf"\bbooking\s+no\.?\b[\s:#\-]*{digits_pattern}\b",
        ]
        return any(re.search(p, full_text, flags=re.IGNORECASE) for p in patterns)

    booking_code = None
    property_context = None

    # 1. Structured HTML table parsing (handles emails with booking tables)
    if html_body:
        print("   🔎 Parsing HTML tables for booking data...")
        table_entries, property_context = parse_html_tables_for_booking_data(html_body)
        if property_context:
            booking_details['property_context'] = property_context
            if 'property_name' not in booking_details:
                store_detail('property_name', property_context)
        if table_entries:
            print(f"   ✅ Parsed {len(table_entries)} table row(s) with potential data")
            booking_details['table_entries'] = table_entries
            # Identify the most complete table entry for downstream use
            best_table_entry = None
            for entry in table_entries:
                candidate = entry.get('booking_code') or entry.get('confirmation_number') or entry.get('reference')
                if candidate and not booking_code:
                    candidate_clean = clean_booking_value(candidate)
                    # Table headers are normalized to booking fields; still apply strict context guardrails.
                    if is_valid_booking_code(candidate_clean, context_label='email_table') and booking_code_has_allowed_context(candidate_clean):
                        booking_code = candidate_clean
                        booking_details['booking_code_source'] = 'email_table'
                        print(f"   ✅ Booking code detected in table row: {booking_code}")
                if entry.get('guest_name'):
                    store_detail('guest_name', entry['guest_name'])
                # client_name is same as guest_name, so use it as fallback for guest_name
                if entry.get('client_name') and 'guest_name' not in booking_details:
                    store_detail('guest_name', entry['client_name'])
                if entry.get('property_name'):
                    store_detail('property_name', entry['property_name'])
                if entry.get('check_in_date'):
                    store_detail('check_in_date', entry['check_in_date'])
                if entry.get('check_out_date'):
                    store_detail('check_out_date', entry['check_out_date'])
                    if not best_table_entry:
                        best_table_entry = entry
                    else:
                        existing_score = sum(1 for key in ['property_name', 'guest_name', 'check_in_date', 'check_out_date']
                                             if best_table_entry.get(key))
                        new_score = sum(1 for key in ['property_name', 'guest_name', 'check_in_date', 'check_out_date']
                                        if entry.get(key))
                        if new_score > existing_score:
                            best_table_entry = entry
            if best_table_entry:
                booking_details['best_table_entry'] = best_table_entry
            booking_details['table_entries'] = table_entries
        if booking_code:
            print(f"   ✅ Booking code extracted from HTML table: {booking_code}")
            booking_details['booking_code'] = booking_code
        elif table_entries:
            print("   ⚠️  Table rows found but no valid booking code parsed")
        else:
            print("   ⚠️  No booking-relevant tables detected in HTML body")

    # 2. Fallback to OpenAI for missing fields or extract all entries
    if OPENAI_AVAILABLE:
        # Helper function to check if a field value is valid
        def is_field_valid(value):
            """Check if a field value is valid (not empty, None, or 'Not Found')"""
            if not value:
                return False
            if isinstance(value, str):
                value_lower = value.lower().strip()
                if value_lower in ['', 'not found', 'none', 'null', 'n/a', 'na']:
                    return False
            return True
        
        table_entries = booking_details.get('table_entries', [])
        
        # Case 1: If we have table entries, fill missing fields for EACH entry
        if table_entries:
            print(f"   🔄 Using OpenAI to fill missing fields for {len(table_entries)} table entry/entries...")
            all_fields = ['booking_code', 'guest_name', 'property_name', 
                          'check_in_date', 'check_out_date', 'total_amount']
            
            enhanced_entries = []
            for idx, entry in enumerate(table_entries, 1):
                print(f"   📋 Processing table entry {idx}/{len(table_entries)}...")
                
                # Determine which fields are missing for this entry
                missing_fields = []
                for field in all_fields:
                    if field == 'property_name':
                        # Check entry and property_context
                        if not is_field_valid(entry.get('property_name')) and not is_field_valid(property_context):
                            missing_fields.append(field)
                    else:
                        if not is_field_valid(entry.get(field)):
                            missing_fields.append(field)
                
                # If there are missing fields, use OpenAI to fill them
                if missing_fields:
                    try:
                        # Prepare context from existing entry data
                        entry_context = {k: v for k, v in entry.items() if is_field_valid(v)}
                        if property_context and 'property_name' not in entry_context:
                            entry_context['property_name'] = property_context
                        
                        openai_booking_code, openai_booking_details = extract_email_data_with_openai(
                            subject, body, html_body, 
                            missing_fields=missing_fields,
                            table_entry_context=entry_context
                        )
                        
                        # Merge OpenAI results into entry
                        enhanced_entry = entry.copy()
                        if openai_booking_code and not enhanced_entry.get('booking_code'):
                            enhanced_entry['booking_code'] = openai_booking_code
                        
                        for key in missing_fields:
                            if is_field_valid(openai_booking_details.get(key)):
                                enhanced_entry[key] = openai_booking_details[key]
                                print(f"      ✅ Filled {key} = '{openai_booking_details[key]}'")
                    except Exception as e:
                        print(f"      ⚠️  Error filling missing fields for entry {idx}: {e}")
                        enhanced_entry = entry.copy()
                else:
                    enhanced_entry = entry.copy()
                
                enhanced_entries.append(enhanced_entry)
            
            # Update booking_details with enhanced entries
            booking_details['table_entries'] = enhanced_entries
            if enhanced_entries:
                # Find best entry (most complete)
                best_entry = max(enhanced_entries, key=lambda e: sum(1 for k in ['property_name', 'guest_name', 'check_in_date', 'check_out_date', 'booking_code'] if is_field_valid(e.get(k))))
                booking_details['best_table_entry'] = best_entry
                
                # Set booking_code from best entry if not already set
                if not booking_code and is_field_valid(best_entry.get('booking_code')):
                    booking_code = best_entry['booking_code']
                    booking_details['booking_code'] = booking_code
                
                # Set property_name from best entry to top-level booking_details (for vendor matching)
                # Priority: OpenAI extracted property_name from email > property_context from HTML tables
                if is_field_valid(best_entry.get('property_name')):
                    booking_details['property_name'] = best_entry['property_name']
                    print(f"   ✅ Copied property_name from best table entry to top-level: {best_entry['property_name']}")
                elif property_context and is_field_valid(property_context):
                    booking_details['property_name'] = property_context
                    print(f"   ✅ Using property_context as property_name: {property_context}")
        
        # Case 2: If no table entries, extract ALL entries from email body
        else:
            print("   🔄 No table entries found - using OpenAI to extract ALL booking entries from email...")
            try:
                openai_booking_code, openai_booking_details = extract_email_data_with_openai(
                    subject, body, html_body, extract_all_entries=True
                )
                
                if openai_booking_details and openai_booking_details.get('all_entries_extracted'):
                    # Use all entries extracted by OpenAI
                    all_entries = openai_booking_details.get('table_entries', [])
                    if all_entries:
                        booking_details['table_entries'] = all_entries
                        best_entry = all_entries[0] if all_entries else None
                        booking_details['best_table_entry'] = best_entry
                        
                        # Set booking_code from first entry if available
                        if all_entries and all_entries[0].get('booking_code'):
                            booking_code = all_entries[0]['booking_code']
                            booking_details['booking_code'] = booking_code
                            booking_details['booking_code_source'] = 'openai_fallback'
                        
                        # Set property_name from best entry to top-level booking_details (for vendor matching)
                        if best_entry and is_field_valid(best_entry.get('property_name')):
                            booking_details['property_name'] = best_entry['property_name']
                            print(f"   ✅ Copied property_name from OpenAI extracted entry to top-level: {best_entry['property_name']}")
                        
                        print(f"   ✅ Using {len(all_entries)} entry/entries extracted by OpenAI")
            except Exception as e:
                print(f"   ⚠️  Error extracting all entries with OpenAI: {e}")

    if not booking_code:
        print("   ⚠️  No booking code found via HTML tables or OpenAI")

    if booking_code and 'booking_code' not in booking_details:
        booking_details['booking_code'] = booking_code

    return booking_code, booking_details

def extract_comprehensive_email_data(email_message, subject, from_email, body):
    """Extract comprehensive information from email and attachments"""
    # Extract email headers
    to_email = email_message.get('To', '')
    cc_email = email_message.get('Cc', '')
    bcc_email = email_message.get('Bcc', '')
    reply_to = email_message.get('Reply-To', '')
    message_id = email_message.get('Message-ID', '')
    in_reply_to = email_message.get('In-Reply-To', '')
    references = email_message.get('References', '')
    
    # Extract both plain text and HTML body for booking code extraction
    body_full, html_body = extract_email_body_full(email_message)
    if not body_full:
        body_full = body  # Fallback to provided body
    
    # Extract attachments
    attachments, attachment_texts = extract_attachment_info(email_message)
    
    # Extract booking code and related details from email/tables/AI
    booking_code = None
    email_booking_details = {}
    try:
        booking_code, email_booking_details = extract_booking_code_from_email(
            email_message,
            subject,
            body_full,
            html_body,
            attachment_texts=attachment_texts
        )
    except Exception as e:
        print(f"   ⚠️  Error during booking detail extraction: {e}")
        booking_code = None
        email_booking_details = {}
    
    # Get all guest names from email table entries for matching with PDFs
    email_table_entries = email_booking_details.get('table_entries', []) if email_booking_details else []
    email_guest_names = []
    if email_table_entries:
        # Extract all unique guest names from table entries
        for entry in email_table_entries:
            guest_name = entry.get('guest_name') or entry.get('client_name')
            if guest_name:
                normalized = normalize_field_text(guest_name)
                if normalized and normalized not in ['', 'not found'] and normalized not in email_guest_names:
                    email_guest_names.append(normalized)
    elif email_booking_details:
        # Fallback to single guest name if no table entries
        email_guest_name = email_booking_details.get('guest_name')
        if email_guest_name:
            normalized = normalize_field_text(email_guest_name)
            if normalized and normalized not in ['', 'not found']:
                email_guest_names.append(normalized)
    
    if email_guest_names:
        print(f"   👤 Guest name(s) from email: {', '.join(email_guest_names)}")
    else:
        print(f"   👤 No guest names found in email")
    
    # Helper function to normalize names for comparison
    def normalize_name_for_matching(name: str) -> dict:
        """
        Normalize a name for comparison. Returns a dict with:
        - full_normalized: Full name normalized
        - first_name: First name
        - last_name: Last name
        - name_parts: List of significant name parts (excluding single letters)
        - initials: List of initials found (for better matching)
        """
        if not name:
            return {'full_normalized': '', 'first_name': '', 'last_name': '', 'name_parts': [], 'initials': []}
        
        # Basic normalization
        name_clean = normalize_field_text(name).strip()
        if not name_clean:
            return {'full_normalized': '', 'first_name': '', 'last_name': '', 'name_parts': [], 'initials': []}
        
        # Remove common titles
        titles = ['mr', 'mrs', 'ms', 'miss', 'dr', 'prof', 'sir', 'madam', 'mr.', 'mrs.', 'ms.', 'miss.', 'dr.', 'prof.']
        name_clean_lower = name_clean.lower()
        for title in titles:
            if name_clean_lower.startswith(title + '.'):
                name_clean = name_clean[len(title) + 1:].strip()
            elif name_clean_lower.startswith(title + ' '):
                name_clean = name_clean[len(title):].strip()
        
        # Handle comma-separated names (Last, First format) - case-insensitive
        # Examples: "Kuttappa, Varun", "KUTTAPPA, VARUN", "Kuttappa, VARUN" all become "varun kuttappa"
        if ',' in name_clean:
            parts = [p.strip() for p in name_clean.split(',')]
            if len(parts) == 2:
                # Reverse: "Kuttappa, Varun" -> "Varun Kuttappa" (will be lowercased later)
                # This handles all case combinations: "KUTTAPPA, VARUN", "Kuttappa, VARUN", etc.
                name_clean = f"{parts[1]} {parts[0]}".strip()
        
        # Split into parts
        name_parts = [p.strip() for p in name_clean.split() if p.strip()]
        
        # NEW: Expand parts that might be concatenated (e.g., "ANILKUMAR" might need to match "ANIL KUMAR")
        # We'll handle this in matching by normalizing spaces
        # (The space normalization happens in the matching function)
        
        # Keep track of original parts (including single letters) for better matching
        # Single letters might be abbreviated last names (e.g., "Varun K" where K = Kuttappa)
        original_parts = name_parts.copy()
        
        # Extract initials (single letters, possibly with periods)
        # NEW: Also handle two-letter abbreviations like "KI" as two initials
        initials = []
        significant_parts = []
        for part in name_parts:
            # Remove periods from initials (e.g., "V." -> "V")
            part_clean = part.rstrip('.')
            if len(part_clean) == 1:
                initials.append(part_clean.lower())
            elif len(part_clean) == 2 and part_clean.isalpha() and part_clean.isupper():
                # NEW: Handle two-letter abbreviations like "KI" as two initials
                # This helps match "Varun KI" with "Varun Kuttappa Ichettiera"
                # Split into two initials: "KI" -> ["K", "I"]
                initials.append(part_clean[0].lower())
                initials.append(part_clean[1].lower())
            else:
                significant_parts.append(part)
        
        # Extract first and last name
        first_name = significant_parts[0] if significant_parts else ''
        last_name = significant_parts[-1] if len(significant_parts) > 1 else ''
        
        # If we have a single letter at the end and no last name, it might be an abbreviated last name
        # e.g., "Varun K" - K might stand for Kuttappa
        if not last_name and len(original_parts) >= 2 and len(original_parts[-1].rstrip('.')) == 1:
            # The last part is a single letter - might be abbreviated last name
            # We'll use it for matching but won't include it in significant_parts
            pass
        
        # Create normalized full name (all significant parts)
        full_normalized = ' '.join(significant_parts).lower().strip()
        
        # Also create reversed version for names that might be in "Last First" order
        # e.g., "Kuttappa Varun" (Last First) should match "Varun Kuttappa" (First Last)
        full_normalized_reversed = ' '.join(reversed(significant_parts)).lower().strip() if len(significant_parts) >= 2 else full_normalized
        
        return {
            'full_normalized': full_normalized,
            'full_normalized_reversed': full_normalized_reversed,  # For "Last First" format names
            'first_name': first_name.lower() if first_name else '',
            'last_name': last_name.lower() if last_name else '',
            'name_parts': [p.lower() for p in significant_parts],
            'original_parts': [p.lower().rstrip('.') for p in original_parts],  # Keep original for reference
            'initials': initials  # Track initials for matching
        }
    
    # Helper function to split multiple names (handles semicolon and comma separators)
    def split_multiple_names(name_str):
        """Split a name string that may contain multiple names separated by semicolon or comma"""
        if not name_str:
            return []
        # Split by semicolon first, then by comma
        names = []
        for part in str(name_str).split(';'):
            for subpart in part.split(','):
                cleaned = subpart.strip()
                if cleaned and cleaned.lower() not in ['', 'not found']:
                    names.append(cleaned)
        return names
    
    # Helper function to normalize spaces in names (handles "ANILKUMAR" vs "ANIL KUMAR")
    def normalize_spaces_in_name(name):
        """Normalize spaces - helps match 'ANILKUMAR' with 'ANIL KUMAR'"""
        if not name:
            return name
        # Convert to lowercase for comparison
        name_lower = name.lower()
        # For all-uppercase long words, try to add spaces at common boundaries
        # This is a heuristic - we'll rely more on fuzzy matching
        return name_lower
    
    # Helper function to compare guest names (improved matching)
    def guest_names_match(email_guest, pdf_guest):
        """Check if two guest names match using improved normalization and fuzzy matching
        Handles multiple names separated by semicolon/comma"""
        if not email_guest or not pdf_guest:
            return False
        
        # NEW: Handle multiple names (split by semicolon/comma)
        email_names = split_multiple_names(email_guest)
        pdf_names = split_multiple_names(pdf_guest)
        
        # If either has multiple names, try matching each combination
        if len(email_names) > 1 or len(pdf_names) > 1:
            for email_name in email_names:
                for pdf_name in pdf_names:
                    if guest_names_match_single(email_name, pdf_name):
                        return True
            return False
        
        # Single name matching
        return guest_names_match_single(email_guest, pdf_guest)
    
    # Helper function to compare single guest names (improved matching)
    def guest_names_match_single(email_guest, pdf_guest):
        """Check if two single guest names match using improved normalization and fuzzy matching"""
        if not email_guest or not pdf_guest:
            return False
        
        # Normalize both names
        email_norm = normalize_name_for_matching(email_guest)
        pdf_norm = normalize_name_for_matching(pdf_guest)
        
        # Early exit if both are empty after normalization
        if not email_norm['full_normalized'] and not pdf_norm['full_normalized']:
            return False
        
        # FIX: Early rejection if last names are clearly different (prevents false matches)
        # BUT: Don't reject if names might be in reversed order (check all name parts first)
        # This catches cases like "BANERJEE" vs "CHATTERJEE" before any fuzzy matching
        if email_norm['last_name'] and pdf_norm['last_name']:
            # Check if last names match directly
            if email_norm['last_name'] == pdf_norm['last_name']:
                pass  # Last names match, continue
            else:
                # Check if names might be in reversed order
                # If email last_name matches PDF first_name, or vice versa, don't reject
                email_last_matches_pdf_first = email_norm['last_name'] == pdf_norm.get('first_name', '')
                pdf_last_matches_email_first = pdf_norm['last_name'] == email_norm.get('first_name', '')
                
                if not email_last_matches_pdf_first and not pdf_last_matches_email_first:
                    # Not reversed order, check similarity
                    try:
                        last_name_similarity = fuzz.ratio(email_norm['last_name'], pdf_norm['last_name'])
                        if last_name_similarity < 90:
                            # Last names are clearly different and not reversed - don't match
                            return False
                    except:
                        pass
        
        # NEW: Check if all name parts match (regardless of order) - handles "Jitendra Balasaheb Kolapkar" vs "Kolapkar Balasaheb Jitendra"
        email_parts_set = set(email_norm['name_parts'])
        pdf_parts_set = set(pdf_norm['name_parts'])
        if email_parts_set and pdf_parts_set:
            # If all parts match (same set), they're the same person
            if email_parts_set == pdf_parts_set and len(email_parts_set) >= 2:
                return True
            
            # NEW: If at least 2 name parts match, consider it a match (more lenient)
            matching_parts = email_parts_set & pdf_parts_set
            if len(matching_parts) >= 2:
                return True
            
            # NEW: If first name matches and at least one other part matches (could be last name or middle name)
            if email_norm['first_name'] and pdf_norm['first_name']:
                if email_norm['first_name'] == pdf_norm['first_name']:
                    # First names match, check if any other part matches
                    other_email_parts = email_parts_set - {email_norm['first_name']}
                    other_pdf_parts = pdf_parts_set - {pdf_norm['first_name']}
                    if other_email_parts & other_pdf_parts:
                        # At least one other part matches (last name, middle name, etc.)
                        return True
            
            # NEW: Check if first name matches and there's an initial that could match
            # e.g., "John K" vs "John Kumar" or "J Kumar" vs "John Kumar"
            if email_norm['first_name'] and pdf_norm['first_name']:
                # Check if first names match
                if email_norm['first_name'] == pdf_norm['first_name']:
                    # Check if one has initials that match the other's name parts
                    email_initials = email_norm.get('initials', [])
                    pdf_initials = pdf_norm.get('initials', [])
                    
                    # Check if email initials match any PDF name part's first letter
                    for initial in email_initials:
                        for pdf_part in pdf_parts_set:
                            if pdf_part and pdf_part[0].lower() == initial.lower():
                                # Initial matches a name part - likely the same person
                                return True
                    
                    # Check if PDF initials match any email name part's first letter
                    for initial in pdf_initials:
                        for email_part in email_parts_set:
                            if email_part and email_part[0].lower() == initial.lower():
                                # Initial matches a name part - likely the same person
                                return True
                    
                    # NEW: Handle two-letter abbreviations like "KI" matching multiple name parts
                    # e.g., "Varun KI" should match "Varun Kuttappa Ichettiera"
                    # Check if one has a two-letter part that matches two initials or two name parts
                    email_parts_list = email_norm.get('name_parts', [])
                    pdf_parts_list = pdf_norm.get('name_parts', [])
                    
                    # Check if email has a two-letter uppercase part that matches PDF name parts
                    for email_part in email_parts_list:
                        if len(email_part) == 2 and email_part.isupper() and email_part.isalpha():
                            # "KI" -> check if "K" and "I" match PDF name parts' first letters
                            if len(pdf_initials) >= 2:
                                # Check if PDF has two initials that match
                                if (email_part[0].lower() in [i.lower() for i in pdf_initials] and 
                                    email_part[1].lower() in [i.lower() for i in pdf_initials]):
                                    return True
                            # Check if PDF has name parts starting with K and I
                            pdf_starts_with_k = any(p and p[0].lower() == email_part[0].lower() for p in pdf_parts_set)
                            pdf_starts_with_i = any(p and p[0].lower() == email_part[1].lower() for p in pdf_parts_set)
                            if pdf_starts_with_k and pdf_starts_with_i:
                                return True
                    
                    # Check if PDF has a two-letter uppercase part that matches email name parts
                    for pdf_part in pdf_parts_list:
                        if len(pdf_part) == 2 and pdf_part.isupper() and pdf_part.isalpha():
                            # "KI" -> check if "K" and "I" match email name parts' first letters
                            if len(email_initials) >= 2:
                                # Check if email has two initials that match
                                if (pdf_part[0].lower() in [i.lower() for i in email_initials] and 
                                    pdf_part[1].lower() in [i.lower() for i in email_initials]):
                                    return True
                            # Check if email has name parts starting with K and I
                            email_starts_with_k = any(p and p[0].lower() == pdf_part[0].lower() for p in email_parts_set)
                            email_starts_with_i = any(p and p[0].lower() == pdf_part[1].lower() for p in email_parts_set)
                            if email_starts_with_k and email_starts_with_i:
                                return True
                
                # Also check reversed: email first matches PDF last (or vice versa) with initial
                if email_norm['first_name'] == pdf_norm.get('last_name', ''):
                    # Email first matches PDF last - check if initials match
                    email_initials = email_norm.get('initials', [])
                    pdf_initials = pdf_norm.get('initials', [])
                    for initial in email_initials:
                        for pdf_part in pdf_parts_set:
                            if pdf_part and pdf_part[0].lower() == initial.lower():
                                return True
                    for initial in pdf_initials:
                        for email_part in email_parts_set:
                            if email_part and email_part[0].lower() == initial.lower():
                                return True
                
                if pdf_norm['first_name'] == email_norm.get('last_name', ''):
                    # PDF first matches email last - check if initials match
                    email_initials = email_norm.get('initials', [])
                    pdf_initials = pdf_norm.get('initials', [])
                    for initial in email_initials:
                        for pdf_part in pdf_parts_set:
                            if pdf_part and pdf_part[0].lower() == initial.lower():
                                return True
                    for initial in pdf_initials:
                        for email_part in email_parts_set:
                            if email_part and email_part[0].lower() == initial.lower():
                                return True
        
        # NEW: Handle space differences (e.g., "ANILKUMAR" vs "ANIL KUMAR")
        # Normalize by removing all spaces and compare
        email_no_spaces = email_norm['full_normalized'].replace(' ', '')
        pdf_no_spaces = pdf_norm['full_normalized'].replace(' ', '')
        if email_no_spaces and pdf_no_spaces and email_no_spaces == pdf_no_spaces:
            return True
        
        # Also check with spaces removed and sets compared
        email_parts_no_spaces_set = {p.replace(' ', '') for p in email_norm['name_parts']}
        pdf_parts_no_spaces_set = {p.replace(' ', '') for p in pdf_norm['name_parts']}
        if email_parts_no_spaces_set and pdf_parts_no_spaces_set:
            if email_parts_no_spaces_set == pdf_parts_no_spaces_set and len(email_parts_no_spaces_set) >= 2:
                return True
        
        # Exact match on normalized full name (try both normal and reversed orderings)
        # This handles: "Mr Kuttappa Varun" (becomes "kuttappa varun") vs "Varun Kuttappa" (becomes "varun kuttappa")
        # We check if either ordering matches
        if (email_norm['full_normalized'] == pdf_norm['full_normalized'] or
            email_norm['full_normalized'] == pdf_norm.get('full_normalized_reversed', '') or
            email_norm.get('full_normalized_reversed', '') == pdf_norm['full_normalized'] or
            email_norm.get('full_normalized_reversed', '') == pdf_norm.get('full_normalized_reversed', '')):
            return True
        
        # NEW: Try reversed order with no spaces
        email_rev_no_spaces = email_norm.get('full_normalized_reversed', '').replace(' ', '')
        pdf_rev_no_spaces = pdf_norm.get('full_normalized_reversed', '').replace(' ', '')
        if (email_no_spaces == pdf_rev_no_spaces or email_rev_no_spaces == pdf_no_spaces or
            email_rev_no_spaces == pdf_rev_no_spaces):
            return True
        
        # NEW: Handle cases where one name is just a single word (last name only)
        # e.g., "Kuttappa" should match "Varun Kuttappa"
        if email_norm['full_normalized'] and pdf_norm['full_normalized']:
            email_words = email_norm['full_normalized'].split()
            pdf_words = pdf_norm['full_normalized'].split()
            
            # If one name is a single word and it matches the last word of the other
            if len(email_words) == 1 and len(pdf_words) > 1:
                if email_words[0] == pdf_words[-1]:  # Last name match
                    return True
            elif len(pdf_words) == 1 and len(email_words) > 1:
                if pdf_words[0] == email_words[-1]:  # Last name match
                    return True
        
        # NEW: Handle initials - more lenient matching
        # If first name matches and initial matches any name part, consider it a match
        if email_norm['initials'] or pdf_norm['initials']:
            email_first_letter = email_norm['first_name'][0] if email_norm['first_name'] else ''
            pdf_first_letter = pdf_norm['first_name'][0] if pdf_norm['first_name'] else ''
            
            # Check if initial matches first letter of first name
            if email_norm['initials'] and pdf_first_letter:
                if any(init.lower() == pdf_first_letter.lower() for init in email_norm['initials']):
                    # Initial matches first name - check if any other part matches (last name, middle name, etc.)
                    if email_norm['last_name'] and pdf_norm['last_name']:
                        if email_norm['last_name'] == pdf_norm['last_name']:
                            return True
                        # Fuzzy match on last names
                        try:
                            if fuzz.ratio(email_norm['last_name'], pdf_norm['last_name']) >= 60:  # More lenient
                                return True
                        except:
                            pass
                    
                    # Also check if initial matches any other name part (middle name, etc.)
                    for pdf_part in pdf_parts_set:
                        if pdf_part and pdf_part[0].lower() == pdf_first_letter.lower():
                            # Initial matches a name part - likely the same person
                            return True
            
            if pdf_norm['initials'] and email_first_letter:
                if any(init.lower() == email_first_letter.lower() for init in pdf_norm['initials']):
                    # Initial matches first name - check if any other part matches
                    if email_norm['last_name'] and pdf_norm['last_name']:
                        if email_norm['last_name'] == pdf_norm['last_name']:
                            return True
                        # Fuzzy match on last names
                        try:
                            if fuzz.ratio(email_norm['last_name'], pdf_norm['last_name']) >= 60:  # More lenient
                                return True
                        except:
                            pass
                    
                    # Also check if initial matches any other name part
                    for email_part in email_parts_set:
                        if email_part and email_part[0].lower() == email_first_letter.lower():
                            # Initial matches a name part - likely the same person
                            return True
            
            # NEW: Check if initials match any name parts (not just first names)
            # e.g., "John K" vs "John Kumar" where K matches Kumar
            for email_initial in email_norm.get('initials', []):
                for pdf_part in pdf_parts_set:
                    if pdf_part and pdf_part[0].lower() == email_initial.lower():
                        # Initial matches a name part - check if first names match or any other part matches
                        if email_norm['first_name'] and pdf_norm['first_name']:
                            if email_norm['first_name'] == pdf_norm['first_name']:
                                return True
                        # Check if any other part matches
                        other_email = email_parts_set - {email_norm.get('first_name', '')}
                        other_pdf = pdf_parts_set - {pdf_part}
                        if other_email & other_pdf:
                            return True
            
            for pdf_initial in pdf_norm.get('initials', []):
                for email_part in email_parts_set:
                    if email_part and email_part[0].lower() == pdf_initial.lower():
                        # Initial matches a name part - check if first names match or any other part matches
                        if email_norm['first_name'] and pdf_norm['first_name']:
                            if email_norm['first_name'] == pdf_norm['first_name']:
                                return True
                        # Check if any other part matches
                        other_email = email_parts_set - {email_part}
                        other_pdf = pdf_parts_set - {pdf_norm.get('first_name', '')}
                        if other_email & other_pdf:
                            return True
        
        # If both have first and last names, check if they match (try both orderings)
        if email_norm['first_name'] and email_norm['last_name'] and pdf_norm['first_name'] and pdf_norm['last_name']:
            # First name and last name both match (normal order)
            if email_norm['first_name'] == pdf_norm['first_name'] and email_norm['last_name'] == pdf_norm['last_name']:
                return True
            # Check reversed order: email first=pdf last and email last=pdf first
            # This handles: "Mr Kuttappa Varun" (first=kuttappa, last=varun) vs "Varun Kuttappa" (first=varun, last=kuttappa)
            if email_norm['first_name'] == pdf_norm['last_name'] and email_norm['last_name'] == pdf_norm['first_name']:
                return True
            # First name matches and last name is similar (handles typos) - STRICTER THRESHOLD
            if email_norm['first_name'] == pdf_norm['first_name']:
                try:
                    last_name_similarity = fuzz.ratio(email_norm['last_name'], pdf_norm['last_name'])
                    if last_name_similarity >= 90:  # INCREASED from 75 to 90 - last names must be very similar
                        return True
                except:
                    pass
            # Check if first name matches last name (reversed order) with fuzzy matching
            if email_norm['first_name'] == pdf_norm['last_name']:
                try:
                    last_name_similarity = fuzz.ratio(email_norm['last_name'], pdf_norm['first_name'])
                    if last_name_similarity >= 90:  # INCREASED from 75 to 90
                        return True
                except:
                    pass
        
        # NEW: Handle cases where only one has first name but both have last names
        # e.g., "Kuttappa" (no first name) vs "Varun Kuttappa" (both)
        if email_norm['last_name'] and pdf_norm['last_name']:
            if email_norm['last_name'] == pdf_norm['last_name']:
                # Last names match exactly
                # If one has first name and other doesn't, still match (last name is key identifier)
                if (email_norm['first_name'] and not pdf_norm['first_name']) or (pdf_norm['first_name'] and not email_norm['first_name']):
                    return True
                # If both have first names, check if they're similar
                if email_norm['first_name'] and pdf_norm['first_name']:
                    try:
                        first_name_similarity = fuzz.ratio(email_norm['first_name'], pdf_norm['first_name'])
                        if first_name_similarity >= 60:  # More lenient for first names
                            return True
                    except:
                        pass
            # Fuzzy match on last names (handles typos) - STRICTER THRESHOLD
            try:
                last_name_similarity = fuzz.ratio(email_norm['last_name'], pdf_norm['last_name'])
                if last_name_similarity >= 90:  # INCREASED from 80 to 90 - last names must be very similar
                    # If last names are very similar, check first names if both exist
                    if email_norm['first_name'] and pdf_norm['first_name']:
                        first_name_similarity = fuzz.ratio(email_norm['first_name'], pdf_norm['first_name'])
                        if first_name_similarity >= 80:  # INCREASED from 70 to 80
                            return True
                    # If only one has first name, still match (last name similarity is key)
                    elif (email_norm['first_name'] and not pdf_norm['first_name']) or (pdf_norm['first_name'] and not email_norm['first_name']):
                        return True
            except:
                pass
        
        # Check if significant name parts overlap (try both normal and reversed orderings)
        email_parts = set(email_norm['name_parts'])
        pdf_parts = set(pdf_norm['name_parts'])
        
        # NEW: Handle space differences in parts (e.g., "anilkumar" vs "anil kumar")
        # Create versions without spaces for comparison
        email_parts_no_spaces = {p.replace(' ', '') for p in email_parts}
        pdf_parts_no_spaces = {p.replace(' ', '') for p in pdf_parts}
        
        # Check if parts match with spaces removed
        if email_parts_no_spaces & pdf_parts_no_spaces:
            # At least one part matches when spaces are removed
            # If we have matching parts, check if other parts also match
            if len(email_parts_no_spaces & pdf_parts_no_spaces) >= 1:
                # Check if we have enough matching parts
                if len(email_parts) >= 2 and len(pdf_parts) >= 2:
                    # If at least 2 parts match (with spaces removed), consider it a match
                    if len(email_parts_no_spaces & pdf_parts_no_spaces) >= 2:
                        return True
                    # Or if last names match (with spaces removed)
                    if email_norm['last_name'] and pdf_norm['last_name']:
                        email_last_no_spaces = email_norm['last_name'].replace(' ', '')
                        pdf_last_no_spaces = pdf_norm['last_name'].replace(' ', '')
                        if email_last_no_spaces == pdf_last_no_spaces:
                            # Last names match, check first names
                            if email_norm['first_name'] and pdf_norm['first_name']:
                                email_first_no_spaces = email_norm['first_name'].replace(' ', '')
                                pdf_first_no_spaces = pdf_norm['first_name'].replace(' ', '')
                                if email_first_no_spaces == pdf_first_no_spaces or fuzz.ratio(email_first_no_spaces, pdf_first_no_spaces) >= 80:
                                    return True
                            else:
                                # Last names match, one has first name - still match
                                return True
        
        # NEW: Handle single-part names (just last name)
        if len(email_parts) == 1 and len(pdf_parts) >= 1:
            if email_parts.issubset(pdf_parts):
                return True
            # Also check with spaces removed
            if email_parts_no_spaces.issubset(pdf_parts_no_spaces):
                return True
        if len(pdf_parts) == 1 and len(email_parts) >= 1:
            if pdf_parts.issubset(email_parts):
                return True
            # Also check with spaces removed
            if pdf_parts_no_spaces.issubset(email_parts_no_spaces):
                return True
        
        # If both have at least 1 part, check overlap (changed from 2 to 1)
        if len(email_parts) >= 1 and len(pdf_parts) >= 1:
            # Check if all parts match (normal order) - handles exact matches
            if email_parts == pdf_parts:
                return True
            
            # NEW: Check if one set is a subset of the other - STRICTER
            if email_parts.issubset(pdf_parts) or pdf_parts.issubset(email_parts):
                # FIX: Require at least 2 parts to match, AND if we have last names, they must match
                matching_parts = email_parts & pdf_parts
                if len(matching_parts) >= 2:
                    # Additional check: if we have last names, they must be in the matching parts
                    if email_norm['last_name'] and pdf_norm['last_name']:
                        if email_norm['last_name'] in matching_parts and pdf_norm['last_name'] in matching_parts:
                            return True
                    else:
                        # No last names, but 2+ parts match - accept
                        return True
            
            # Check if first name matches and at least one other part matches (normal order)
            if email_norm['first_name'] and pdf_norm['first_name']:
                if email_norm['first_name'] == pdf_norm['first_name']:
                    # Check if any other part matches (could be last name or middle name)
                    other_email_parts = email_parts - {email_norm['first_name']}
                    other_pdf_parts = pdf_parts - {pdf_norm['first_name']}
                    if other_email_parts and other_pdf_parts:
                        # Check for exact match in other parts
                        if other_email_parts & other_pdf_parts:
                            return True
                        # Check for fuzzy match in other parts - STRICTER THRESHOLD
                        for email_part in other_email_parts:
                            for pdf_part in other_pdf_parts:
                                try:
                                    similarity = fuzz.ratio(email_part, pdf_part)
                                    # FIX: Require very high similarity for last names, moderate for other parts
                                    # If either part is likely a last name (longer, at end), require 90%+
                                    if len(email_part) >= 6 or len(pdf_part) >= 6:  # Likely last name
                                        if similarity >= 90:
                                            return True
                                    else:  # Other parts (middle names, etc.)
                                        if similarity >= 85:  # INCREASED from 75 to 85
                                            return True
                                except:
                                    pass
            # NEW: If first names don't match exactly, try fuzzy matching - STRICTER
            elif email_norm['first_name'] and pdf_norm['first_name']:
                try:
                    first_name_similarity = fuzz.ratio(email_norm['first_name'], pdf_norm['first_name'])
                    if first_name_similarity >= 85:  # INCREASED from 75 to 85
                        # If first names are similar, check if last names match EXACTLY or very closely
                        if email_norm['last_name'] and pdf_norm['last_name']:
                            if email_norm['last_name'] == pdf_norm['last_name']:
                                return True
                            last_name_similarity = fuzz.ratio(email_norm['last_name'], pdf_norm['last_name'])
                            if last_name_similarity >= 90:  # INCREASED from 75 to 90
                                return True
                except:
                    pass
            
            # Check reversed order: email first matches pdf last (or vice versa)
            # This handles: "Mr PATIL NIKHIL" (first=patil, last=nikhil) vs "Nikhil Patil" (first=nikhil, last=patil)
            if email_norm['first_name'] and pdf_norm['last_name']:
                if email_norm['first_name'] == pdf_norm['last_name']:
                    # Check if email last matches pdf first (reversed order match)
                    if email_norm['last_name'] == pdf_norm['first_name']:
                        return True
                    # Check if any other parts match
                    other_email_parts = email_parts - {email_norm['first_name'], email_norm['last_name']}
                    other_pdf_parts = pdf_parts - {pdf_norm['first_name'], pdf_norm['last_name']}
                    if other_email_parts and other_pdf_parts:
                        if other_email_parts & other_pdf_parts:
                            return True
            
            # Also check the reverse: email last matches pdf first
            if email_norm['last_name'] and pdf_norm['first_name']:
                if email_norm['last_name'] == pdf_norm['first_name']:
                    # Check if email first matches pdf last (reversed order match)
                    if email_norm['first_name'] == pdf_norm['last_name']:
                        return True
        
        # Use fuzzy matching on full normalized names (try both normal and reversed orderings)
        # INCREASED THRESHOLDS to prevent false matches
        try:
            # Normal order
            similarity = fuzz.ratio(email_norm['full_normalized'], pdf_norm['full_normalized'])
            # FIX: Require BOTH high similarity AND last name match for fuzzy matching
            if similarity >= 80:  # Increased from 70 to 80
                # Additional check: if we have last names, they must match
                if email_norm['last_name'] and pdf_norm['last_name']:
                    if email_norm['last_name'] == pdf_norm['last_name'] or fuzz.ratio(email_norm['last_name'], pdf_norm['last_name']) >= 90:
                        return True
                else:
                    # No last names, but high similarity - accept
                    return True
            # Reversed order (one name reversed)
            similarity_rev1 = fuzz.ratio(email_norm['full_normalized'], pdf_norm.get('full_normalized_reversed', ''))
            if similarity_rev1 >= 80:  # Increased from 70 to 80
                if email_norm['last_name'] and pdf_norm['last_name']:
                    if email_norm['last_name'] == pdf_norm['last_name'] or fuzz.ratio(email_norm['last_name'], pdf_norm['last_name']) >= 90:
                        return True
                else:
                    return True
            # Reversed order (other name reversed)
            similarity_rev2 = fuzz.ratio(email_norm.get('full_normalized_reversed', ''), pdf_norm['full_normalized'])
            if similarity_rev2 >= 80:  # Increased from 70 to 80
                if email_norm['last_name'] and pdf_norm['last_name']:
                    if email_norm['last_name'] == pdf_norm['last_name'] or fuzz.ratio(email_norm['last_name'], pdf_norm['last_name']) >= 90:
                        return True
                else:
                    return True
            # Both reversed
            similarity_rev3 = fuzz.ratio(email_norm.get('full_normalized_reversed', ''), pdf_norm.get('full_normalized_reversed', ''))
            if similarity_rev3 >= 80:  # Increased from 70 to 80
                if email_norm['last_name'] and pdf_norm['last_name']:
                    if email_norm['last_name'] == pdf_norm['last_name'] or fuzz.ratio(email_norm['last_name'], pdf_norm['last_name']) >= 90:
                        return True
                else:
                    return True
            
            # NEW: Try fuzzy matching with spaces removed (handles "ANILKUMAR" vs "ANIL KUMAR")
            email_no_spaces = email_norm['full_normalized'].replace(' ', '')
            pdf_no_spaces = pdf_norm['full_normalized'].replace(' ', '')
            if email_no_spaces and pdf_no_spaces:
                similarity_no_spaces = fuzz.ratio(email_no_spaces, pdf_no_spaces)
                if similarity_no_spaces >= 85:  # Higher threshold since we removed spaces
                    return True
                # Also try with one reversed
                email_rev_no_spaces = email_norm.get('full_normalized_reversed', '').replace(' ', '')
                pdf_rev_no_spaces = pdf_norm.get('full_normalized_reversed', '').replace(' ', '')
                if email_rev_no_spaces and pdf_no_spaces:
                    similarity_rev_no_spaces = fuzz.ratio(email_rev_no_spaces, pdf_no_spaces)
                    if similarity_rev_no_spaces >= 85:
                        return True
                if email_no_spaces and pdf_rev_no_spaces:
                    similarity_rev_no_spaces2 = fuzz.ratio(email_no_spaces, pdf_rev_no_spaces)
                    if similarity_rev_no_spaces2 >= 85:
                        return True
            
            # NEW: Try partial ratio - STRICTER with last name check
            if email_norm['full_normalized'] and pdf_norm['full_normalized']:
                partial_ratio = fuzz.partial_ratio(email_norm['full_normalized'], pdf_norm['full_normalized'])
                if partial_ratio >= 85:  # INCREASED from 75 to 85
                    # Require last name match for partial ratio
                    if email_norm['last_name'] and pdf_norm['last_name']:
                        if email_norm['last_name'] == pdf_norm['last_name'] or fuzz.ratio(email_norm['last_name'], pdf_norm['last_name']) >= 90:
                            return True
                    else:
                        return True
                # Try token sort ratio (handles word order differences) - STRICTER
                token_sort_ratio = fuzz.token_sort_ratio(email_norm['full_normalized'], pdf_norm['full_normalized'])
                if token_sort_ratio >= 85:  # INCREASED from 75 to 85
                    # Require last name match for token sort ratio
                    if email_norm['last_name'] and pdf_norm['last_name']:
                        if email_norm['last_name'] == pdf_norm['last_name'] or fuzz.ratio(email_norm['last_name'], pdf_norm['last_name']) >= 90:
                            return True
                    else:
                        return True
        except:
            pass
        
        # FIX: Substring matching is too lenient - require last name match
        # Check if one normalized name contains the other (for partial matches, try both orderings)
        if email_norm['full_normalized'] and pdf_norm['full_normalized']:
            # Only allow substring matching if last names match
            if email_norm['last_name'] and pdf_norm['last_name']:
                if email_norm['last_name'] == pdf_norm['last_name'] or fuzz.ratio(email_norm['last_name'], pdf_norm['last_name']) >= 90:
                    # Normal order
                    if (email_norm['full_normalized'] in pdf_norm['full_normalized'] or 
                        pdf_norm['full_normalized'] in email_norm['full_normalized']):
                        return True
                    # Reversed order
                    if (email_norm['full_normalized'] in pdf_norm.get('full_normalized_reversed', '') or 
                        pdf_norm.get('full_normalized_reversed', '') in email_norm['full_normalized'] or
                        email_norm.get('full_normalized_reversed', '') in pdf_norm['full_normalized'] or
                        pdf_norm['full_normalized'] in email_norm.get('full_normalized_reversed', '')):
                        return True
        
        # Check if first name matches and one name is a subset of the other
        # (e.g., "Varun K" vs "Varun Madhav Kuttappa" or "Varun Kuttappa" vs "Varun Madhav Kuttappa")
        # Also handle reversed orderings: "Mr Kuttappa Varun" vs "Varun Kuttappa"
        if email_norm['first_name'] and pdf_norm['first_name']:
            # Check normal order: first names match
            if email_norm['first_name'] == pdf_norm['first_name']:
                # Check if all parts of shorter name are in longer name
                shorter_parts_set = set(email_norm['name_parts']) if len(email_norm['name_parts']) <= len(pdf_norm['name_parts']) else set(pdf_norm['name_parts'])
                longer_parts_set = set(pdf_norm['name_parts']) if len(email_norm['name_parts']) <= len(pdf_norm['name_parts']) else set(email_norm['name_parts'])
                
                # If shorter name is a subset of longer name, they match
                # This handles: "Varun Kuttappa" (2 parts) vs "Varun Madhav Kuttappa" (3 parts)
                # CHANGED: Now requires only 1 part match (more lenient)
                if shorter_parts_set.issubset(longer_parts_set) and len(shorter_parts_set) >= 1:
                    return True
                
                # Special case: If shorter name has only first name after filtering (e.g., "Varun" from "Varun K")
                # Match if first names match (we're already in the block where first names match)
                # This handles: "Varun K" (becomes "varun") vs "Varun Madhav Kuttappa" (becomes "varun madhav kuttappa")
                if len(shorter_parts_set) == 1 and len(longer_parts_set) >= 2:
                    # First names already match (we're in that block), and shorter name is just first name
                    # This is likely the same person with abbreviated name
                    # e.g., "Varun K" vs "Varun Madhav Kuttappa" - both have "varun" as first name
                    return True
                
                # NEW: Check if last names match even if middle names differ
                if email_norm['last_name'] and pdf_norm['last_name']:
                    if email_norm['last_name'] == pdf_norm['last_name']:
                        return True
                    # Fuzzy match on last names
                    try:
                        if fuzz.ratio(email_norm['last_name'], pdf_norm['last_name']) >= 60:
                            return True
                    except:
                        pass
            
            # Check reversed order: email first name matches pdf last name (or vice versa)
            # This handles: "Mr Kuttappa Varun" (first=kuttappa) vs "Varun Kuttappa" (first=varun, last=kuttappa)
            if (email_norm['first_name'] == pdf_norm['last_name'] or 
                email_norm['last_name'] == pdf_norm['first_name']):
                # Check if all parts match when reversed
                shorter_parts_set = set(email_norm['name_parts']) if len(email_norm['name_parts']) <= len(pdf_norm['name_parts']) else set(pdf_norm['name_parts'])
                longer_parts_set = set(pdf_norm['name_parts']) if len(email_norm['name_parts']) <= len(pdf_norm['name_parts']) else set(email_norm['name_parts'])
                
                # If shorter name is a subset of longer name (when reversed), they match
                if shorter_parts_set.issubset(longer_parts_set) and len(shorter_parts_set) >= 2:
                    return True
                
                # Also check if last names match (even if middle names differ)
                # Try both normal and reversed orderings
                if email_norm['last_name'] and pdf_norm['last_name']:
                    # Normal order: last names match
                    if email_norm['last_name'] == pdf_norm['last_name']:
                        return True
                    # Reversed order: email last name matches pdf first name (or vice versa)
                    if email_norm['last_name'] == pdf_norm['first_name'] or email_norm['first_name'] == pdf_norm['last_name']:
                        return True
                    # Fuzzy match on last names if first names match (normal order) - LOWERED THRESHOLD
                    try:
                        last_name_similarity = fuzz.ratio(email_norm['last_name'], pdf_norm['last_name'])
                        if last_name_similarity >= 60:  # Lowered from 85 to 60
                            return True
                    except:
                        pass
                    # Fuzzy match on reversed order (email last vs pdf first, or email first vs pdf last)
                    try:
                        if email_norm['last_name'] == pdf_norm['first_name']:
                            # Email last matches PDF first, check if email first matches PDF last
                            first_last_similarity = fuzz.ratio(email_norm['first_name'], pdf_norm['last_name'])
                            if first_last_similarity >= 60:  # Lowered from 85 to 60
                                return True
                    except:
                        pass
        
        # NEW: Final catch-all - check if any significant word from one name appears in the other
        # This handles cases like "John" vs "John Smith" or "Smith" vs "John Smith"
        if email_norm['name_parts'] and pdf_norm['name_parts']:
            email_words = set(email_norm['name_parts'])
            pdf_words = set(pdf_norm['name_parts'])
            
            # If at least 2 words match, consider it a match (MORE LENIENT)
            common_words = email_words & pdf_words
            if len(common_words) >= 2:
                return True
            
            # NEW: If first name matches and at least one other part matches (could be last name, middle name, or initial)
            if email_norm['first_name'] and pdf_norm['first_name']:
                if email_norm['first_name'] == pdf_norm['first_name']:
                    # First names match - check if any other part matches
                    other_email = email_words - {email_norm['first_name']}
                    other_pdf = pdf_words - {pdf_norm['first_name']}
                    if other_email & other_pdf:
                        # At least one other part matches
                        return True
                    
                    # Also check if initials match other parts
                    email_initials = email_norm.get('initials', [])
                    pdf_initials = pdf_norm.get('initials', [])
                    
                    # Check if email initials match PDF name parts
                    for initial in email_initials:
                        for pdf_part in other_pdf:
                            if pdf_part and pdf_part[0].lower() == initial.lower():
                                return True
                    
                    # Check if PDF initials match email name parts
                    for initial in pdf_initials:
                        for email_part in other_email:
                            if email_part and email_part[0].lower() == initial.lower():
                                return True
            
            # If 1 word matches and it's a last name, also match (last names are more unique)
            if len(common_words) == 1:
                common_word = list(common_words)[0]
                # Check if it's a last name in either
                if (email_norm['last_name'] == common_word or pdf_norm['last_name'] == common_word):
                    # Additional check: if first names are similar or one is missing
                    if (not email_norm['first_name'] or not pdf_norm['first_name'] or
                        email_norm['first_name'] == pdf_norm['first_name'] or
                        (email_norm['first_name'] and pdf_norm['first_name'] and 
                         fuzz.ratio(email_norm['first_name'], pdf_norm['first_name']) >= 60)):
                        return True
                    
                    # Also check if initials match first names
                    email_initials = email_norm.get('initials', [])
                    pdf_initials = pdf_norm.get('initials', [])
                    
                    if email_norm['first_name'] and pdf_initials:
                        if email_norm['first_name'][0].lower() in [i.lower() for i in pdf_initials]:
                            return True
                    
                    if pdf_norm['first_name'] and email_initials:
                        if pdf_norm['first_name'][0].lower() in [i.lower() for i in email_initials]:
                            return True
        
        return False
    
    # More lenient matching function for fallback matching of unmatched email entries
    def guest_names_match_lenient(email_guest, pdf_guest):
        """More lenient matching for unmatched email entries - lower thresholds, more flexible"""
        if not email_guest or not pdf_guest:
            return False
        
        # Handle multiple names
        email_names = split_multiple_names(email_guest)
        pdf_names = split_multiple_names(pdf_guest)
        
        if len(email_names) > 1 or len(pdf_names) > 1:
            for email_name in email_names:
                for pdf_name in pdf_names:
                    if guest_names_match_lenient_single(email_name, pdf_name):
                        return True
            return False
        
        return guest_names_match_lenient_single(email_guest, pdf_guest)
    
    def guest_names_match_lenient_single(email_guest, pdf_guest):
        """Lenient single name matching - lower thresholds for fallback matching"""
        if not email_guest or not pdf_guest:
            return False
        
        # Normalize both names
        email_norm = normalize_name_for_matching(email_guest)
        pdf_norm = normalize_name_for_matching(pdf_guest)
        
        if not email_norm['full_normalized'] and not pdf_norm['full_normalized']:
            return False
        
        # LENIENT: Lower threshold for last name similarity (70% instead of 90%)
        if email_norm['last_name'] and pdf_norm['last_name']:
            try:
                last_name_similarity = fuzz.ratio(email_norm['last_name'], pdf_norm['last_name'])
                # If last names are very different (< 70%), still reject
                if last_name_similarity < 70:
                    return False
            except:
                pass
        
        # LENIENT: Lower thresholds for fuzzy matching
        try:
            # Full name similarity - lower threshold (60% instead of 80%)
            similarity = fuzz.ratio(email_norm['full_normalized'], pdf_norm['full_normalized'])
            if similarity >= 60:
                # If we have last names, they should still match reasonably (70%+)
                if email_norm['last_name'] and pdf_norm['last_name']:
                    last_sim = fuzz.ratio(email_norm['last_name'], pdf_norm['last_name'])
                    if last_sim >= 60:
                        return True
                else:
                    return True
            
            # Reversed order
            similarity_rev = fuzz.ratio(email_norm.get('full_normalized_reversed', ''), pdf_norm['full_normalized'])
            if similarity_rev >= 60:
                if email_norm['last_name'] and pdf_norm['last_name']:
                    last_sim = fuzz.ratio(email_norm['last_name'], pdf_norm['last_name'])
                    if last_sim >= 60:
                        return True
                else:
                    return True
            
            # Partial ratio - lower threshold (65% instead of 85%)
            partial_ratio = fuzz.partial_ratio(email_norm['full_normalized'], pdf_norm['full_normalized'])
            if partial_ratio >= 65:
                if email_norm['last_name'] and pdf_norm['last_name']:
                    last_sim = fuzz.ratio(email_norm['last_name'], pdf_norm['last_name'])
                    if last_sim >= 60:
                        return True
                else:
                    return True
            
            # Token sort ratio - lower threshold (65% instead of 85%)
            token_sort_ratio = fuzz.token_sort_ratio(email_norm['full_normalized'], pdf_norm['full_normalized'])
            if token_sort_ratio >= 65:
                if email_norm['last_name'] and pdf_norm['last_name']:
                    last_sim = fuzz.ratio(email_norm['last_name'], pdf_norm['last_name'])
                    if last_sim >= 60:
                        return True
                else:
                    return True
        except:
            pass
        
        # LENIENT: Check if first names match and last names are similar (70%+)
        if email_norm['first_name'] and pdf_norm['first_name']:
            if email_norm['first_name'] == pdf_norm['first_name']:
                if email_norm['last_name'] and pdf_norm['last_name']:
                    try:
                        last_sim = fuzz.ratio(email_norm['last_name'], pdf_norm['last_name'])
                        if last_sim >= 60:  # Lower threshold
                            return True
                    except:
                        pass
        
        # LENIENT: Check if last names match and first names are similar (60%+)
        if email_norm['last_name'] and pdf_norm['last_name']:
            if email_norm['last_name'] == pdf_norm['last_name']:
                if email_norm['first_name'] and pdf_norm['first_name']:
                    try:
                        first_sim = fuzz.ratio(email_norm['first_name'], pdf_norm['first_name'])
                        if first_sim >= 60:  # Lower threshold
                            return True
                    except:
                        pass
                else:
                    # Last names match, one missing first name - accept
                    return True
        
        # LENIENT: Check name parts overlap (at least 1 part match)
        if email_norm['name_parts'] and pdf_norm['name_parts']:
            email_parts = set(email_norm['name_parts'])
            pdf_parts = set(pdf_norm['name_parts'])
            if email_parts & pdf_parts:  # At least one part matches
                return True
        
        return False

    # ------------------------------------------------------------------
    # OVERRIDE (user request):
    # Keep ONLY "remove title" normalization, and use RapidFuzz scoring with
    # threshold > 60 for guest-name matching (replaces the complex logic above).
    # ------------------------------------------------------------------
    def _remove_titles_only(value: str) -> str:
        if not value:
            return ""
        text_value = normalize_field_text(value).strip()
        if not text_value:
            return ""
        titles = {"mr", "mrs", "ms", "miss", "dr", "prof", "sir", "madam"}
        tokens = text_value.split()
        while tokens:
            first = tokens[0].strip()
            first_key = first.rstrip(".").lower()
            if first_key in titles:
                tokens.pop(0)
                continue
            break
        return " ".join(tokens).strip()

    def guest_names_match(email_guest, pdf_guest, threshold: int = 70) -> bool:
        if not email_guest or not pdf_guest:
            return False
        a = _remove_titles_only(email_guest).lower()
        b = _remove_titles_only(pdf_guest).lower()
        if not a or not b:
            return False
        score = max(
            fuzz.ratio(a, b),
            fuzz.partial_ratio(a, b),
            fuzz.token_sort_ratio(a, b),
            fuzz.token_set_ratio(a, b),
        )
        return score > threshold

    def guest_names_match_lenient(email_guest, pdf_guest) -> bool:
        # Per user request: keep threshold above 60.
        return guest_names_match(email_guest, pdf_guest, threshold=70)
    
    # Helper function to merge email and PDF data
    def merge_email_pdf_data(email_data, pdf_data, booking_code=None):
        """Merge email and PDF data - booking_code and property_name come from email primarily, PDF fills if email is missing, all other fields from PDF only"""
        
        def is_valid_field_value(value):
            """Check if a field value is valid (not empty, not 'Not Found', not placeholder)"""
            if not value or value is None:
                return False
            value_str = str(value).strip()
            if not value_str:
                return False
            value_lower = value_str.lower()
            # Check for invalid/placeholder values
            invalid_values = ['', 'not found', 'n/a', 'na', 'none', 'value']
            if value_lower in invalid_values:
                return False
            # Check if value looks like another field name (common GPT parsing error)
            invalid_patterns = [
                r'^(?:Guest|Hotel|HBD|Client|Bill|Check)\s+(?:Name|Date|Number|GST|IN|OUT)\s*=',
                r'^Check\s+(?:IN|OUT)\s*=',
                r'^Amount\s*=',
            ]
            for pattern in invalid_patterns:
                if re.match(pattern, value_str, re.IGNORECASE):
                    return False  # This is another field name, not the actual value
            return True
        
        merged = pdf_data.copy()  # Start with PDF data - all fields come from PDF
        
        # Merge booking_code from email (if PDF doesn't have it or PDF has invalid value)
        email_booking_code = email_data.get('booking_code') or booking_code
        
        # Add booking code from email (only if valid and PDF doesn't have it, or PDF has invalid value)
        if is_valid_field_value(email_booking_code):
            pdf_booking_code = merged.get('booking_code', '')
            if not is_valid_field_value(pdf_booking_code):
                merged['booking_code'] = normalize_field_text(email_booking_code)
        
        # Merge property_name - PRIMARY from email, PDF fills if email is missing
        # property_name can be in 'property_name' or 'property_context' field
        email_property_name = email_data.get('property_name') or email_data.get('property_context')
        pdf_property_name = merged.get('hotel_name', '')
        
        # Use email property name primarily, PDF only if email is missing/invalid
        if is_valid_field_value(email_property_name):
            # Email has valid property name - use it
            merged['hotel_name'] = normalize_field_text(email_property_name)
        elif is_valid_field_value(pdf_property_name):
            # Email doesn't have it, but PDF does - use PDF
            # hotel_name already comes from pdf_data.copy(), so no change needed
            pass
        
        # All other fields remain as extracted from PDF (or 'Not Found' if not in PDF)
        # No email data is merged for: guest_name, check_in_date, check_out_date, total_amount, etc.
        # PDF-specific fields (bill_number, bill_date, GST numbers) are already PDF-only
        
        return merged
    
    # Analyze PDF attachments using invoice2.py logic
    pdf_analysis = {}
    
    # Helper function to validate PDF guest names (used in both first and second pass)
    def is_valid_guest_name_local(name):
        """Check if name looks like a valid guest name (not a hotel name)"""
        if not name or name == 'Not Found':
            return False
        name_lower = name.lower().strip()
        # Check if it contains hotel-related keywords
        hotel_keywords = ['hotel', 'resort', 'inn', 'lodge', 'villa', 'apartment', 'stay']
        if any(keyword in name_lower for keyword in hotel_keywords):
            return False
        # Check if it's too short or looks like a code
        if len(name_lower) < 3:
            return False
        # Check if it's mostly numbers or special chars
        if re.search(r'^[^a-zA-Z]*$', name_lower):
            return False
        return True
    
    if attachments and len(attachments) > 0:
        print(f"   📎 Found {len(attachments)} attachment(s)")
        valid_attachments = [a for a in attachments if isinstance(a, dict)]
        
        if not valid_attachments:
            print(f"   ⚠️  No valid attachments found (all are non-dictionary types)")
        else:
            print(f"   📎 Processing {len(valid_attachments)} valid attachment(s)")
            pdf_count = sum(1 for a in valid_attachments if a.get('content_type') == 'application/pdf' and a.get('payload'))
            if pdf_count > 0:
                print(f"   📄 Found {pdf_count} PDF attachment(s) for analysis")
            else:
                print(f"   ⚠️  No PDF attachments found (checking content_type and payload)")
            
            # Check for Azure OpenAI credentials first, then fallback to regular OpenAI
            azure_api_key = os.getenv('AZURE_OPENAI_API_KEY')
            azure_base_url = os.getenv('AZURE_OPENAI_BASE_URL')
            openai_api_key = os.getenv('OPENAI_API_KEY')
            
            # Determine which API key to use
            if azure_api_key and azure_base_url:
                api_key_to_use = azure_api_key
            elif openai_api_key:
                api_key_to_use = openai_api_key
            else:
                api_key_to_use = None
            
            for i, attachment in enumerate(valid_attachments):
                if attachment.get('content_type') == 'application/pdf' and attachment.get('payload'):
                    filename = attachment.get('filename', f'pdf_{i+1}')
                    pdf_bytes = attachment.get('payload')
                    try:
                        print(f"   📄 Analyzing PDF: {filename}")
                    
                        ocr_text, structured_payload, pdf_page_count = process_pdf_bytes(
                            pdf_bytes, filename, api_key_to_use
                        )
                        if isinstance(structured_payload, list):
                            structured_items = structured_payload
                        elif structured_payload is not None:
                            structured_items = [structured_payload]
                        else:
                            structured_items = []

                        attachment_slot = i + 1
                        num_extracted = len(structured_items)
                        data_sequence = structured_items if structured_items else [None]

                        for inv_idx, structured_data in enumerate(data_sequence):
                            multi_inv = num_extracted > 1
                            eff_filename = filename
                            pdf_key = f"pdf_{attachment_slot}"
                            if multi_inv:
                                stem, ext = os.path.splitext(filename)
                                eff_filename = f"{stem}_inv{inv_idx + 1}{ext}"
                                pdf_key = f"pdf_{attachment_slot}_inv{inv_idx + 1}"

                            pdf_data = {
                                "filename": eff_filename,
                                "source_pdf_filename": filename,
                                "payload": pdf_bytes,
                                "ocr_text": ocr_text,
                                "page_count": pdf_page_count,
                                "source_attachment_slot": attachment_slot,
                                "invoice_segment_in_pdf": (inv_idx + 1) if multi_inv else None,
                            }

                            # Carry invoice-type classification (Prompt A / Prompt B) into pdf_data
                            # so update_excel_sheet can set MySQL flags after insert.
                            if isinstance(structured_data, dict) and structured_data.get("_classification"):
                                pdf_data["_classification"] = structured_data.get("_classification")

                            if structured_data:
                                print(f"   ✅ Extracted structured data from {eff_filename}")
                        
                                # FIX: Normalize empty strings to 'Not Found' - GPT may return empty strings
                                def normalize_pdf_field(value, default='Not Found'):
                                    """Normalize PDF field - empty strings become 'Not Found', also check for invalid values"""
                                    if value is None:
                                        return default
                                    value_str = str(value).strip()
                                    if not value_str or value_str == '':
                                        return default
                                    # Check if value looks like another field (e.g., "Check IN = ...")
                                    # Common GPT errors where empty fields get next field's value
                                    invalid_patterns = [
                                        r'^(?:Guest|Hotel|HBD|Client|Bill|Check)\s+(?:Name|Date|Number|GST|IN|OUT)\s*=',
                                        r'^Check\s+(?:IN|OUT)\s*=',
                                        r'^Amount\s*=',
                                    ]
                                    for pattern in invalid_patterns:
                                        if re.match(pattern, value_str, re.IGNORECASE):
                                            return default  # This is another field, not the actual value
                                    # Check for placeholder values
                                    if value_str.lower() in ['value', 'n/a', 'na', 'none', 'not found', '']:
                                        return default
                                    return value_str
                        
                                # Normalize all fields
                                pdf_data['hotel_name'] = normalize_pdf_field(structured_data.get('hotel_name'), 'Not Found')
                                pdf_data['guest_name'] = normalize_pdf_field(structured_data.get('guest_name'), 'Not Found')
                                pdf_data['bill_number'] = normalize_pdf_field(structured_data.get('bill_number'), 'Not Found')
                                pdf_data['bill_date'] = normalize_pdf_field(structured_data.get('bill_date'), 'Not Found')
                                pdf_data['check_in_date'] = format_date_to_dd_mm_yy(normalize_pdf_field(structured_data.get('check_in'), 'Not Found'))
                                pdf_data['check_out_date'] = format_date_to_dd_mm_yy(normalize_pdf_field(structured_data.get('check_out'), 'Not Found'))
                                pdf_data['total_amount'] = normalize_pdf_field(structured_data.get('amount'), 'Not Found')
                                if all(pdf_data.get(_k) == 'Not Found' for _k in ('hotel_name', 'guest_name', 'bill_number', 'total_amount')):
                                    _log_pdf_failure(filename, subject, message_id, "all key fields Not Found (hotel, guest, bill number, amount)")
                        
                                # Extract CGST and SGST fields from GPT output
                                pdf_data['cgst_number'] = normalize_pdf_field(structured_data.get('cgst_number'), 'Not Found')
                                pdf_data['sgst_number'] = normalize_pdf_field(structured_data.get('sgst_number'), 'Not Found')
                                pdf_data['total_cgst'] = normalize_pdf_field(structured_data.get('total_cgst'), 'Not Found')
                                pdf_data['total_sgst'] = normalize_pdf_field(structured_data.get('total_sgst'), 'Not Found')

                                pdf_data['room_charge'] = normalize_pdf_field(structured_data.get('room_charge'), 'Not Found')
                                pdf_data['total_tariff'] = normalize_pdf_field(structured_data.get('total_tariff'), 'Not Found')
                                pdf_data['other_charges'] = normalize_pdf_field(structured_data.get('other_charges'), 'Not Found')
                                pdf_data['total_igst'] = normalize_pdf_field(structured_data.get('total_igst'), 'Not Found')
                                pdf_data['total_taxes'] = normalize_pdf_field(structured_data.get('total_taxes'), 'Not Found')
                                pdf_data['discount'] = normalize_pdf_field(structured_data.get('discount'), 'Not Found')
                                pdf_data['advance_received'] = normalize_pdf_field(structured_data.get('advance_received'), 'Not Found')
                        
                                # GST from GPT: roles defined in prompt (HBD = customer/Bill To; Client GST = hotel header).
                                # We validate GSTIN + optional OCR check only — no 5874 re-bucketing.
                                raw_hbd_gst = structured_data.get('hbd_gst', '')
                                raw_client_gst = structured_data.get('client_gst', '')

                                if not raw_hbd_gst or (isinstance(raw_hbd_gst, str) and raw_hbd_gst.strip() == ''):
                                    raw_hbd_gst = 'Not Found'
                                if not raw_client_gst or (isinstance(raw_client_gst, str) and raw_client_gst.strip() == ''):
                                    raw_client_gst = 'Not Found'

                                print(f"   🔍 Raw GST from GPT (spatial rules in prompt; not re-bucketed by 5874):")
                                print(f"      Raw HBD GST (customer/Bill To): '{raw_hbd_gst}'")
                                print(f"      Raw Client GST (hotel header): '{raw_client_gst}'")

                                ocr_for_gst = (ocr_text or pdf_data.get('ocr_text', '') or '')

                                def verify_gst_in_ocr(gst_number, ocr_t):
                                    if not gst_number or gst_number == 'Not Found' or not ocr_t:
                                        return True
                                    gst_clean = re.sub(r'[^A-Z0-9]', '', str(gst_number).upper())
                                    if len(gst_clean) < 10:
                                        return True
                                    ocr_no_spaces = re.sub(r'[^A-Z0-9]', '', ocr_t.upper())
                                    if gst_clean in ocr_no_spaces:
                                        return True
                                    for i in range(len(ocr_no_spaces) - len(gst_clean) + 1):
                                        window = ocr_no_spaces[i:i + len(gst_clean)]
                                        if len(window) == len(gst_clean):
                                            matches = sum(1 for a, b in zip(gst_clean, window) if a == b)
                                            if matches >= len(gst_clean) * 0.85:
                                                return True
                                    return False

                                def _process_gst_field_raw(raw_value, field_label, ocr_t):
                                    invalid_segments = []
                                    valids_ordered = []
                                    if not raw_value or raw_value == 'Not Found':
                                        return 'Not Found', invalid_segments
                                    parts = re.split(r'[\n;]+', str(raw_value))
                                    for g in [x.strip() for x in parts if x.strip()]:
                                        g_clean = re.sub(r'[^A-Z0-9]', '', g.upper())
                                        if not g_clean:
                                            continue
                                        if ocr_t and not verify_gst_in_ocr(g_clean, ocr_t):
                                            print(f"   ⚠️  HALLUCINATION DETECTED: {field_label} '{g_clean}' not in OCR — skipped")
                                            continue
                                        if is_valid_gstin(g_clean):
                                            valids_ordered.append(g_clean)
                                            print(f"   ✅ {field_label} '{g_clean}' passed GSTIN validation")
                                        else:
                                            if g_clean not in invalid_segments:
                                                invalid_segments.append(g_clean)
                                            print(f"   ⚠️  {field_label} '{g}' failed GSTIN validation")
                                    primary = valids_ordered[0] if valids_ordered else 'Not Found'
                                    return primary, invalid_segments

                                hbd_gst, invalid_hbd_segments = _process_gst_field_raw(raw_hbd_gst, 'HBD GST', ocr_for_gst)
                                client_gst, invalid_client_segments = _process_gst_field_raw(
                                    raw_client_gst, 'Client GST (hotel)', ocr_for_gst
                                )

                                if hbd_gst != 'Not Found' and client_gst != 'Not Found' and hbd_gst == client_gst:
                                    print(f"   ⚠️  HBD GST and Client GST (hotel) are identical after validation: {hbd_gst}")

                                def _merge_gst_column(valid_str, invalid_list):
                                    parts = []
                                    if valid_str and str(valid_str).strip() not in ('', 'Not Found'):
                                        for x in re.split(r'\s*;\s*', str(valid_str)):
                                            t = x.strip()
                                            if t:
                                                parts.append(t)
                                    for s in invalid_list:
                                        if s and s not in parts:
                                            parts.append(s)
                                    return ' ; '.join(parts) if parts else 'Not Found'

                                hbd_gst = _merge_gst_column(hbd_gst, invalid_hbd_segments)
                                client_gst = _merge_gst_column(client_gst, invalid_client_segments)

                                print(f"   ✅ Final GST (validated; prompt roles preserved):")
                                print(f"      📋 HBD GST (customer/Bill To): {hbd_gst}")
                                print(f"      📋 Client GST (hotel): {client_gst}")

                                pdf_bytes_for_fallback = pdf_data.get('payload')
                                if (hbd_gst == 'Not Found' or client_gst == 'Not Found') and pdf_bytes_for_fallback:
                                    print(
                                        "   ℹ️  GST still missing — OCR fallback cannot assign hotel vs customer "
                                        "(spatial rules); skipped."
                                    )

                                pdf_data['hbd_gst'] = hbd_gst
                                pdf_data['client_gst'] = client_gst
                    
                            else:
                                if pdf_page_count is None:
                                    _fail_reason = "exception while processing PDF (see error above)"
                                elif pdf_page_count == 0:
                                    _fail_reason = "no pages could be rendered from the PDF"
                                else:
                                    _fail_reason = "no structured output received from GPT"
                                _log_pdf_failure(filename, subject, message_id, _fail_reason)

                            # Check if guest name matches any email guest name from table entries (exact logic from test.py)
                            pdf_guest_name = pdf_data.get('guest_name', 'Not Found')
                            matched_table_entries = []  # List of (entry, match_diagnostics) tuples
                    
                            if pdf_guest_name != 'Not Found' and is_valid_guest_name_local(pdf_guest_name) and email_table_entries:
                                def _to_date_for_match(value):
                                    """Parse a date for matching, including 4-digit and 2-digit years."""
                                    if not value or value == 'Not Found':
                                        return None
                                    return to_date(value)

                                def _date_matches_or_pm1(email_value, invoice_value) -> bool:
                                    """True if dates match exactly or within ±1 day."""
                                    d1 = _to_date_for_match(email_value)
                                    d2 = _to_date_for_match(invoice_value)
                                    if not d1 or not d2:
                                        return False
                                    return abs((d1 - d2).days) <= 1

                                print(f"   🔍 Attempting to match PDF guest name '{pdf_guest_name}' with {len(email_table_entries)} email table entry/entries...")
                                # CHANGED: Try to find ALL matching table entries for this PDF (don't break after first match)
                                for idx, entry in enumerate(email_table_entries, 1):
                                    entry_guest = entry.get('guest_name') or entry.get('client_name')
                                    if entry_guest:
                                        # Calculate and log the guest name match score for debugging
                                        try:
                                            a = _remove_titles_only(entry_guest).lower()
                                            b = _remove_titles_only(pdf_guest_name).lower()
                                            guest_score = max(
                                                fuzz.ratio(a, b),
                                                fuzz.partial_ratio(a, b),
                                                fuzz.token_sort_ratio(a, b),
                                                fuzz.token_set_ratio(a, b),
                                            )
                                            # Log the score for debugging
                                            print(f"      🔍 Entry {idx}: Guest name score = {int(guest_score)}/100 ('{entry_guest}' vs '{pdf_guest_name}')")
                                        except Exception as e:
                                            guest_score = 0
                                            print(f"      ⚠️  Entry {idx}: Error calculating guest name score: {e}")
                                
                                        if guest_names_match(entry_guest, pdf_guest_name):
                                            # Required rule: guest name must match using existing logic,
                                            # AND at least one of check-in/check-out must match (exact or ±1 day).
                                            email_check_in = entry.get('check_in_date') or entry.get('email_check_in_date')
                                            email_check_out = entry.get('check_out_date') or entry.get('email_check_out_date')

                                            invoice_check_in = pdf_data.get('check_in_date') or pdf_data.get('arrival_date')
                                            invoice_check_out = pdf_data.get('check_out_date') or pdf_data.get('departure_date')

                                            # Log date matching attempts
                                            ci_match = _date_matches_or_pm1(email_check_in, invoice_check_in)
                                            co_match = _date_matches_or_pm1(email_check_out, invoice_check_out)
                                    
                                            print(f"      📅 Entry {idx}: Check-in match = {ci_match} (Email: '{email_check_in}' vs PDF: '{invoice_check_in}')")
                                            print(f"      📅 Entry {idx}: Check-out match = {co_match} (Email: '{email_check_out}' vs PDF: '{invoice_check_out}')")

                                            if (ci_match or co_match):
                                                # Store match diagnostics for this specific match
                                                try:
                                                    a = _remove_titles_only(entry_guest).lower()
                                                    b = _remove_titles_only(pdf_guest_name).lower()
                                                    guest_match_score = int(max(
                                                        fuzz.ratio(a, b),
                                                        fuzz.partial_ratio(a, b),
                                                        fuzz.token_sort_ratio(a, b),
                                                        fuzz.token_set_ratio(a, b),
                                                    ))
                                                except Exception:
                                                    guest_match_score = None

                                                d_email_ci = _to_date_for_match(email_check_in)
                                                d_inv_ci = _to_date_for_match(invoice_check_in)
                                                d_email_co = _to_date_for_match(email_check_out)
                                                d_inv_co = _to_date_for_match(invoice_check_out)

                                                def _delta(email_d, inv_d):
                                                    if not email_d or not inv_d:
                                                        return None
                                                    diff = (inv_d - email_d).days  # 1=invoice next day, 0=same day, -1=invoice previous day
                                                    return diff if diff in (-1, 0, 1) else None

                                                match_diagnostics = {
                                                    "match_guest_score": guest_match_score,
                                                    "match_checkin_delta": _delta(d_email_ci, d_inv_ci),
                                                    "match_checkout_delta": _delta(d_email_co, d_inv_co),
                                                    "matched_entry_index": idx - 1,  # 0-based index
                                                }
                                        
                                                matched_table_entries.append((entry, match_diagnostics))
                                                print(f"   ✅ Guest+date match found! PDF {i+1} matches table entry {idx}/{len(email_table_entries)}:")
                                                print(f"      Email: '{entry_guest}' ↔ PDF: '{pdf_guest_name}'")
                                                print(f"      Booking Code: {entry.get('booking_code', 'N/A')}")
                                            else:
                                                print(f"      ❌ Entry {idx}: Guest name matched (score={int(guest_score)}) but dates didn't match (±1 day)")
                                        else:
                                            # Log failed match attempts for debugging
                                            print(f"      ❌ Entry {idx}: Guest name score {int(guest_score)} <= 60 threshold ('{entry_guest}' vs '{pdf_guest_name}')")
                        
                                if not matched_table_entries:
                                    print(f"   ⚠️  No match found for PDF guest name '{pdf_guest_name}' among {len(email_table_entries)} email entry/entries")
                                else:
                                    print(f"   ✅ Found {len(matched_table_entries)} match(es) for PDF {i+1}")
                            elif pdf_guest_name != 'Not Found' and not is_valid_guest_name_local(pdf_guest_name):
                                print(f"   ⚠️  PDF guest name '{pdf_guest_name}' appears invalid (looks like hotel name or invalid) - skipping match")
                    
                            # CHANGED: Store all matches in pdf_data instead of single match
                            if matched_table_entries:
                                # Store list of matched entries with diagnostics
                                pdf_data['matched_table_entries'] = matched_table_entries
                                # For backwards compatibility, also store the first match (if needed elsewhere)
                                pdf_data['matched_table_entry'] = matched_table_entries[0][0]
                                pdf_data['matched_table_entry_index'] = matched_table_entries[0][1]['matched_entry_index']
                        
                                # Merge email data into pdf_data (prioritizes email property_name and booking_code)
                                # This ensures hotel_name is updated with email property_name if valid
                                first_entry = matched_table_entries[0][0]
                                entry_booking_code = first_entry.get('booking_code') or booking_code
                                merged_data = merge_email_pdf_data(first_entry, pdf_data, booking_code=entry_booking_code)
                                # Update pdf_data with merged hotel_name (email property_name takes priority)
                                pdf_data['hotel_name'] = merged_data.get('hotel_name', pdf_data.get('hotel_name', 'Not Found'))
                                # Also update booking_code if email has it (though each match will use its own booking_code later)
                                if merged_data.get('booking_code') and merged_data.get('booking_code') != 'Not Found':
                                    pdf_data['booking_code'] = merged_data.get('booking_code')
                        
                                print(f"   🔄 Found {len(matched_table_entries)} match(es) - will create {len(matched_table_entries)} MySQL row(s) with different booking codes")
                                print(f"   📋 PDF DATA (will be used for all matches):")
                                print(f"      Hotel Name: {pdf_data.get('hotel_name', 'Not Found')}")
                                print(f"      Guest Name: {pdf_data.get('guest_name', 'Not Found')}")
                                print(f"      Bill Number: {pdf_data.get('bill_number', 'Not Found')}")
                                print(f"      Bill Date: {pdf_data.get('bill_date', 'Not Found')}")
                                print(f"      Check-in Date: {pdf_data.get('arrival_date') or pdf_data.get('check_in_date', 'Not Found')}")
                                print(f"      Check-out Date: {pdf_data.get('departure_date') or pdf_data.get('check_out_date', 'Not Found')}")
                                print(f"      Total Amount: {pdf_data.get('total_amount', 'Not Found')}")
                                hbd_gst = pdf_data.get('hbd_gst_number') or pdf_data.get('hbd_gst', 'Not Found')
                                client_gst = pdf_data.get('client_gst_number') or pdf_data.get('client_gst', 'Not Found')
                                print(f"      HBD GST Number: {hbd_gst}")
                                print(f"      Client GST Number: {client_gst}")
                                for match_idx, (entry, diagnostics) in enumerate(matched_table_entries, 1):
                                    print(f"      Match {match_idx}: Booking Code = {entry.get('booking_code', 'N/A')}, Score = {diagnostics.get('match_guest_score', 'N/A')}")
                            else:
                                # No match found - process PDF independently
                                pdf_data['matched_table_entries'] = []
                                pdf_data['matched_table_entry'] = None
                                pdf_data['matched_table_entry_index'] = None
                        
                                if not email_guest_names:
                                    print(f"   ℹ️  No guest name in email - PDF {i+1} processed independently")
                                elif pdf_guest_name == 'Not Found':
                                    print(f"   ℹ️  PDF {i+1} missing guest name - processed independently")
                                else:
                                    print(f"   ⚠️  Guest name mismatch: PDF='{pdf_guest_name}' doesn't match any email guest names - PDF processed independently")
                        
                                # Print PDF-only data (no merge)
                                print(f"   📋 PDF DATA (no merge) for {filename}:")
                                print(f"      Hotel Name: {pdf_data.get('hotel_name', 'Not Found')}")
                                print(f"      Guest Name: {pdf_data.get('guest_name', 'Not Found')}")
                                print(f"      Bill Number: {pdf_data.get('bill_number', 'Not Found')}")
                                print(f"      Bill Date: {pdf_data.get('bill_date', 'Not Found')}")
                                print(f"      Check-in Date: {pdf_data.get('arrival_date') or pdf_data.get('check_in_date', 'Not Found')}")
                                print(f"      Check-out Date: {pdf_data.get('departure_date') or pdf_data.get('check_out_date', 'Not Found')}")
                                print(f"      Total Amount: {pdf_data.get('total_amount', 'Not Found')}")
                                hbd_gst = pdf_data.get('hbd_gst_number') or pdf_data.get('hbd_gst', 'Not Found')
                                client_gst = pdf_data.get('client_gst_number') or pdf_data.get('client_gst', 'Not Found')
                                print(f"      HBD GST Number: {hbd_gst}")
                                print(f"      Client GST Number: {client_gst}")
                    
                            pdf_analysis[pdf_key] = pdf_data
                    except ProcessCancelled:
                        raise
                    except Exception as _pdf_err:
                        _log_pdf_failure(filename, subject, message_id, f"exception while analysing PDF: {_pdf_err}")
                        import traceback as _tb
                        _tb.print_exc()
                        _slot = i + 1
                        if not any(k == f"pdf_{_slot}" or str(k).startswith(f"pdf_{_slot}_inv") for k in pdf_analysis):
                            pdf_analysis[f"pdf_{_slot}"] = {
                                "filename": filename,
                                "source_pdf_filename": filename,
                                "payload": pdf_bytes,
                                "ocr_text": "",
                                "page_count": None,
                                "source_attachment_slot": _slot,
                                "invoice_segment_in_pdf": None,
                            }

            # FIX: Second pass - try lenient matching for unmatched email entries
            # Track which email entries matched PDFs in first pass
            matched_entry_indices = set()
            for pdf_key, pdf_data in pdf_analysis.items():
                # CHANGED: Check for multiple matches
                all_matches = pdf_data.get('matched_table_entries', [])
                if all_matches:
                    # Add all matched entry indices
                    for entry, diagnostics in all_matches:
                        matched_idx = diagnostics.get('matched_entry_index')
                        if matched_idx is not None:
                            matched_entry_indices.add(matched_idx)
                else:
                    # Fallback to old single match for backwards compatibility
                    matched_idx = pdf_data.get('matched_table_entry_index')
                    if matched_idx is not None:
                        matched_entry_indices.add(matched_idx)
            
            # Find unmatched email entries
            unmatched_email_entries = []
            for idx, entry in enumerate(email_table_entries):
                if idx not in matched_entry_indices:
                    entry_guest = entry.get('guest_name') or entry.get('client_name')
                    if entry_guest:
                        unmatched_email_entries.append((idx, entry, entry_guest))
            
            # If there are unmatched email entries, try lenient matching with all PDFs
            if False and unmatched_email_entries and len(pdf_analysis) > 0:
                print(f"   🔄 Second pass: Trying lenient matching for {len(unmatched_email_entries)} unmatched email entry/entries...")
                for pdf_key, pdf_data in pdf_analysis.items():
                    # Skip if this PDF already matched (check for multiple matches)
                    all_matches = pdf_data.get('matched_table_entries', [])
                    if all_matches or pdf_data.get('matched_table_entry_index') is not None:
                        continue
                    
                    pdf_guest_name = pdf_data.get('guest_name', 'Not Found')
                    if pdf_guest_name == 'Not Found' or not is_valid_guest_name_local(pdf_guest_name):
                        continue
                    
                    # Try lenient matching with unmatched email entries
                    for entry_idx, entry, entry_guest in unmatched_email_entries:
                        if guest_names_match_lenient(entry_guest, pdf_guest_name):
                            # Match found with lenient matching!
                            entry_booking_code = entry.get('booking_code') or booking_code
                            pdf_data = merge_email_pdf_data(entry, pdf_data, booking_code=entry_booking_code)
                            pdf_data['matched_table_entry_index'] = entry_idx
                            pdf_data['matched_with_lenient'] = True  # Flag to indicate lenient match
                            
                            print(f"   ✅ Lenient match found! PDF '{pdf_key}' matches unmatched email entry {entry_idx+1}/{len(email_table_entries)}:")
                            print(f"      Email: '{entry_guest}' ↔ PDF: '{pdf_guest_name}'")
                            print(f"      Booking Code: {entry.get('booking_code', 'N/A')}")
                            
                            # Update the PDF data in pdf_analysis
                            pdf_analysis[pdf_key] = pdf_data
                            
                            # Remove from unmatched list
                            unmatched_email_entries = [(idx, e, g) for idx, e, g in unmatched_email_entries if idx != entry_idx]
                            break
                
                # Log remaining unmatched entries
                if unmatched_email_entries:
                    print(f"   ℹ️  {len(unmatched_email_entries)} email entry/entries still unmatched after lenient matching")
    else:
        print(f"   ⚠️  No attachments found in email")
    
    # Basic text extraction for email metadata only
    combined_text = body_full + ' ' + subject
    amounts = extract_amounts(combined_text)
    urgency_level = extract_urgency_level(combined_text)
    
    result = {
        'to_email': to_email,
        'cc_email': cc_email,
        'bcc_email': bcc_email,
        'reply_to': reply_to,
        'message_id': message_id,
        'in_reply_to': in_reply_to,
        'references': references,
        'attachments': attachments,
        'attachment_texts': attachment_texts,
        'pdf_analysis': pdf_analysis,
        'amounts': amounts,
        'urgency_level': urgency_level,
        'booking_code': booking_code,  # Booking code extracted from email, not PDF
        'email_booking_details': email_booking_details
    }

    if email_booking_details:
        if email_booking_details.get('guest_name'):
            result['email_guest_name'] = email_booking_details['guest_name']
        # Set property_name from property_name or property_context
        property_name = email_booking_details.get('property_name') or email_booking_details.get('property_context')
        if property_name:
            result['email_property_name'] = property_name
        if email_booking_details.get('check_in_date'):
            result['email_check_in_date'] = email_booking_details['check_in_date']
        if email_booking_details.get('check_out_date'):
            result['email_check_out_date'] = email_booking_details['check_out_date']
        if email_booking_details.get('best_table_entry'):
            result['email_table_best_entry'] = email_booking_details['best_table_entry']
        if email_booking_details.get('table_entries'):
            result['email_table_entries'] = email_booking_details.get('table_entries')
    elif booking_code and 'table_entries' in email_booking_details:
        # Ensure table entry propagated even if other fields absent
        entries = email_booking_details.get('table_entries') or []
        if entries:
            result['email_table_best_entry'] = entries[0]
            result['email_table_entries'] = entries

    return result
