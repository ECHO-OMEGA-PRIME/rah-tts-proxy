# RAH TTS Proxy

Cloudflare Worker that routes text-to-speech requests for the Right at Home BnB voice assistant. Primary provider is ElevenLabs (for Steven's cloned voice), with automatic fallback to Edge TTS via Echo Speak Cloud when ElevenLabs is unavailable.

## Features

- **ElevenLabs TTS** -- Generate speech using ElevenLabs API with support for cloned voices (must use `eleven_multilingual_v2` model)
- **Edge TTS Fallback** -- Automatic fallback to Edge TTS (en-US-GuyNeural) via `echo-speak-cloud` Worker when ElevenLabs fails or is unconfigured
- **Multiple Voices** -- 4 pre-configured voices: Steven (cloned owner voice), Echo Prime, Bree, and Belle
- **Voice Cloning** -- Clone new voices by uploading base64-encoded audio samples to ElevenLabs Instant Voice Cloning API
- **Voice Settings** -- Configurable stability, similarity boost, and style parameters per request
- **Audio Validation** -- Rejects suspiciously small audio responses (<100 bytes) and falls back automatically
- **Structured JSON Logging** -- All operations logged with timestamps and context

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check (Steven voice status, ElevenLabs config) |
| `GET` | `/voices` | List available voices with availability status |
| `POST` | `/tts` | Generate speech from text |
| `POST` | `/clone` | Clone a new voice from audio samples |

### POST `/tts` Request Body

```json
{
  "text": "Welcome to Right at Home BnB!",
  "voice": "steven",
  "stability": 0.5,
  "similarity_boost": 0.75,
  "style": 0.0
}
```

Returns `audio/mpeg` binary response with headers `X-Voice` and `X-Provider`.

### POST `/clone` Request Body

```json
{
  "name": "Steven's Voice",
  "description": "Right at Home BnB owner voice clone",
  "files": [
    {
      "data": "<base64-encoded-audio>",
      "name": "sample1.mp3",
      "type": "audio/mpeg"
    }
  ]
}
```

Returns the new `voice_id` and instructions for setting it as a Worker secret.

## Configuration

### Environment Variables (`wrangler.toml`)

```toml
[vars]
DEFAULT_VOICE = "steven"
PROPERTY_NAME = "Right at Home BnB"
```

### Secrets (set via `wrangler secret put`)

| Secret | Description |
|--------|-------------|
| `ELEVENLABS_API_KEY` | ElevenLabs API key |
| `STEVEN_VOICE_ID` | Cloned voice ID for Steven (from `/clone` response) |
| `ECHO_API_KEY` | Echo API key for Edge TTS fallback |

### Voice Configuration

| Voice | ID | Model | Description |
|-------|----|-------|-------------|
| `steven` | Set via secret | `eleven_multilingual_v2` | Property owner's cloned voice |
| `echo` | `keDMh3sQlEXKM4EQxvvi` | `eleven_multilingual_v2` | Echo Prime AI voice |
| `bree` | `pzKXffibtCDxnrVO8d1U` | `eleven_multilingual_v2` | Bree personality voice |
| `belle` | `pzKXffibtCDxnrVO8d1U` | `eleven_multilingual_v2` | Belle (uses Bree voice) |

## Deployment

```bash
cd O:\ECHO_OMEGA_PRIME\WORKERS\rah-tts-proxy
npx wrangler deploy

# Set secrets
echo "API_KEY" | npx wrangler secret put ELEVENLABS_API_KEY
echo "VOICE_ID" | npx wrangler secret put STEVEN_VOICE_ID
echo "ECHO_KEY" | npx wrangler secret put ECHO_API_KEY

# Verify
curl -s https://rah-tts-proxy.bmcii1976.workers.dev/health
```

## Tech Stack

- **Runtime**: Cloudflare Workers
- **Language**: JavaScript (vanilla)
- **TTS Primary**: ElevenLabs API v1 (text-to-speech, voice cloning)
- **TTS Fallback**: Echo Speak Cloud Worker (Edge TTS, en-US-GuyNeural)
- **Audio Format**: MP3 (audio/mpeg)
