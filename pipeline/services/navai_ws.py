"""NavAI gateway WebSocket clients for STT and TTS (ai.navai.pro).

The gateway's HTTP transcribe endpoints are disabled/unstable; STT and TTS
are served over WebSocket. Both flows are request/response-shaped here so
they slot into the existing batch pipeline (one complete utterance in → one
transcript out; one text in → full PCM out).

Auth: the API key goes in the `?api_key=` query param (nginx checks it at the
edge — browser WebSockets can't set headers, so the gateway uses the query
param for WS).

STT — wss://ai.navai.pro/api/stt/transcribe/live  (subprotocol "stt.v1")
  → send  {"type":"start","language":"uz","sample_rate":16000,"encoding":"pcm16"}
  → send  raw pcm16 binary frames
  → send  {"type":"end"}
  ← {"type":"speech_start"} / {"type":"interim","text":...} / {"type":"final","text":...}

TTS — wss://ai.navai.pro/api/tts/synthesize/ws
  Auth: `X-API-Key` header on the upgrade (matches NavAI-pro/navai-agent's
  navai_ws_tts provider; keeps the key out of logged URLs).
  → send  {"text":..., "voice_id":"muxlisa"}  plus optional "speed" (rate
          multiplier), "mode" (gateway currently enables only "local") and
          "reference_text"
  ← {"event":"start","sample_rate":24000,...} then binary pcm16 chunks then
    {"event":"done","chunks":N,"first_packet_ms":...}
  ← {"event":"error","detail":...} — e.g. text under 5 characters ("Text is
    too short ... at least 5 characters"), or a mode that isn't enabled.
"""

from __future__ import annotations

import asyncio
import json
import logging

import numpy as np
import websockets

from pipeline.audio import resample_pcm_int16
from pipeline.services.navai_text_norm import normalize_tts_text

log = logging.getLogger(__name__)

STT_SUBPROTOCOL = "stt.v1"

# The TTS gateway rejects text shorter than this with an `event:error`.
TTS_MIN_CHARS = 5
# Hard ceiling on one synthesis: guards a server that keeps streaming audio
# without ever sending `done`.
TTS_MAX_SECONDS = 60.0


def _with_key(ws_url: str, api_key: str) -> str:
    sep = "&" if "?" in ws_url else "?"
    return f"{ws_url}{sep}api_key={api_key}" if api_key else ws_url


async def transcribe_ws(
    pcm_int16_bytes: bytes,
    ws_url: str,
    api_key: str,
    language: str = "uz",
    sample_rate: int = 16000,
    timeout: float = 30.0,
    chunk_bytes: int = 2048,
) -> dict:
    """Stream one complete utterance to the live STT socket and return its
    final transcript as {"text": ..., "no_speech_prob": ...}."""
    url = _with_key(ws_url, api_key)
    text = ""
    try:
        async with websockets.connect(
            url, subprotocols=[STT_SUBPROTOCOL], open_timeout=8, close_timeout=3
        ) as ws:
            await ws.send(json.dumps({
                "type": "start", "language": language,
                "sample_rate": sample_rate, "encoding": "pcm16",
            }))
            for i in range(0, len(pcm_int16_bytes), chunk_bytes):
                await ws.send(pcm_int16_bytes[i:i + chunk_bytes])
            await ws.send(json.dumps({"type": "end"}))

            async def _drain():
                nonlocal text
                while True:
                    msg = await ws.recv()
                    if not isinstance(msg, str):
                        continue
                    d = json.loads(msg)
                    t = d.get("type")
                    if t == "final":
                        text = (d.get("text") or "").strip()
                        return
                    if t == "error":
                        log.warning(f"STT ws error: {d.get('message')}")

            try:
                await asyncio.wait_for(_drain(), timeout=timeout)
            except asyncio.TimeoutError:
                log.warning("STT ws: timed out waiting for final; using last interim")
            except websockets.ConnectionClosed:
                pass
    except Exception as e:
        log.warning(f"STT ws connect/transcribe failed: {e}")
        return {"text": "", "no_speech_prob": 1.0}

    log.info(f'ASR  "{text}"  (navai-ws, lang={language})')
    return {"text": text, "no_speech_prob": 0.0 if text else 1.0}


