"""
Loads and validates required environment variables. Single place every
other module imports config from — don't read os.environ directly elsewhere.
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    openai_api_key: str
    openai_assistant_id: str
    openai_vector_store_id: str
    zendesk_subdomain: str
    chunk_size_tokens: int
    chunk_overlap_tokens: int
    markdown_output_dir: str
    state_file_path: str
    # Optional Zendesk auth — only needed if some articles require auth
    zendesk_email: str = ""
    zendesk_api_token: str = ""


def load_config() -> Config:
    """
    Reads env vars (via python-dotenv from .env, falling back to real env).
    Fails fast at startup with a clear error if anything required is missing.

    ZENDESK_EMAIL and ZENDESK_API_TOKEN are optional — the public Help Center
    API does not require auth (confirmed live 2026-06-19).
    """
    missing = []

    def require(name: str) -> str:
        val = os.environ.get(name, "").strip()
        if not val:
            missing.append(name)
        return val

    openai_api_key = require("OPENAI_API_KEY")
    openai_assistant_id = require("OPENAI_ASSISTANT_ID")
    openai_vector_store_id = require("OPENAI_VECTOR_STORE_ID")
    zendesk_subdomain = os.environ.get("ZENDESK_SUBDOMAIN", "support.optisigns.com").strip()

    if missing:
        raise EnvironmentError(
            f"Missing required environment variable(s): {', '.join(missing)}\n"
            "Copy .env.sample to .env and fill in the values."
        )

    return Config(
        openai_api_key=openai_api_key,
        openai_assistant_id=openai_assistant_id,
        openai_vector_store_id=openai_vector_store_id,
        zendesk_subdomain=zendesk_subdomain,
        zendesk_email=os.environ.get("ZENDESK_EMAIL", ""),
        zendesk_api_token=os.environ.get("ZENDESK_API_TOKEN", ""),
        chunk_size_tokens=int(os.environ.get("CHUNK_SIZE_TOKENS", "800")),
        chunk_overlap_tokens=int(os.environ.get("CHUNK_OVERLAP_TOKENS", "400")),
        markdown_output_dir=os.environ.get("MARKDOWN_OUTPUT_DIR", "data/markdown"),
        state_file_path=os.environ.get("STATE_FILE_PATH", "data/state.json"),
    )
