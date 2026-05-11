# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Onju Voice v2 (OnjuClaw) — a server pipeline plus ESP32 firmware that turns a Google-Nest-Mini-form-factor speaker (custom PCB `onjuino` or off-the-shelf `m5_echo`) into a voice front-end for an LLM or agent. Devices stream mu-law audio over UDP to the server; the server runs VAD → ASR → LLM → TTS → Opus → TCP back to the speaker.

Read `README.md` for protocol details and `pipeline/config.yaml.example` for every tunable. See also `m5_echo/README.md` for the M5 Echo target's quirks (PDM mic, no PSRAM, I2S TX/RX switching).

## Common commands

```bash
# Server
./run.sh                           # runs `python -m pipeline.main` with DYLD_FALLBACK_LIBRARY_PATH set for opuslib on macOS
./run.sh --warmup                  # warmup LLM + TTS before accepting devices
./run.sh --device onju=10.0.0.5    # pre-register a device (skips multicast discovery)

# ASR (separate process, Apple Silicon only)
python -m pipeline.services.asr_server                     # default port 8100
ASR_MODEL=mlx-community/parakeet-tdt-0.6b-v3 python -m pipeline.services.asr_server

# Firmware
./flash.sh                         # onjuino (default)
./flash.sh m5_echo
./flash.sh compile                 # no upload, no device needed
./flash.sh --regen                 # regenerate WiFi creds from macOS Keychain

# Tests (no pytest — these are standalone scripts run from repo root)
python tests/test_client.py [server-ip]     # emulate an ESP32
python tests/test_speaker.py <device-ip>    # send a WAV to a device
python tests/test_mic.py --duration 10      # receive UDP audio from a device
python tests/test_stall.py                  # benchmark stall classifier
python tests/test_stream.py ["prompt"]      # inspect raw SSE from agentic gateway

# Serial monitor (auto-detects USB)
python serial_monitor.py

# Optional: configure an OpenClaw gateway as the agentic backend
./setup_openclaw.sh                # toggles HTTP endpoint, appends voice prompt to ~/.openclaw/workspace/AGENTS.md, restarts gateway
```

Install: `uv venv && source .venv/bin/activate && uv pip install -e .` plus `brew install opus portaudio` on macOS. ASR extras: `uv pip install -e ".[asr]"`. Local TTS extras: `uv pip install -e ".[tts-local]"`. There is no lint or formatter configured and no test runner — tests are scripts.

## Architecture

### Async pipeline (`pipeline/main.py`)

Four tasks run concurrently from `main()`:

1. `multicast_listener` — receives `<hostname> <git-hash> [PTT]` announcements on `239.0.0.1:12345`, creates a `Device` via `DeviceManager`, and sends a greeting + LED pulse (which is how the device first learns the server's IP).
2. `udp_listener` — receives mu-law frames from devices on UDP 3000. For VOX devices it runs `VAD.process_frame` per 32 ms / 512-sample frame; for PTT devices it buffers raw PCM and flushes when the packet stream stops (`ptt_timeout = 0.5s`). Both paths push complete utterances onto `utterance_queue`.
3. `process_utterances` — the main turn loop: ASR → optional stall classifier (agentic only) → streaming LLM → `sentence_chunks` splitter → TTS → Opus → TCP. The whole turn is wrapped in `device.processing = True` plus an `asyncio.Event` named `interrupted` that lets a fresh utterance or new speech kill the in-flight turn.
4. `control_server` — tiny hand-rolled HTTP server on `control_port` (default 3002) exposing `GET/POST/DELETE /devices` for manually adding/removing devices without going through multicast.

### Streaming + stall handshake

The most subtle code in this repo. `process_utterances` keeps a `pending` sentence — non-final sentences are sent with `mic_timeout=0` so the device doesn't reopen the mic between chunks, and only the *last* sentence at end-of-stream is sent with the full `default_mic_timeout`. If the LLM returns nothing or fails after partial audio was sent, `reopen_mic_if_needed()` sends an empty audio frame to restore mic state.

In agentic mode, before opening the main LLM stream the pipeline calls `stall.decide_stall` (a fast Gemini classifier; see `pipeline/conversation/stall.py` and the `conversation.stall.prompt` in config). If it returns a phrase, that phrase is TTS'd and sent in parallel with the main agent call, AND injected back into the agent's user message as a parenthetical so the agent doesn't repeat it. This exists specifically because OpenClaw's OpenAI-compat endpoint buffers all first-turn content until the first tool round completes — without the stall the user hears silence for 5–60+ seconds. Don't remove the stall path without understanding this constraint.

### Conversation backends (`pipeline/conversation/`)

Three backends, selected by `conversation.backend` in `config.yaml`:

- `ConversationalBackend` — manages full message history client-side, persists per-device to `data/conversations/<hostname>.json` if `persist_dir` is set. `_sanitize` enforces strict user/assistant alternation after the system prompt (some endpoints reject otherwise). Assistant turn is appended **only after successful TTS delivery** via `commit()` — see commit `44c7be0`. Don't move `_finalize` back to inside `send()`/`stream()`.
- `AgenticBackend` — sends only the latest user message; the remote gateway (OpenClaw) owns history keyed by `device_id`. Sends an `x-openclaw-message-channel` header (default `onju-voice`) and optional `x-openclaw-model` if `provider_model` is set. `commit()`/`reset()` are no-ops.
- `GeminiLiveBackend` — end-to-end audio over a WebSocket to the Gemini Live API. **Does not use ASR or TTS** — the utterance's PCM bytes go straight into the model and Gemini's PCM bytes come straight out. Each device keeps one persistent Live session with a background task draining `session.receive()` into a queue; each turn calls `send_client_content(turns=Content(parts=[Part(inline_data=Blob(audio_pcm))]), turn_complete=True)` and drains the queue until `server_content.turn_complete`. Input is 16 kHz PCM, output is 24 kHz PCM — resampled to 16 kHz via `audio.resample_pcm_int16` before Opus encoding. Use the `gemini-3.1-flash-live-preview` model (half-cascade, ~0.6 s first audio, no thought/text leakage). The `native-audio-*` models mix `thought` and `text` parts into the response and shouldn't be used here.

Conversational and Agentic backends expose `send()` (non-streaming) and `stream()` (yields content deltas) — Live exposes `converse(pcm)` instead and `process_utterances` branches on `live_mode` at the top to skip the ASR/TTS path entirely. `sentence_chunks` in `pipeline/conversation/__init__.py` buffers deltas and yields complete sentences using two regexes: punctuation+whitespace is the primary boundary, and a secondary regex catches OpenClaw's "now.The" chunk-boundary joins without breaking abbreviations like "U.S.".

### TCP command protocol

Server → device, 6-byte header (see `pipeline/protocol.py`):

| Byte 0 | Command | Payload |
|---|---|---|
| `0xAA` | Audio | `mic_timeout(2B) | volume | fade | compression(=2 Opus)` then length-prefixed Opus frames; `0x00 0x00` frame = end |
| `0xBB` | Set LEDs | LED bitmask + RGB |
| `0xCC` | LED blink | intensity + RGB + fade rate |
| `0xDD` | Mic timeout only | 2-byte hold seconds |

LED blinks during VAD use a *persistent* TCP connection (`open_led_connection` / `write_led_blink` / `close_led_connection`) so we don't pay TCP setup cost at 40 Hz. The connection is opened when VAD starts recording and closed when the utterance is queued. If you see `device.vad_writer` references, that's why.

### Audio formats

- Upstream: mu-law @ 16 kHz over UDP, 512 samples (32 ms) per packet.
- Downstream: Opus @ 16 kHz over TCP, 320-sample frames (20 ms). `pipeline/audio.py` has `opus_encode` and `opus_frames_to_tcp_payload` (length-prefix each frame, terminate with a zero-length frame).
- `chunk_size: 512` in config must match the ESP32 firmware's `SAMPLE_CHUNK_SIZE`. `opus_frame_size: 320` is one of Opus's valid frame sizes at 16 kHz; don't pick arbitrary values.

### VAD (`pipeline/vad.py`)

Wraps the Silero VAD v5 ONNX model directly (loaded from the `silero-vad` package's data dir). Stateful per device: keeps LSTM `_state` (2,1,128) and audio `_context` (1,64) between frames. Uses hysteresis (`threshold` vs `neg_threshold`) plus a `silence_time` count of frames-below-neg-threshold to detect end of utterance. There's a `pre_buffer` deque so the first ~1 s of audio before VAD trips is included in the utterance. Frame size **must** be 512 samples — that's a Silero requirement.

### Firmware

- `onjuino/onjuino.ino` (ESP32-S3, ~1300 lines): I2S in + out simultaneously, capacitive touch (tap to start, VAD ends), 6 NeoPixels, 2 MB PSRAM playback buffer, mu-law upstream + Opus downstream.
- `m5_echo/m5_echo.ino` (ESP32-PICO-D4, ~880 lines): PDM mic + I2S speaker share pins so the driver switches modes between PTT-held (mic) and idle (speaker). No PSRAM — uses more DMA buffers. Decoder only (mic still sends mu-law). Hold-to-talk physical button on GPIO 39.
- FreeRTOS layout: Core 0 runs the Arduino loop (TCP server, input, UART). Core 1 runs `micTask`, `opusDecodeTask` (created per playback), and `updateLedTask` (40 Hz, gamma-corrected fade).
- Both targets emit a multicast announcement `<hostname> <git-hash> [PTT]` to `239.0.0.1:12345` on boot. The `PTT` token is what makes the server treat the device as push-to-talk.
- `git_hash.h` is regenerated by `flash.sh` on every flash (only rewritten when changed to avoid recompile churn). `credentials.h` is generated from `credentials.h.template` using your macOS Keychain WiFi password.

### Config conventions

- `config.yaml` is gitignored; only `config.yaml.example` is checked in. Always read the example for the full schema — many fields have non-obvious effects (e.g. `reasoning_effort: "none"` is what disables Gemini 2.5 thinking for sub-second latency).
- API keys can be inlined or use `${ENV_VAR}` syntax, resolved by `_resolve_env` in the backend constructors.
- Env vars surfaced by the README: `OPENROUTER_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENCLAW_GATEWAY_TOKEN`.

## Gotchas worth knowing before editing

- **Don't move history persistence**: `ConversationalBackend.commit()` is called from `process_utterances` only after the final TTS chunk is delivered. Persisting earlier means failed turns get saved as if they succeeded.
- **PTT vs VOX paths diverge in `udp_listener`**: PTT devices ignore VAD and flush on packet-stream-stop; VOX devices ignore the buffer flush path. Don't unify them naively.
- **`mic_timeout=0` is load-bearing**: it's how mid-stream Opus chunks avoid prematurely reopening the mic. After any partial send, you must eventually send either a final chunk with non-zero timeout or an empty-audio mic-reopen.
- **First-turn OpenClaw buffering**: covered above — the stall path is the workaround, not a nice-to-have.
- **Silero VAD frame size is fixed at 512**: matches `audio.chunk_size`. Don't change one without the other and without verifying the model still accepts it.
- **opuslib on macOS**: needs `DYLD_FALLBACK_LIBRARY_PATH` to find Homebrew's libopus. `run.sh` sets this; bare `python -m pipeline.main` from a fresh shell on macOS won't work without it.
- **m5_echo I2S sample rates are doubled/halved on purpose**: see `m5_echo/README.md` "I2S quirks" — the ESP32-PICO-D4 driver treats DMA as stereo-interleaved, so 16 kHz mono is `SAMPLE_RATE/2` for TX and `SAMPLE_RATE*2` for RX with every-other-sample deinterleaving.
