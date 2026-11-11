"""Warsoft invoice-push HTTP client and token refresh. Payload mapping stays in the push service."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Any, Optional

import requests
from dotenv import find_dotenv, load_dotenv, set_key

load_dotenv()

API_PUSH_ENDPOINT = os.getenv(
    "API_PUSH_ENDPOINT",
    "https://hbinvoiceapi.staysimplyfied.com/api/HotelInvoice/Push",
)


def _is_token_expired(access_token, token_expiry):
    """Check if the current token is expired or about to expire"""
    if not access_token:
        return True

    if not token_expiry:
        return True

    try:
        # Parse expiry date (format: 2025-12-29T12:22:27.7406891+05:30)
        expiry_dt = datetime.fromisoformat(token_expiry.replace("+05:30", ""))

        # Refresh if token expires within 1 day
        if datetime.now() >= expiry_dt - timedelta(days=1):
            return True

        return False
    except Exception as e:
        print(f"⚠️  Error parsing token expiry: {e}")
        return True


def refresh_api_token():
    """Fetch new access token from Warsoft authentication API"""
    try:
        print("🔐 Authenticating with Warsoft API to refresh token...")

        auth_url = os.getenv(
            "WARSOFT_AUTH_URL",
            "https://hbinvoiceapi.staysimplyfied.com/api/auth/authentication",
        )
        username = os.getenv("WARSOFT_USERNAME", "hbsashai")
        password = os.getenv("WARSOFT_PASSWORD", "PRODB5701261C")

        payload = {"username": username, "password": password}

        response = requests.post(
            auth_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )

        response.raise_for_status()
        auth_data = response.json()

        new_token = auth_data.get("token")
        expires_at = auth_data.get("expiresAt")
        validity_days = auth_data.get("validityDays", 7)

        if not new_token:
            print("❌ Failed to get token from authentication response")
            return None

        env_file = find_dotenv()
        if env_file:
            set_key(env_file, "ACCESS_TOKEN", new_token)
            set_key(env_file, "WARSOFT_TOKEN_EXPIRY", expires_at)
            print(f"✅ New token obtained (valid for {validity_days} days)")
            print(f"   Expires at: {expires_at}")
            print(f"   Token saved to .env file")
        else:
            print("⚠️  .env file not found - token not saved to file")

        return new_token

    except requests.exceptions.RequestException as e:
        print(f"❌ Error authenticating with Warsoft: {e}")
        if hasattr(e, "response") and e.response is not None:
            print(f"   Status code: {e.response.status_code}")
            print(f"   Response: {e.response.text[:500]}")
        return None
    except Exception as e:
        print(f"❌ Unexpected error during authentication: {type(e).__name__}: {e}")
        return None


def get_api_bearer_token():
    """Get API bearer token, refreshing if expired or missing"""
    access_token = os.getenv("WARSOFT_ACCESS_TOKEN") or os.getenv("ACCESS_TOKEN", "")
    token_expiry = os.getenv("WARSOFT_TOKEN_EXPIRY")

    if _is_token_expired(access_token, token_expiry):
        print("🔄 Access token expired or missing - fetching new token...")
        new_token = refresh_api_token()
        if new_token:
            os.environ["ACCESS_TOKEN"] = new_token
            return new_token
        print("⚠️  Failed to refresh token - using existing token if available")
        return access_token if access_token else ""

    return access_token


def post_invoice_payload(
    json_data: dict[str, Any],
    *,
    row_index: int,
    token: Optional[str] = None,
    endpoint: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """POST a pre-built invoice payload. Returns (ok, error_message)."""
    current_token = token if token is not None else get_api_bearer_token()
    push_url = endpoint or API_PUSH_ENDPOINT
    headers = {
        "accept": "text/plain",
        "Authorization": f"Bearer {current_token}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            push_url,
            headers=headers,
            json=json_data,
            timeout=30,
        )
    except requests.exceptions.Timeout:
        print(f"      ❌ Row {row_index + 1} API push failed: Request timeout")
        return False, "Request timeout"
    except requests.exceptions.RequestException as req_err:
        print(f"      ❌ Request Exception: {type(req_err).__name__}: {str(req_err)}")
        print(f"      ❌ Row {row_index + 1} API push failed: {str(req_err)}")
        return False, f"Network error: {str(req_err)}"

    print("      📥 Response Details:")
    print(f"         Status Code: {response.status_code}")
    print(f"         Status Text: {response.reason}")
    print("         Response Headers:")
    for header_name, header_value in response.headers.items():
        if header_name.lower() in ["authorization", "set-cookie", "cookie"]:
            print(f"            - {header_name}: {'*' * 20}...")
        else:
            print(f"            - {header_name}: {header_value}")

    try:
        response_json = response.json()
        print("         Response Body (JSON):")
        response_str = json.dumps(response_json, indent=12, ensure_ascii=False)
        for line in response_str.split("\n"):
            print(f"            {line}")
    except (ValueError, json.JSONDecodeError):
        response_text = response.text
        print("         Response Body (Text):")
        if len(response_text) > 500:
            print(
                f"            {response_text[:500]}... (truncated, total length: {len(response_text)})"
            )
        else:
            for line in response_text.split("\n"):
                print(f"            {line}")

    if response.status_code == 200:
        print(f"      ✅ Row {row_index + 1} pushed successfully to API")
        return True, None
    if response.status_code == 401:
        print(
            f"      ❌ Row {row_index + 1} API push failed: Status {response.status_code} (Unauthorized)"
        )
        print("         💡 This usually means:")
        print("            - The Bearer token is missing or invalid")
        print("            - The token has expired")
        print("            - The token doesn't have the required permissions")
        print("         🔍 Check your ACCESS_TOKEN environment variable")
        current_token = get_api_bearer_token()
        if not current_token:
            print("         ⚠️  WARNING: ACCESS_TOKEN is empty or not set!")
        elif len(current_token) < 20:
            print(
                f"         ⚠️  WARNING: ACCESS_TOKEN seems too short (length: {len(current_token)})"
            )
        try:
            api_msg = response.json().get("message") or "Unauthorized (HTTP 401)"
        except Exception:
            api_msg = "Unauthorized (HTTP 401)"
        return False, api_msg

    try:
        api_msg = response.json().get("message") or f"HTTP {response.status_code}"
    except Exception:
        api_msg = f"HTTP {response.status_code}"
    print(f"      ❌ Row {row_index + 1} API push failed: {api_msg}")
    return False, api_msg
