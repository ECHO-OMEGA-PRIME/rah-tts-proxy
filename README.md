# RAH TTS Proxy

Private FORGE replacement for the retired Cloudflare Worker contract used by the Right at Home BnB voice assistant. It preserves `/health`, `/voices`, `/tts`, and `/clone` while delegating synthesis and cloning to the canonical Echo Voice Gateway on loopback.

## Security contract

- `/health` is public and metadata-only.
- `/voices` and `/tts` require `Authorization: Bearer <service token>`.
- `/clone` also requires `X-Clone-Authorization`, `X-Consent-Ref`, and `Idempotency-Key`.
- Browser access is restricted to configured RAH origins. All responses include no-store and security headers.
- Text, audio, provider identifiers, credentials, and upstream response bodies are never logged.
- TTS concurrency is four; clone concurrency is one; payloads, provider responses, and rates are bounded.
- Provider credentials stay in the Voice Gateway. This adapter receives only inbound tokens and an alias-to-voice map through systemd credentials.

## Run and verify

```bash
python -m pip install -r requirements.txt
pytest -q
uvicorn app:app --host 127.0.0.1 --port 8472
python smoke_live.py --base http://127.0.0.1:8472 --expect-build development
```

The smoke suite is intentionally no-spend: it exercises health, authentication negatives, exact CORS, security headers, 404/405 behavior, and deployed-build identity without synthesizing or cloning audio.

## Deployment

`deploy.sh` creates an immutable release under `/opt/rah-tts-proxy`, compiles and tests it, boots the exact release on staging port `18472`, runs the no-spend smoke, atomically promotes `current`, then smokes production port `8472`. Any failed post-promotion smoke restores the prior symlink and verifies rollback. Set `FORCE_POST_PROMOTE_FAILURE=1` once during commissioning to produce deterministic rollback evidence.

Credential files are provisioned outside Git under `/etc/echo/credentials/rah-tts-proxy/` and attached with `LoadCredential=`. Do not put credential values in environment files, shell history, deployment output, or reports.

## Migration evidence

[`migration_manifest.json`](migration_manifest.json) pins the inventory rescue hash, the recovered FORGE bundle hash, and the canonical Git source hash independently; it does not falsely claim those artifacts are byte-equivalent. It records 4/4 route coverage, the unused Analytics binding retirement, absence of scheduled handlers, and every deliberate security delta.

The legacy Worker files remain in `src/index.js` and `wrangler.toml` as immutable provenance. They are not deployed.
