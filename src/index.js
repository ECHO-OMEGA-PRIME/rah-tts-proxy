/* ═══════════════════════════════════════════════════════════
   RAH TTS PROXY — Right at Home BnB Voice Assistant
   Routes TTS requests to ElevenLabs for Steven's voice
   Falls back to Edge TTS if ElevenLabs unavailable
   ═══════════════════════════════════════════════════════════ */

// Voice configuration — MUST use eleven_multilingual_v2 for cloned voices
const VOICES = {
  steven: { id: null, model: 'eleven_multilingual_v2', label: "Steven's Voice" },
  echo:   { id: 'keDMh3sQlEXKM4EQxvvi', model: 'eleven_multilingual_v2', label: 'Echo Prime' },
  bree:   { id: 'pzKXffibtCDxnrVO8d1U', model: 'eleven_multilingual_v2', label: 'Bree' },
  belle:  { id: 'pzKXffibtCDxnrVO8d1U', model: 'eleven_multilingual_v2', label: 'Belle (Bree voice)' },
};

const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type, Authorization, X-Echo-API-Key',
};

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json', ...CORS },
  });
}

function log(level, msg, extra = {}) {
  console.log(JSON.stringify({ ts: new Date().toISOString(), level, worker: 'rah-tts-proxy', msg, ...extra }));
}

export default {
  async fetch(request, env) {
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: CORS });
    }

    const url = new URL(request.url);
    const path = url.pathname;

    try {
      if (path === '/health') return handleHealth(env);
      if (path === '/voices') return handleVoices(env);
      if (path === '/tts' && request.method === 'POST') return handleTTS(request, env);
      if (path === '/clone' && request.method === 'POST') return handleClone(request, env);

      return json({ error: 'Not found', endpoints: ['/health', '/voices', '/tts', '/clone'] }, 404);
    } catch (err) {
      log('error', 'Unhandled error', { error: err.message, stack: err.stack });
      return json({ error: 'Internal server error' }, 500);
    }
  },
};

// ── Health ──────────────────────────────────────────────
function handleHealth(env) {
  const stevenVoiceId = env.STEVEN_VOICE_ID || null;
  return json({
    status: 'ok',
    version: '1.0.0',
    service: 'rah-tts-proxy',
    property: env.PROPERTY_NAME || 'Right at Home BnB',
    steven_voice: stevenVoiceId ? 'configured' : 'not_configured',
    elevenlabs: env.ELEVENLABS_API_KEY ? 'configured' : 'not_configured',
    timestamp: new Date().toISOString(),
  });
}

// ── List available voices ──────────────────────────────
function handleVoices(env) {
  const stevenId = env.STEVEN_VOICE_ID || null;
  const voices = Object.entries(VOICES).map(([key, v]) => ({
    id: key,
    voice_id: key === 'steven' ? stevenId : v.id,
    label: v.label,
    available: key === 'steven' ? !!stevenId : !!v.id,
  }));
  return json({ voices, default: env.DEFAULT_VOICE || 'steven' });
}

// ── Text-to-Speech ─────────────────────────────────────
async function handleTTS(request, env) {
  const body = await request.json().catch(() => null);
  if (!body || !body.text) {
    return json({ error: 'Missing "text" field' }, 400);
  }

  const text = body.text.slice(0, 2000);
  const voiceKey = body.voice || env.DEFAULT_VOICE || 'steven';
  const voiceConfig = VOICES[voiceKey] || VOICES.steven;

  // Resolve voice ID
  let voiceId = voiceConfig.id;
  if (voiceKey === 'steven') {
    voiceId = env.STEVEN_VOICE_ID || null;
  }

  if (!voiceId) {
    log('warn', 'No voice ID configured, falling back to edge TTS', { voice: voiceKey });
    return fallbackEdgeTTS(text, env);
  }

  if (!env.ELEVENLABS_API_KEY) {
    log('warn', 'No ElevenLabs API key, falling back to edge TTS');
    return fallbackEdgeTTS(text, env);
  }

  const model = voiceConfig.model || 'eleven_multilingual_v2';

  log('info', 'Generating TTS', { voice: voiceKey, voice_id: voiceId, model, text_length: text.length });

  try {
    const resp = await fetch(`https://api.elevenlabs.io/v1/text-to-speech/${voiceId}`, {
      method: 'POST',
      headers: {
        'xi-api-key': env.ELEVENLABS_API_KEY,
        'Content-Type': 'application/json',
        'Accept': 'audio/mpeg',
      },
      body: JSON.stringify({
        text,
        model_id: model,
        voice_settings: {
          stability: body.stability || 0.5,
          similarity_boost: body.similarity_boost || 0.75,
          style: body.style || 0.0,
          use_speaker_boost: true,
        },
      }),
    });

    if (!resp.ok) {
      const errText = await resp.text().catch(() => 'unknown');
      log('error', 'ElevenLabs API error', { status: resp.status, error: errText });
      return fallbackEdgeTTS(text, env);
    }

    const audioData = await resp.arrayBuffer();

    // Validate we got real audio (not empty)
    if (audioData.byteLength < 100) {
      log('warn', 'ElevenLabs returned tiny audio, falling back', { bytes: audioData.byteLength });
      return fallbackEdgeTTS(text, env);
    }

    log('info', 'TTS generated successfully', { bytes: audioData.byteLength, voice: voiceKey });

    return new Response(audioData, {
      status: 200,
      headers: {
        'Content-Type': 'audio/mpeg',
        'Content-Length': audioData.byteLength.toString(),
        'X-Voice': voiceKey,
        'X-Provider': 'elevenlabs',
        ...CORS,
      },
    });
  } catch (err) {
    log('error', 'ElevenLabs request failed', { error: err.message });
    return fallbackEdgeTTS(text, env);
  }
}

