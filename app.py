from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from config import Settings

log = logging.getLogger("rah_tts_proxy")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cache-Control": "no-store",
}


class TTSRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=2000)
    voice: str = Field(default="steven", pattern=r"^[a-z0-9_-]{1,32}$")
    stability: float = Field(default=0.5, ge=0, le=1)
    similarity_boost: float = Field(default=0.75, ge=0, le=1)
    style: float = Field(default=0.0, ge=0, le=1)
    allow_fallback: bool = False

    @field_validator("text")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be blank")
        return value


class CloneFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data: str = Field(min_length=16, max_length=5_600_000)
    name: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")
    type: str = Field(pattern=r"^audio/(mpeg|wav|x-wav)$")


class CloneRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=80)
    description: str = Field(default="", max_length=240)
    files: list[CloneFile] = Field(min_length=1, max_length=3)


class RateLimiter:
    def __init__(self, limit: int, window: int) -> None:
        self.limit, self.window = limit, window
        self.events: dict[str, deque[float]] = defaultdict(deque)
        self.lock = asyncio.Lock()

    async def check(self, key: str) -> None:
        now = time.monotonic()
        async with self.lock:
            entries = self.events[key]
            while entries and entries[0] <= now - self.window:
                entries.popleft()
            if len(entries) >= self.limit:
                raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": str(self.window)})
            entries.append(now)


@dataclass
class Sample:
    data: bytes
    content_type: str
    expires_at: float
    consumed: bool = False


class GatewayClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def _json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        timeout = httpx.Timeout(self.settings.gateway_timeout_seconds, connect=3.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.post(f"{self.settings.gateway_url}{path}", json=body)
        if response.status_code >= 400:
            raise HTTPException(503, "voice service unavailable")
        try:
            value = response.json()
        except ValueError as exc:
            raise HTTPException(503, "voice service returned invalid metadata") from exc
        if not isinstance(value, dict):
            raise HTTPException(503, "voice service returned invalid metadata")
        return value

    async def speak(self, payload: TTSRequest, voice_id: str | None, fallback: bool = False) -> tuple[bytes, str]:
        provider = "chatterbox" if fallback else "elevenlabs"
        body: dict[str, Any] = {
            "text": payload.text,
            "voice": None if fallback else voice_id,
            "provider": provider,
            "model": None if fallback else "eleven_multilingual_v2",
            "format": "mp3",
            "provider_opts": {
                "stability": payload.stability,
                "similarity_boost": payload.similarity_boost,
                "style": payload.style,
            },
        }
        meta = await self._json("/speak", body)
        url = meta.get("url")
        if not isinstance(url, str):
            raise HTTPException(503, "voice service returned no audio reference")
        host = (urlparse(url).hostname or "").lower()
        if host not in {"192.168.1.49", "anvil", "anvil.tail07cfd0.ts.net"}:
            raise HTTPException(503, "voice service returned an untrusted audio reference")
        timeout = httpx.Timeout(20.0, connect=3.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            async with client.stream("GET", url) as response:
                if response.status_code != 200:
                    raise HTTPException(503, "audio retrieval failed")
                chunks, total = [], 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > self.settings.max_audio_bytes:
                        raise HTTPException(503, "audio response exceeded limit")
                    chunks.append(chunk)
        audio = b"".join(chunks)
        validate_audio(audio)
        return audio, str(meta.get("provider") or provider)

    async def clone(self, name: str, sample_urls: list[str], consent_ref: str) -> dict[str, Any]:
        return await self._json(
            "/clone",
            {"name": name, "samples": sample_urls, "provider": "elevenlabs", "consent_ref": consent_ref},
        )


class AuditStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS clone_requests (
                    idempotency_key TEXT PRIMARY KEY,
                    request_digest TEXT NOT NULL,
                    consent_digest TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','complete','failed')),
                    result_json TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )"""
            )

    def begin(self, key: str, request_digest: str, consent_digest: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT request_digest, status, result_json FROM clone_requests WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            if row:
                if not hmac.compare_digest(row["request_digest"], request_digest):
                    raise HTTPException(409, "idempotency key conflict")
                if row["status"] == "complete" and row["result_json"]:
                    return json.loads(row["result_json"])
                raise HTTPException(409, "clone request already recorded")
            connection.execute(
                "INSERT INTO clone_requests(idempotency_key, request_digest, consent_digest, status) VALUES (?, ?, ?, 'pending')",
                (key, request_digest, consent_digest),
            )
        return None

    def finish(self, key: str, status: str, result: dict[str, Any] | None = None) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE clone_requests SET status = ?, result_json = ?, updated_at = CURRENT_TIMESTAMP WHERE idempotency_key = ?",
                (status, json.dumps(result, separators=(",", ":")) if result else None, key),
            )


def validate_audio(data: bytes, minimum: int = 100) -> None:
    is_mp3 = data.startswith(b"ID3") or (len(data) > 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0)
    if len(data) < minimum or not is_mp3:
        raise HTTPException(503, "voice service returned invalid audio")


def _decode_file(item: CloneFile) -> bytes:
    try:
        value = base64.b64decode(item.data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(422, "invalid audio encoding") from exc
    if len(value) > 4_194_304:
        raise HTTPException(413, "audio sample exceeded limit")
    is_wav = value.startswith(b"RIFF") and value[8:12] == b"WAVE"
    is_mp3 = value.startswith(b"ID3") or (len(value) > 2 and value[0] == 0xFF and value[1] & 0xE0 == 0xE0)
    if not (is_wav or is_mp3):
        raise HTTPException(422, "unsupported audio sample")
    return value


def create_app(
    settings: Settings | None = None,
    gateway: GatewayClient | None = None,
    audit_store: AuditStore | None = None,
) -> FastAPI:
    cfg = settings or Settings()
    upstream = gateway or GatewayClient(cfg)
    app = FastAPI(title="RAH TTS Proxy", docs_url=None, redoc_url=None, openapi_url=None)
    limiter = RateLimiter(cfg.request_limit, cfg.request_window_seconds)
    tts_slots, clone_slots = asyncio.Semaphore(4), asyncio.Semaphore(1)
    samples: dict[str, Sample] = {}
    audit = audit_store or AuditStore(cfg.state_db)

    def authorized(value: str | None, expected: str) -> bool:
        return bool(value and expected and hmac.compare_digest(value, f"Bearer {expected}"))

    @app.middleware("http")
    async def policy(request: Request, call_next):
        if int(request.headers.get("content-length", "0") or "0") > cfg.max_body_bytes:
            response = JSONResponse({"error": "request body exceeded limit"}, status_code=413)
        elif request.method == "OPTIONS":
            origin = request.headers.get("origin", "")
            if origin not in cfg.allowed_origins:
                response = JSONResponse({"error": "origin not allowed"}, status_code=403)
            else:
                response = Response(status_code=204)
        else:
            response = await call_next(request)
        origin = request.headers.get("origin", "")
        if origin in cfg.allowed_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, Idempotency-Key, X-Consent-Ref, X-Clone-Authorization"
        for key, value in SECURITY_HEADERS.items():
            response.headers[key] = value
        response.headers["X-Request-ID"] = request.headers.get("x-request-id", secrets.token_hex(8))[:64]
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, __: RequestValidationError):
        return JSONResponse({"error": "invalid request"}, status_code=422)

    @app.exception_handler(Exception)
    async def unexpected(_: Request, exc: Exception):
        log.error(json.dumps({"event": "request_failed", "class": type(exc).__name__}))
        return JSONResponse({"error": "internal server error"}, status_code=500)

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": cfg.service_name, "version": "2.0.0", "build": cfg.build_sha}

    @app.get("/voices")
    async def voices(authorization: str | None = Header(default=None)):
        if not authorized(authorization, cfg.api_key):
            raise HTTPException(401, "authentication required")
        mapping = cfg.voice_map
        return {
            "voices": [
                {"id": alias, "label": alias.replace("_", " ").title(), "available": bool(voice_id)}
                for alias, voice_id in sorted(mapping.items())
            ],
            "default": cfg.default_voice,
        }

    @app.post("/tts")
    async def tts(payload: TTSRequest, request: Request, authorization: str | None = Header(default=None)):
        if not authorized(authorization, cfg.api_key):
            raise HTTPException(401, "authentication required")
        principal = hashlib.sha256(authorization.encode()).hexdigest()[:16]
        await limiter.check(f"tts:{principal}:{request.client.host if request.client else 'unknown'}")
        voice_id = cfg.voice_map.get(payload.voice)
        if not voice_id and not payload.allow_fallback:
            raise HTTPException(503, "requested voice unavailable")
        async with tts_slots:
            try:
                audio, provider = await upstream.speak(payload, voice_id, fallback=False)
            except (HTTPException, httpx.HTTPError):
                if not payload.allow_fallback:
                    raise HTTPException(503, "voice service unavailable")
                audio, provider = await upstream.speak(payload, None, fallback=True)
        return Response(
            audio,
            media_type="audio/mpeg",
            headers={"X-Voice": payload.voice if voice_id else "edge-fallback", "X-Provider": provider},
        )

    @app.post("/clone")
    async def clone(
        payload: CloneRequest,
        request: Request,
        authorization: str | None = Header(default=None),
        x_clone_authorization: str | None = Header(default=None),
        x_consent_ref: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None),
    ):
        if not authorized(authorization, cfg.api_key) or not authorized(x_clone_authorization, cfg.clone_key):
            raise HTTPException(403, "clone authorization required")
        if not x_consent_ref or len(x_consent_ref) > 200:
            raise HTTPException(400, "consent reference required")
        if not idempotency_key or len(idempotency_key) > 100:
            raise HTTPException(400, "idempotency key required")
        principal = hashlib.sha256(authorization.encode()).hexdigest()[:16]
        await limiter.check(f"clone:{principal}:{request.client.host if request.client else 'unknown'}")
        decoded = [_decode_file(item) for item in payload.files]
        if sum(map(len, decoded)) > cfg.max_body_bytes:
            raise HTTPException(413, "audio samples exceeded aggregate limit")
        digest = hashlib.sha256(
            payload.model_dump_json().encode() + x_consent_ref.encode()
        ).hexdigest()
        previous = audit.begin(
            idempotency_key,
            digest,
            hashlib.sha256(x_consent_ref.encode()).hexdigest(),
        )
        if previous is not None:
            return previous
        tokens = []
        base = cfg.callback_base_url.rstrip("/")
        now = time.monotonic()
        for item, data in zip(payload.files, decoded):
            token = secrets.token_urlsafe(32)
            samples[token] = Sample(data, item.type, now + cfg.sample_ttl_seconds)
            tokens.append(token)
        urls = [f"{base}/_internal/sample/{token}" for token in tokens]
        try:
            async with clone_slots:
                result = await upstream.clone(payload.name, urls, x_consent_ref)
        except Exception:
            audit.finish(idempotency_key, "failed")
            raise
        finally:
            for token in tokens:
                samples.pop(token, None)
        sanitized = {
            "success": result.get("status") in {"ready", "complete", "succeeded"} or bool(result.get("voice_id")),
            "status": result.get("status", "submitted"),
            "model": "eleven_multilingual_v2",
        }
        audit.finish(idempotency_key, "complete", sanitized)
        return sanitized

    @app.get("/_internal/sample/{token}", include_in_schema=False)
    async def internal_sample(token: str, request: Request):
        if not request.client or request.client.host not in {"127.0.0.1", "::1", "testclient"}:
            raise HTTPException(404, "not found")
        sample = samples.get(token)
        if not sample or sample.consumed or sample.expires_at < time.monotonic():
            raise HTTPException(404, "not found")
        sample.consumed = True
        return Response(sample.data, media_type=sample.content_type)

    return app


app = create_app()
