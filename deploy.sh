#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=/opt/rah-tts-proxy
SOURCE_DIR=${SOURCE_DIR:-$(cd "$(dirname "$0")" && pwd)}
COMMIT=${COMMIT:-$(git -C "$SOURCE_DIR" rev-parse HEAD)}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
RELEASE="$ROOT/releases/${STAMP}-${COMMIT:0:12}"
PREVIOUS=""
if [[ -L "$ROOT/current" ]]; then
  PREVIOUS=$(readlink -f "$ROOT/current" 2>/dev/null || true)
fi
STAGE_PID=""

cleanup() {
  if [[ -n "$STAGE_PID" ]]; then kill "$STAGE_PID" 2>/dev/null || true; fi
}
trap cleanup EXIT

install -d -m 0755 "$ROOT/releases"
install -d -m 0755 "$RELEASE"
git -C "$SOURCE_DIR" archive "$COMMIT" | tar -x -C "$RELEASE"
printf '%s\n' "$COMMIT" >"$RELEASE/BUILD_SHA"
"$ROOT/venv/bin/python" -m compileall -q "$RELEASE"
(
  cd "$RELEASE"
  "$ROOT/venv/bin/python" -m pytest -q tests
)

RAH_TTS_BUILD_SHA="$COMMIT" RAH_TTS_CALLBACK_BASE_URL=http://127.0.0.1:18472 RAH_TTS_STATE_DB="$ROOT/staging-state.db" "$ROOT/venv/bin/python" -m uvicorn app:app --app-dir "$RELEASE" --host 127.0.0.1 --port 18472 >"$ROOT/staging.log" 2>&1 &
STAGE_PID=$!
for _ in $(seq 1 40); do curl -fsS http://127.0.0.1:18472/health >/dev/null && break; sleep 0.25; done
"$ROOT/venv/bin/python" "$RELEASE/smoke_live.py" --base http://127.0.0.1:18472 --expect-build "$COMMIT"
kill "$STAGE_PID"; STAGE_PID=""

ln -sfn "$RELEASE" "$ROOT/current.next"
mv -Tf "$ROOT/current.next" "$ROOT/current"
systemctl restart echo-rah-tts-proxy.service
if [[ "${FORCE_POST_PROMOTE_FAILURE:-0}" == "1" ]] || ! "$ROOT/venv/bin/python" "$RELEASE/smoke_live.py" --base http://127.0.0.1:8472 --expect-build "$COMMIT"; then
  if [[ -n "$PREVIOUS" && -d "$PREVIOUS" ]]; then
    ln -sfn "$PREVIOUS" "$ROOT/current.next"
    mv -Tf "$ROOT/current.next" "$ROOT/current"
    systemctl restart echo-rah-tts-proxy.service
    "$ROOT/venv/bin/python" "$PREVIOUS/smoke_live.py" --base http://127.0.0.1:8472
  fi
  echo "promotion rolled back" >&2
  exit 1
fi
echo "$RELEASE"