// ── Edge TTS fallback ──────────────────────────────────
async function fallbackEdgeTTS(text, env) {
  // Use echo-speak-cloud as fallback (Edge TTS provider)
  try {
    const resp = await fetch('https://echo-speak-cloud.bmcii1976.workers.dev/tts', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Echo-API-Key': env.ECHO_API_KEY || '',
      },
      body: JSON.stringify({
        text,
        voice: 'edge',
        edge_voice: 'en-US-GuyNeural',
      }),
    });

    if (resp.ok) {
      const audio = await resp.arrayBuffer();
      log('info', 'Edge TTS fallback succeeded', { bytes: audio.byteLength });
      return new Response(audio, {
        status: 200,
        headers: {
          'Content-Type': 'audio/mpeg',
          'X-Voice': 'edge-fallback',
          'X-Provider': 'edge-tts',
          ...CORS,
        },
      });
    }
  } catch (err) {
    log('error', 'Edge TTS fallback failed', { error: err.message });
  }

  return json({ error: 'TTS unavailable — both ElevenLabs and Edge TTS failed' }, 503);
}

// ── Voice Cloning ──────────────────────────────────────
async function handleClone(request, env) {
  if (!env.ELEVENLABS_API_KEY) {
    return json({ error: 'ElevenLabs API key not configured' }, 503);
  }

  const body = await request.json().catch(() => null);
  if (!body || !body.files || !body.files.length) {
    return json({ error: 'Missing "files" array with base64 audio samples' }, 400);
  }

  const name = body.name || "Steven's Voice";
  const description = body.description || 'Right at Home BnB owner voice clone';

  log('info', 'Starting voice clone', { name, file_count: body.files.length });

  try {
    const formData = new FormData();
    formData.append('name', name);
    formData.append('description', description);

    for (const file of body.files) {
      const bytes = Uint8Array.from(atob(file.data), c => c.charCodeAt(0));
      const blob = new Blob([bytes], { type: file.type || 'audio/mpeg' });
      formData.append('files', blob, file.name || 'sample.mp3');
    }

    const resp = await fetch('https://api.elevenlabs.io/v1/voices/add', {
      method: 'POST',
      headers: { 'xi-api-key': env.ELEVENLABS_API_KEY },
      body: formData,
    });

    const result = await resp.json();

    if (!resp.ok) {
      log('error', 'Voice clone failed', { status: resp.status, error: result });
      return json({ error: 'Clone failed', detail: result }, resp.status);
    }

    log('info', 'Voice cloned successfully', { voice_id: result.voice_id, name });

    return json({
      success: true,
      voice_id: result.voice_id,
      name,
      provider: 'elevenlabs',
      model: 'eleven_multilingual_v2',
      instructions: `Set STEVEN_VOICE_ID=${result.voice_id} as a Worker secret: echo "${result.voice_id}" | npx wrangler secret put STEVEN_VOICE_ID`,
    });
  } catch (err) {
    log('error', 'Voice clone error', { error: err.message, stack: err.stack });
    return json({ error: 'Clone request failed', detail: err.message }, 500);
  }
}
