from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


def _csv(name: str, default: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in os.getenv(name, default).split(",") if item.strip())


def _read_secret(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _load_voice_map(path: str) -> dict[str, str]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(k): str(v) for k, v in value.items() if isinstance(k, str) and isinstance(v, str)}


def _build_sha() -> str:
    from_env = os.getenv("RAH_TTS_BUILD_SHA")
    if from_env:
        return from_env
    try:
        return Path(__file__).with_name("BUILD_SHA").read_text(encoding="utf-8").strip() or "development"
    except OSError:
        return "development"


@dataclass(frozen=True)
class Settings:
    service_name: str = "rah-tts-proxy"
    build_sha: str = field(default_factory=_build_sha)
    default_voice: str = field(default_factory=lambda: os.getenv("RAH_TTS_DEFAULT_VOICE", "steven"))
    allowed_origins: tuple[str, ...] = field(
        default_factory=lambda: _csv(
            "RAH_TTS_ALLOWED_ORIGINS",
            "https://rightathomebnb.com,https://www.rightathomebnb.com",
        )
    )
    gateway_url: str = field(default_factory=lambda: os.getenv("RAH_TTS_GATEWAY_URL", "http://127.0.0.1:8239"))
    callback_base_url: str = field(default_factory=lambda: os.getenv("RAH_TTS_CALLBACK_BASE_URL", "http://127.0.0.1:8472"))
    api_key_file: str = field(default_factory=lambda: os.getenv("RAH_TTS_API_KEY_FILE", "/run/credentials/rah_tts_api_key"))
    clone_key_file: str = field(default_factory=lambda: os.getenv("RAH_TTS_CLONE_KEY_FILE", "/run/credentials/rah_tts_clone_key"))
    voice_map_file: str = field(default_factory=lambda: os.getenv("RAH_TTS_VOICE_MAP_FILE", "/run/credentials/rah_tts_voice_map"))
    request_limit: int = field(default_factory=lambda: int(os.getenv("RAH_TTS_REQUEST_LIMIT", "30")))
    request_window_seconds: int = field(default_factory=lambda: int(os.getenv("RAH_TTS_REQUEST_WINDOW_SECONDS", "60")))
    max_body_bytes: int = field(default_factory=lambda: int(os.getenv("RAH_TTS_MAX_BODY_BYTES", "6291456")))
    max_audio_bytes: int = field(default_factory=lambda: int(os.getenv("RAH_TTS_MAX_AUDIO_BYTES", "16777216")))
    min_audio_bytes: int = field(default_factory=lambda: int(os.getenv("RAH_TTS_MIN_AUDIO_BYTES", "100")))
    gateway_timeout_seconds: float = field(default_factory=lambda: float(os.getenv("RAH_TTS_GATEWAY_TIMEOUT", "45")))
    sample_ttl_seconds: int = field(default_factory=lambda: int(os.getenv("RAH_TTS_SAMPLE_TTL", "90")))
    state_db: str = field(
        default_factory=lambda: os.getenv("RAH_TTS_STATE_DB", str(Path(tempfile.gettempdir()) / "rah-tts-proxy-state.db"))
    )

    @property
    def api_key(self) -> str:
        return _read_secret(self.api_key_file)

    @property
    def clone_key(self) -> str:
        return _read_secret(self.clone_key_file)

    @property
    def voice_map(self) -> dict[str, str]:
        return _load_voice_map(self.voice_map_file)
