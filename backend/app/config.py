from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_origins: str = ""
    app_origin: str = ""

    supabase_url: str = ""
    supabase_publishable_key: str = ""
    supabase_anon_key: str = ""
    supabase_service_role_key: str = ""

    orcarouter_api_key: str = ""
    instavm_api_key: str = ""
    browser_use_api_key: str = ""
    gemini_api_key: str = ""
    flymyai_api_key: str = ""

    model_name: str = "tencent/hy4-preview-free"
    orcarouter_url: str = "https://api.orcarouter.ai/v1/chat/completions"
    instavm_url: str = "https://api.instavm.io"
    published_origin: str = "https://chat-pal-deploy.lovable.app"

    chat_deployment_max_request_ms: str = ""
    chat_sandbox_allowed_domains: str = ""
    chat_browser_allowed_hosts: str = ""
    chat_nixpkgs_rev: str = ""
    chat_nix_version: str = ""
    chat_nix_installer_sha256: str = ""

    static_dir: str = "../public"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

