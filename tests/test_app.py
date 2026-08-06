import base64
import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import AuditStore, create_app, validate_audio
from config import Settings


MP3 = b"ID3" + b"x" * 200
WAV = b"RIFF" + (b"x" * 4) + b"WAVE" + b"x" * 200


class FakeGateway:
    def __init__(self):
        self.fail_primary = False
        self.clone_calls = 0

    async def speak(self, payload, voice_id, fallback=False):
        if self.fail_primary and not fallback:
            raise HTTPException(503, "failed")
        return MP3, "chatterbox" if fallback else "elevenlabs"

    async def clone(self, name, sample_urls, consent_ref):
        self.clone_calls += 1
        return {"status": "ready", "voice_id": "redacted"}


@pytest.fixture()
def client(tmp_path: Path):
    (tmp_path / "api").write_text("api-test", encoding="utf-8")
    (tmp_path / "clone").write_text("clone-test", encoding="utf-8")
    (tmp_path / "voices").write_text(json.dumps({"steven": "provider-id"}), encoding="utf-8")
    settings = Settings(
        api_key_file=str(tmp_path / "api"),
        clone_key_file=str(tmp_path / "clone"),
        voice_map_file=str(tmp_path / "voices"),
        allowed_origins=("https://rightathomebnb.com",),
        request_limit=2,
        max_body_bytes=1024 * 1024,
        state_db=str(tmp_path / "state.db"),
    )
    gateway = FakeGateway()
    with TestClient(create_app(settings, gateway, AuditStore(settings.state_db))) as test_client:
        test_client.gateway = gateway
        yield test_client


def auth():
    return {"Authorization": "Bearer api-test"}


def test_health_is_minimal_and_hardened(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert set(response.json()) == {"status", "service", "version", "build"}
    for name in ("x-content-type-options", "x-frame-options", "referrer-policy", "permissions-policy", "cache-control"):
        assert response.headers[name]


def test_route_and_method_contract(client):
    assert client.get("/missing").status_code == 404
    assert client.post("/health").status_code == 405
    assert client.get("/tts").status_code == 405
    assert client.get("/clone").status_code == 405


def test_authentication_required(client):
    assert client.get("/voices").status_code == 401
    assert client.post("/tts", json={"text": "hello"}).status_code == 401
    assert client.post("/clone", json={}).status_code in {403, 422}


def test_voices_do_not_expose_provider_ids(client):
    response = client.get("/voices", headers=auth())
    assert response.status_code == 200
    assert response.json()["voices"] == [{"id": "steven", "label": "Steven", "available": True}]
    assert "provider-id" not in response.text


def test_tts_binary_contract(client):
    response = client.post("/tts", json={"text": "hello", "voice": "steven"}, headers=auth())
    assert response.status_code == 200
    assert response.content == MP3
    assert response.headers["content-type"].startswith("audio/mpeg")
    assert response.headers["x-voice"] == "steven"


def test_fallback_is_explicit(client):
    client.gateway.fail_primary = True
    denied = client.post("/tts", json={"text": "hello", "voice": "steven"}, headers=auth())
    assert denied.status_code == 503
    allowed = client.post(
        "/tts", json={"text": "hello", "voice": "steven", "allow_fallback": True}, headers=auth()
    )
    assert allowed.status_code == 200
    assert allowed.headers["x-provider"] == "chatterbox"


def test_rate_limit(client):
    headers = {**auth(), "X-Forwarded-For": "unique-rate"}
    assert client.post("/tts", json={"text": "one"}, headers=headers).status_code == 200
    assert client.post("/tts", json={"text": "two"}, headers=headers).status_code == 200
    assert client.post("/tts", json={"text": "three"}, headers=headers).status_code == 429


def test_exact_cors(client):
    good = client.options("/tts", headers={"Origin": "https://rightathomebnb.com"})
    bad = client.options("/tts", headers={"Origin": "https://evil.example"})
    assert good.status_code == 204
    assert good.headers["access-control-allow-origin"] == "https://rightathomebnb.com"
    assert bad.status_code == 403
    assert "access-control-allow-origin" not in bad.headers


def test_clone_requires_admin_consent_and_idempotency(client):
    body = {
        "name": "Authorized sample",
        "files": [{"data": base64.b64encode(WAV).decode(), "name": "sample.wav", "type": "audio/wav"}],
    }
    assert client.post("/clone", json=body, headers=auth()).status_code == 403
    admin = {
        **auth(),
        "X-Clone-Authorization": "Bearer clone-test",
        "X-Consent-Ref": "consent://test/1",
        "Idempotency-Key": "clone-one",
    }
    first = client.post("/clone", json=body, headers=admin)
    second = client.post("/clone", json=body, headers=admin)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert client.gateway.clone_calls == 1


def test_clone_rejects_bad_audio(client):
    headers = {
        **auth(),
        "X-Clone-Authorization": "Bearer clone-test",
        "X-Consent-Ref": "consent://test/1",
        "Idempotency-Key": "clone-bad",
    }
    body = {"name": "Bad", "files": [{"data": base64.b64encode(b"not audio data").decode(), "name": "x.wav", "type": "audio/wav"}]}
    assert client.post("/clone", json=body, headers=headers).status_code == 422


def test_body_limit(client):
    response = client.post("/tts", content=b"x" * (1024 * 1024 + 1), headers={**auth(), "Content-Type": "application/json"})
    assert response.status_code == 413


def test_invalid_audio_is_never_success():
    with pytest.raises(HTTPException) as caught:
        validate_audio(b"not-an-mp3")
    assert caught.value.status_code == 503


def test_migration_manifest_attests_full_contract():
    manifest = json.loads((Path(__file__).parents[1] / "migration_manifest.json").read_text(encoding="utf-8"))
    assert manifest["route_coverage"] == 1.0
    assert len(manifest["routes"]) == 4
    hashes = set(manifest["source_attestation"][key] for key in (
        "inventory_rescue_sha256",
        "forge_normalized_bundle_sha256",
        "canonical_git_blob_sha256",
    ))
    assert len(hashes) == 3
    assert manifest["source_attestation"]["equivalence_claimed"] is False