class StreamingSTT:
    """Live STT session: open on speech onset, feed pcm16 frames as they
    arrive, finalize on speech end. Because transcription overlaps the user's
    speech, `finish()` returns almost immediately instead of paying the full
    transcription cost after the utterance ends.

    All methods are defensive — any failure marks the session dead so the
    caller can fall back to batch transcription of the buffered audio.
    """

    def __init__(self, ws_url: str, api_key: str, language: str = "uz",
                 sample_rate: int = 16000):
        self._url = _with_key(ws_url, api_key)
        self._language = language
        self._sample_rate = sample_rate
        self.ws = None
        self._reader: asyncio.Task | None = None
        self._final: str | None = None
        self._interim: str = ""
        self.ok = False

    async def start(self) -> bool:
        try:
            self.ws = await websockets.connect(
                self._url, subprotocols=[STT_SUBPROTOCOL],
                open_timeout=6, close_timeout=2,
            )
            await self.ws.send(json.dumps({
                "type": "start", "language": self._language,
                "sample_rate": self._sample_rate, "encoding": "pcm16",
            }))
            self._reader = asyncio.create_task(self._read_loop())
            self.ok = True
            return True
        except Exception as e:
            log.warning(f"STT stream start failed: {e}")
            self.ok = False
            return False

    async def _read_loop(self):
        try:
            while True:
                msg = await self.ws.recv()
                if not isinstance(msg, str):
                    continue
                d = json.loads(msg)
                t = d.get("type")
                if t == "interim":
                    self._interim = (d.get("text") or "").strip()
                elif t == "final":
                    self._final = (d.get("text") or "").strip()
                elif t == "error":
                    log.warning(f"STT stream error: {d.get('message')}")
        except Exception:
            pass  # closed / cancelled

    async def feed(self, pcm_bytes: bytes):
        if not self.ok or self.ws is None:
            return
        try:
            await self.ws.send(pcm_bytes)
        except Exception as e:
            log.warning(f"STT stream feed failed: {e}")
            self.ok = False

    async def finish(self, timeout: float = 5.0) -> str | None:
        """Signal end-of-audio and return the final transcript, or None on
        failure (caller should fall back to batch)."""
        if not self.ok or self.ws is None:
            return None
        try:
            await self.ws.send(json.dumps({"type": "end"}))
            deadline = timeout
            step = 0.05
            waited = 0.0
            while self._final is None and waited < deadline:
                await asyncio.sleep(step)
                waited += step
            text = self._final if self._final is not None else self._interim
            return text
        except Exception as e:
            log.warning(f"STT stream finish failed: {e}")
            return None
        finally:
            await self.close()

    async def close(self):
        if self._reader:
            self._reader.cancel()
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
        self.ws = None
        self.ok = False


def prepare_tts_text(text: str, language: str = "uz") -> str:
    """Normalize text the way NavAI's own agent does before synthesis (Uzbek
    number/acronym spelling), and pad anything under the gateway's minimum
    with trailing dots so a one-word reply ("Ha.") is still spoken instead of
    rejected."""
    text = normalize_tts_text((text or "").strip(), language)
    if text and len(text) < TTS_MIN_CHARS:
        text = text.rstrip(".") + "." * (TTS_MIN_CHARS - len(text.rstrip(".")))
    return text


def _tts_request(
    text: str,
    voice_id: str,
    speed: float = 1.0,
    mode: str | None = None,
    reference_text: str | None = None,
) -> str:
    payload: dict = {"text": text, "voice_id": voice_id}
    if speed and float(speed) != 1.0:
        payload["speed"] = float(speed)
    if mode:
        payload["mode"] = mode
    if reference_text:
        payload["reference_text"] = reference_text
    return json.dumps(payload)


def _tts_headers(api_key: str) -> dict:
    return {"X-API-Key": api_key} if api_key else {}


async def synthesize_ws_stream(
    text: str,
    ws_url: str,
    api_key: str,
    voice_id: str = "muxlisa",
    target_rate: int = 16000,
    timeout: float = 60.0,
    speed: float = 1.0,
    mode: str | None = None,
    reference_text: str | None = None,
    language: str = "uz",
    max_seconds: float = TTS_MAX_SECONDS,
):
    """Async generator: yield PCM16 (mono, `target_rate`) chunks AS the TTS
    socket streams them, instead of buffering the whole sentence. The first
    chunk lands at ~0.5s vs ~1.4s for the full sentence, so the device starts
    playing much sooner. Each server chunk is resampled independently — the
    chunks are large (~1s), so per-chunk resampling is artefact-free.

    `timeout` bounds the gap between chunks; `max_seconds` bounds the whole
    synthesis."""
    text = prepare_tts_text(text, language)
    if not text:
        return
    request = _tts_request(text, voice_id, speed, mode, reference_text)
    queue: asyncio.Queue = asyncio.Queue()
    _DONE = object()

    async def _reader():
        src_rate = 24000
        try:
            async with asyncio.timeout(max_seconds):
                async with websockets.connect(
                    ws_url, additional_headers=_tts_headers(api_key),
                    open_timeout=8, close_timeout=3, max_size=None,
                ) as ws:
                    await ws.send(request)
                    while True:
                        msg = await ws.recv()
                        if isinstance(msg, str):
                            d = json.loads(msg)
                            ev = d.get("event")
                            if ev == "start":
                                src_rate = int(d.get("sample_rate", src_rate))
                            elif ev == "done":
                                log.debug(f"TTS navai-ws done: chunks={d.get('chunks')} "
                                          f"first_packet_ms={d.get('first_packet_ms')}")
                                break
                            elif ev == "error":
                                await queue.put(RuntimeError(f"TTS ws error: {d.get('detail')}"))
                                break
                        else:
                            pcm = bytes(msg)
                            if src_rate != target_rate and pcm:
                                pcm = resample_pcm_int16(pcm, src_rate, target_rate)
                            await queue.put(pcm)
        except websockets.ConnectionClosed:
            pass
        except TimeoutError:
            await queue.put(TimeoutError(f"TTS ws synthesis exceeded {max_seconds:.0f}s"))
        except Exception as e:
            await queue.put(e)
        finally:
            await queue.put(_DONE)

    task = asyncio.create_task(_reader())
    try:
        while True:
            item = await asyncio.wait_for(queue.get(), timeout=timeout)
            if item is _DONE:
                break
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        task.cancel()


async def synthesize_ws(text: str, ws_url: str, api_key: str, **kwargs) -> bytes:
    """Send text to the TTS socket and return the whole sentence as mono int16
    bytes at `target_rate`. Takes the same keyword options as
    `synthesize_ws_stream`."""
    chunks = [pcm async for pcm in synthesize_ws_stream(text, ws_url, api_key, **kwargs)]
    pcm = b"".join(chunks)
    log.debug(f"TTS navai-ws: {len(text)} chars -> {len(pcm)//2} samples")
    return pcm
