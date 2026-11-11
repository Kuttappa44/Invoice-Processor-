"""Azure OpenAI / OpenAI client. Services pass images and prompts; they do not build this client."""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

try:
    from openai import OpenAI

    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False
    OpenAI = None

client = None

if OPENAI_AVAILABLE:
    azure_api_key = os.getenv("AZURE_OPENAI_API_KEY")
    azure_base_url = os.getenv("AZURE_OPENAI_BASE_URL")

    if azure_api_key and azure_base_url:
        client = OpenAI(api_key=azure_api_key, base_url=azure_base_url)
    else:
        openai_api_key = os.getenv("OPENAI_API_KEY")
        if openai_api_key:
            client = OpenAI(api_key=openai_api_key)
        else:
            client = None
            print(
                "⚠️  Warning: Neither AZURE_OPENAI_API_KEY/AZURE_OPENAI_BASE_URL nor OPENAI_API_KEY found in .env"
            )
else:
    client = None
