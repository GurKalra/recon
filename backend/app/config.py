"""Application settings, read from environment variables."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ../.env covers running from backend/, .env covers running from the repo root.
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    app_env: str = "development"
    database_url: str = "postgresql://recon:recon@localhost:5432/recon"
    db_pool_min: int = 1
    db_pool_max: int = 10
    cors_origins: list[str] = ["http://localhost:5173"]
    # The session cookie is sent over HTTPS only outside development.
    cookie_name: str = "recon_session"
    # Uploads: where staged files wait, and the limits that protect the server.
    upload_dir: str = "/tmp/recon_uploads"
    max_upload_mb: int = 100
    max_unzipped_mb: int = 1024        # an .xlsx may not expand beyond this
    # The folder of sample partner files the demo workspace loads and offers for download.
    sample_data_dir: str = "../sample_data/small"
    # Addresses of reverse proxies whose X-Forwarded-For header can be believed ("*" = all; only
    # safe when the proxy is the only way in). Empty = use the connecting address.
    trusted_proxies: str = ""
    # Folder of the built React app. When set, the API serves the website too.
    web_dist: str = ""
    # AI suggestions each person may ask for per 10 minutes.
    ai_suggestions_per_10min: int = 10
    # Requests to ask the AI about unsure payments (up to 10 payments each) per person.
    ai_reranks_per_10min: int = 5

    # AI features. Everything works without a key: the rules-only path is used instead.
    groq_api_key: str = ""
    groq_model: str = "qwen/qwen3.8-27b"
    # Reasoning models (Qwen, gpt-oss, ...): hide the thinking so only the JSON comes back, and
    # keep the thinking short. Leave both empty for models that do not reason.
    groq_reasoning_format: str = "hidden"
    groq_reasoning_effort: str = "low"   # none | low | medium | high
    groq_temperature: float = 0.3        # Qwen advises against 0 for reasoning models
    groq_max_tokens: int = 4000          # thinking tokens count towards this
    # Tokens per minute the client may spend (the provider counts the reserved answer too).
    # Groq's free plan allows about 8000; 0 turns pacing off (for paid plans).
    groq_tpm_limit: int = 7000
    groq_base_url: str = "https://api.groq.com/openai/v1"
    llm_timeout_s: float = 30.0


@lru_cache
def get_settings() -> Settings:
    return Settings()