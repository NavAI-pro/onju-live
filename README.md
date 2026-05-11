# onju-live

An Uzbek voice assistant in a Google-Nest-Mini form factor. Hold a button, speak Uzbek, hear an Uzbek answer.

Fork of [onju-voice](https://github.com/justLV/onju-voice) / [onju-v2](https://github.com/justLV/onju-v2) with:

* Gemini Live end-to-end audio backend (`pipeline/conversation/gemini_live.py`) — voice in, voice out, no transcription stage.
* Conversational backend wired to NavAI's offline Uzbek STT + TTS on GCP, with Gemini 3.1 Flash Lite as the LLM. This is the default — works fully offline-of-Google for the speech parts.
* M5 Stack ATOM Echo firmware fixes (Opus stream handling, bigger playback buffer).

If you want to know **what to do, step by step**, read [`STEPS.md`](STEPS.md).
The notes below are for understanding the architecture before you touch code.

## Supported hardware

| | Onjuino (custom PCB) | M5 Stack ATOM Echo |
|---|---|---|
| **Board** | ESP32-S3 | ESP32-PICO-D4 |
| **Interaction** | Capacitive touch: tap to start (server VAD ends) | Physical button: hold to talk |
| **Mic** | I2S INMP441 | PDM SPM1423 |
| **Speaker** | MAX98357A, 6 NeoPixel LEDs | NS4168, 1 SK6812 LED |
| **Wi-Fi** | 2.4 GHz | 2.4 GHz only |

Both flash with `./flash.sh [target]` and speak the same network protocol.

## Architecture

```
                ESP32 Device                              Server
  ┌──────────────────────────────┐       ┌──────────────────────────────────────┐
  │  Mic > I2S RX > mu-law =======UDP 3000===> mu-law decode > VAD/PTT > ASR    │
  │                              │       │                                      │
  │  Speaker < I2S TX < Opus <===TCP 3001<=== Opus encode < TTS < LLM           │
  └──────────────────────────────┘       └──────────────────────────────────────┘
```

* **Upstream** (device → server): mu-law @ 16 kHz over UDP, 512 samples/packet. Stateless, cheap on the ESP32.
* **Downstream** (server → device): Opus @ 16 kHz over **one persistent TCP per turn**, length-prefixed frames, zero-length terminator at end of speech. Persistent TCP is load-bearing — see `CLAUDE.md` for why one-stream-per-chunk caused audible clicks.
* **Discovery**: device multicasts `<hostname> <git-hash> [PTT]` to `239.0.0.1:12345` on boot. Server greets back; device learns server IP from the inbound TCP and saves it to NVS. If your router doesn't bridge multicast across 2.4/5 GHz bands, register manually with `POST /devices`.

## Three conversation backends

Selected by `conversation.backend` in `pipeline/config.yaml`:

* **`conversational`** (default) — text-based ASR → LLM → TTS. Configured for NavAI's Uzbek STT (`/transcribe/transcribe-batch`, lang=uz) + NavAI's Uzbek TTS (`/synthesize/local`, `voice_id` as a **query** param) + Gemini 3.1 Flash Lite. Replies always in Uzbek regardless of the input language via the `system_prompt`.
* **`agentic`** — OpenClaw gateway with server-side tool execution. Sentence-level streaming, stall classifier for slow turns. See upstream docs.
* **`gemini_live`** — Gemini Live WebSocket. PCM in, PCM out, no ASR/TTS step. Lowest latency (~1.7 s to first audio) but the model picks the language; the `system_instruction` should pin it.

See `pipeline/config.yaml.example` for every field.

## Repo layout

```
pipeline/        Async server (UDP listener, VAD, ASR, LLM, TTS, TCP push)
  conversation/  Three pluggable backends
  services/      asr.py, tts.py, asr_server.py (optional parakeet)
m5_echo/         ATOM Echo firmware (.ino)
onjuino/         Custom-PCB firmware (.ino)
tests/           Standalone scripts (test_client emulates a device on the laptop)
hardware/        Altium PCB source for the custom board
```

## License

MIT. Original work by [justLV](https://github.com/justLV).
