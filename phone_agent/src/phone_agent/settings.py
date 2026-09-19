"""Runtime configuration, loaded from environment variables / .env."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Public address of this server -------------------------------------
    # Twilio fetches TwiML from here and opens the media WebSocket here, so it
    # must be reachable from the internet (ngrok/cloudflared locally, or the
    # hostname of wherever you deploy).
    public_base_url: str = Field(default="http://localhost:8080")

    # --- Twilio -------------------------------------------------------------
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""  # the number the agent dials out from
    validate_twilio_signatures: bool = True

    # --- Who to hand the call to -------------------------------------------
    my_phone_number: str = ""  # your phone; we ring this when a human answers

    # --- Anthropic (only needed for mode="auto") ---------------------------
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5"

    # --- Speech recognition (only needed for mode="auto") ------------------
    asr_provider: Literal["deepgram", "none"] = "none"
    deepgram_api_key: str = ""
    deepgram_model: str = "nova-2-phonecall"

    # --- Notifications ------------------------------------------------------
    notify_sms: bool = True
    notify_ntfy_topic: str = ""  # e.g. "ahmed-phone-agent" on ntfy.sh (free)
    notify_ntfy_server: str = "https://ntfy.sh"
    notify_pushover_token: str = ""
    notify_pushover_user: str = ""

    # --- Guardrails ---------------------------------------------------------
    # Only these destinations may be dialled. Empty list = allow anything,
    # which is fine for personal use but a bad idea on a shared deployment.
    allowed_destinations: list[str] = Field(default_factory=list)
    max_call_seconds: int = 3600
    max_concurrent_calls: int = 3
    # Emergency numbers are refused outright regardless of the allowlist.
    blocked_destinations: list[str] = Field(
        default_factory=lambda: ["911", "112", "999", "988", "+1911"]
    )

    # --- Behaviour ----------------------------------------------------------
    # Spoken once when a live human is detected, to hold the line for the few
    # seconds it takes you to pick up. Disclosure is deliberate and on by
    # default: the person on the other end should know what they're talking to.
    human_greeting: str = (
        "Hello. This is an automated assistant holding the line on behalf of "
        "the caller. Please stay on for just a moment while I connect them."
    )
    bridge_announcement: str = (
        "Your call has reached a person. Connecting you now."
    )
    detector_strictness: float = 0.55  # 0 = trigger early/often, 1 = be sure
    tts_voice: str = "Polly.Matthew-Neural"

    # --- Storage ------------------------------------------------------------
    database_path: str = "phone_agent.db"

    @property
    def ws_base_url(self) -> str:
        base = self.public_base_url.rstrip("/")
        if base.startswith("https://"):
            return "wss://" + base[len("https://"):]
        if base.startswith("http://"):
            return "ws://" + base[len("http://"):]
        return base

    def url(self, path: str) -> str:
        return f"{self.public_base_url.rstrip('/')}/{path.lstrip('/')}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
