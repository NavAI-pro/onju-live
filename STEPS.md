# STEPS — from zero to talking to an Uzbek speaker

These are the exact steps that took a fresh macOS laptop + an unflashed M5 Stack ATOM Echo to a working Uzbek voice loop. Run them top to bottom. Estimated time: 20–30 minutes, most of which is `arduino-cli` installing the ESP32 core.

> The walkthrough assumes M5 Echo on macOS (Apple Silicon). For the custom Onjuino PCB, swap `m5_echo` for `onjuino` in every `flash.sh` line.

---

## 0. Prerequisites

* macOS 13+ (this guide is Apple-Silicon-tested; Intel works too).
* [Homebrew](https://brew.sh).
* Python 3.11 or newer. macOS ships with one; check with `python3 --version`.
* [`uv`](https://docs.astral.sh/uv/) for the Python venv. Install with `brew install uv` if you don't have it.
* A USB-C cable that can carry data (some cables are charge-only).
* A Wi-Fi network you know the password for. **The device only does 2.4 GHz** — if your network exposes separate SSIDs per band (e.g. `Mutolaa` and `Mutolaa_5G`), use the 2.4 GHz one.
* A Gemini API key. Get one free at <https://aistudio.google.com/apikey>.

---

## 1. Install system dependencies

```bash
brew install opus portaudio arduino-cli
```

`opus` and `portaudio` are needed by the Python pipeline. `arduino-cli` builds the firmware.

The Arduino toolchain bundles an old x86-only `ctags`. On Apple Silicon it won't run without Rosetta:

```bash
softwareupdate --install-rosetta --agree-to-license
```

(Skip if you're on Intel or you already have Rosetta.)

Install the ESP32 board core. This downloads ~400 MB and takes a few minutes:

```bash
arduino-cli core install esp32:esp32
```

---

## 2. Get the code

```bash
git clone git@github.com:NavAI-pro/onju-live.git
cd onju-live
```

---

## 3. Python pipeline

```bash
uv venv
source .venv/bin/activate
uv pip install -e .
```

This installs `google-genai`, `openai`, `silero-vad`, `opuslib`, etc.

Create your `.env` with the Gemini key:

```bash
cp .env.example .env
# edit .env, set GEMINI_API_KEY=your-key-here
```

Copy the example config:

```bash
cp pipeline/config.yaml.example pipeline/config.yaml
```

The example file is set up for the NavAI Uzbek pipeline by default (offline Uzbek STT + TTS, Gemini 3.1 Flash Lite). Edit `pipeline/config.yaml` only if you want a different backend or different STT/TTS hosts.

---

## 4. Firmware

Plug the M5 Echo into your Mac via USB-C. Check it shows up:

```bash
ls /dev/cu.usbserial-* /dev/cu.usbmodem* 2>/dev/null
```

You should see one device. The M5 Echo uses `/dev/cu.usbserial-XXXXXXXX`; the custom Onjuino uses `/dev/cu.usbmodem*`.

Flash. The script prompts for your Wi-Fi SSID and tries to pull the password from your macOS Keychain (Touch ID will prompt):

```bash
./flash.sh m5_echo
```

Pick the right SSID when asked. If Touch ID can't find the password (or you'd rather not), the script falls back to a manual prompt. If you want to skip Keychain entirely, edit `m5_echo/credentials.h` directly before running `./flash.sh` — the file template is `m5_echo/credentials.h.template`.

On success you'll see `Upload successful!` then a serial monitor opens. The device prints:

```
SSID: <your-network>
WiFi.............
IP: 192.168.1.15
Announced on multicast (PTT mode)
Opus decoder initialized
Device: m5-echo-XXXXXX @ 192.168.1.15 (<git-hash>)
Ready - push button to talk
```

If you see `WiFi.....` continuing forever, the credentials are wrong. Two ways out:

* Reflash with `./flash.sh m5_echo --regen` to re-enter credentials.
* Or fix on the fly via the device's serial config: in the serial monitor, type `c<enter>`, then `ssid YOURSSID<enter>`, `pass YOURPASSWORD<enter>`, `exit<enter>`. Device saves and reboots.

Press `Ctrl+C` to leave the serial monitor when you're happy.

---

## 5. Run the server

In a new terminal, in the same repo:

```bash
source .venv/bin/activate
./run.sh --warmup
```

`run.sh` handles the macOS `DYLD_FALLBACK_LIBRARY_PATH` quirk for `opuslib`. `--warmup` pings the LLM and TTS once to surface config mistakes immediately. Expect:

```
TTS  warmup OK  (0.4s) -> 700ms audio
LLM  warmup OK  (1.2s) -> 'Salom, ...'
Pipeline server starting
  ASR   http://34.32.153.192:8080
  LLM   conversational: gemini-3.1-flash-lite-preview @ ...
  TTS   navai_uz
UDP  listening on :3000
MCAST  listening on 239.0.0.1:12345
CTRL listening on :3002
```

If you see `LLM warmup FAILED`, your `GEMINI_API_KEY` isn't set or is wrong. If `TTS warmup FAILED`, the NavAI TTS host is unreachable from your laptop — check it's up.

---

## 6. Pair the device

The device announces itself by multicast on boot. The server will pick it up and you'll see in the log:

```
DEVICE  m5-echo-XXXXXX (192.168.1.15) PTT
```

**If you don't see that line**, your router probably doesn't bridge multicast between the 2.4 GHz band (device) and the 5 GHz band (laptop). Register manually:

```bash
curl -X POST http://127.0.0.1:3002/devices \
  -H "Content-Type: application/json" \
  -d '{"ip":"192.168.1.15","hostname":"m5-echo-XXXXXX","ptt":true}'
```

Replace the IP and hostname with what the device's serial output showed.

---

## 7. Talk to it

Hold the **top button** (the whole front face is the button) for ~3 seconds, speak, release. The server log will show:

```
PTT  end from m5-echo-XXXXXX (3.4s)
ASR  "salom yaxshimisiz"  (navai-batch, lang=uz)
LLM  Uzbek reply text
TTS  ...
SEND ...
```

And the speaker should reply in Uzbek with the muxlisa voice.

If audio is too quiet, in the serial monitor press `+` several times to raise volume (max 20). Or set `device.default_volume` higher in `pipeline/config.yaml`.

---

## 8. Common gotchas (recap)

| Symptom | Cause | Fix |
|---|---|---|
| `Error during build: ... ctags: bad CPU type` | x86 ctags on Apple Silicon, no Rosetta | `softwareupdate --install-rosetta --agree-to-license` |
| Device prints `WiFi....` forever | Wrong creds or 5 GHz SSID | Use a 2.4 GHz SSID; serial `c` command to reconfigure live |
| Server doesn't see the device | Router blocks cross-band multicast | `POST /devices` via the control endpoint |
| Device receives audio but plays clicks/silence | Old firmware before the buffer fix | Reflash from this repo |
| `LLM warmup FAILED` with 401/403 | `GEMINI_API_KEY` not loaded | Confirm `.env` exists at the repo root and the key isn't expired |
| `TTS warmup FAILED` timeout | TTS host (port 5000) not reachable | Check the NavAI TTS server is up; port 5000 (`/synthesize/local`) is the right one, not 8002 |
| `voice_id 'muxlisa' not found` from TTS | Sent voice_id as a form field | This repo already sends it as a query param; if you change the TTS code, keep it that way |

---

## 9. Day-to-day commands

```bash
# Start the server (auto-loads .env)
./run.sh

# Re-flash after firmware tweaks (no creds prompt if credentials.h exists)
./flash.sh m5_echo --no-monitor

# Reset / reconfigure the device without reflashing
python serial_monitor.py
# then in the monitor: 'r' = reboot, 'c' = enter config mode, '+/-' = volume

# Manually register a device that won't appear via multicast
curl -X POST http://127.0.0.1:3002/devices \
  -H "Content-Type: application/json" \
  -d '{"ip":"<device-ip>","hostname":"<hostname>","ptt":true}'

# List currently-registered devices
curl http://127.0.0.1:3002/devices

# Remove a device
curl -X DELETE http://127.0.0.1:3002/devices \
  -H "Content-Type: application/json" \
  -d '{"hostname":"<hostname>"}'
```

That's everything. If something still doesn't work, the server log at `INFO` level shows every stage of every turn — read it before guessing.
