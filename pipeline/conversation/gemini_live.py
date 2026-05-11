"""Gemini Live API backend — end-to-end audio (no ASR / no TTS).

The session is bidirectional: PCM 16 kHz int16 in, PCM 24 kHz int16 out.
We keep one WebSocket open per device for the lifetime of the device so
Gemini retains conversation context across turns. A background task drains
`session.receive()` into a queue; each turn pushes audio in, then drains
the queue until the server signals `turn_complete`.
"""

import asyncio
import logging
import os
import re
from typing import AsyncIterator

from google import genai
from google.genai import types

log = logging.getLogger(__name__)

# Send the user's utterance in ~100 ms chunks. Gemini Live recommends small
# chunks for realtime input.
_SEND_CHUNK_MS = 100
_INPUT_RATE = 16000
_INPUT_BYTES_PER_CHUNK = _INPUT_RATE * 2 * _SEND_CHUNK_MS // 1000


def _resolve_env(value: str) -> str:
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), value)


class GeminiLiveBackend:
    """Persistent Gemini Live session per device. Sends PCM utterances and
    yields PCM response audio chunks until the server says the turn is done."""

    def __init__(self, cfg: dict, device_id: str):
        self.cfg = cfg
        self.device_id = device_id
        api_key = _resolve_env(cfg.get("api_key", ""))
        if not api_key or api_key.startswith("${"):
            log.warning(f"[{device_id}] Gemini Live api_key not resolved: {api_key!r}")

        self.client = genai.Client(
            api_key=api_key,
            http_options={"api_version": "v1beta"},
        )
        self.model = cfg.get("model", "models/gemini-3.1-flash-live-preview")
        self.live_config = self._build_live_config(cfg)

        self._session = None
        self._session_ctx = None
        self._recv_task: asyncio.Task | None = None
        self._response_queue: asyncio.Queue | None = None
        self._connect_lock = asyncio.Lock()
        self._closed = False

    @staticmethod
    def _build_live_config(cfg: dict) -> types.LiveConnectConfig:
        kwargs: dict = {
            "response_modalities": [types.Modality.AUDIO],
        }
        if (sys_prompt := cfg.get("system_instruction")):
            kwargs["system_instruction"] = types.Content(
                parts=[types.Part(text=sys_prompt)],
                role="user",
            )
        if (voice := cfg.get("voice_name")):
            kwargs["speech_config"] = types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
                )
            )
        if (temp := cfg.get("temperature")) is not None:
            kwargs["temperature"] = temp
        return types.LiveConnectConfig(**kwargs)

    async def _ensure_connected(self) -> None:
        async with self._connect_lock:
            if self._session is not None:
                return
            log.info(f"[{self.device_id}] Opening Gemini Live session ({self.model})")
            self._session_ctx = self.client.aio.live.connect(
                model=self.model, config=self.live_config
            )
            self._session = await self._session_ctx.__aenter__()
            self._response_queue = asyncio.Queue()
            self._recv_task = asyncio.create_task(self._recv_loop())

    async def _recv_loop(self) -> None:
        try:
            async for msg in self._session.receive():
                await self._response_queue.put(msg)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(f"[{self.device_id}] Live recv loop exited: {e}")
        finally:
            # Signal disconnect to any in-flight converse().
            if self._response_queue is not None:
                await self._response_queue.put(None)

    async def close(self) -> None:
        self._closed = True
        if self._recv_task is not None:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):
                pass
            self._recv_task = None
        if self._session_ctx is not None:
            try:
                await self._session_ctx.__aexit__(None, None, None)
            except Exception:
                pass
        self._session = None
        self._session_ctx = None
        self._response_queue = None

    async def converse(self, pcm_16khz_int16: bytes) -> AsyncIterator[bytes]:
        """Send one utterance, yield response PCM chunks (24 kHz int16) until
        the server signals turn_complete. Reconnects once on transport errors.

        Uses send_client_content (turn-based) rather than send_realtime_input
        (server-VAD-based) because we already have a fully-segmented
        utterance — relying on Gemini's VAD to detect end-of-speech is
        unreliable when our own VAD has already trimmed trailing silence.

        Bounded waits: if no response message arrives within `recv_timeout_s`
        the session is presumed dead (idle-timed-out or dropped) and we
        reconnect for the next turn instead of hanging the whole pipeline.
        """
        recv_timeout_s = float(self.cfg.get("recv_timeout_s", 20.0))
        for attempt in range(2):
            try:
                await self._ensure_connected()
                # Drain any stale messages from a prior turn before sending.
                while self._response_queue and not self._response_queue.empty():
                    self._response_queue.get_nowait()

                turn = types.Content(
                    parts=[types.Part(
                        inline_data=types.Blob(
                            data=pcm_16khz_int16,
                            mime_type=f"audio/pcm;rate={_INPUT_RATE}",
                        )
                    )],
                    role="user",
                )
                await self._session.send_client_content(turns=turn, turn_complete=True)

                # Drain responses until the server marks the turn done.
                while True:
                    try:
                        msg = await asyncio.wait_for(
                            self._response_queue.get(), timeout=recv_timeout_s
                        )
                    except asyncio.TimeoutError:
                        raise ConnectionError(
                            f"No Live response within {recv_timeout_s}s — session likely dead"
                        )
                    if msg is None:
                        raise ConnectionError("Live recv loop ended unexpectedly")
                    if msg.data:
                        yield msg.data
                    sc = msg.server_content
                    if sc is not None and sc.turn_complete:
                        # Reliability over context: close after every turn.
                        # Gemini Live sessions seem to idle-time-out
                        # aggressively; reconnect cost is ~0.6 s and dwarfs
                        # the cost of a stuck pipeline waiting on a dead WS.
                        if not self.cfg.get("persistent_session", False):
                            await self.close()
                        return
                return
            except (ConnectionError, asyncio.TimeoutError) as e:
                log.warning(f"[{self.device_id}] Live transport error (attempt {attempt+1}): {e}")
                await self.close()
                if attempt == 1:
                    raise
            except Exception as e:
                log.error(f"[{self.device_id}] Live error: {e}")
                await self.close()
                raise

    # ConversationBackend protocol stubs — Live is audio-only, so the
    # text-based methods are no-ops. process_utterances knows to call
    # `converse(pcm)` instead of `stream(text)` when the backend is live.
    async def send(self, user_text: str, extra_context: str | None = None) -> str:
        return ""

    async def stream(self, user_text: str, extra_context: str | None = None):
        if False:
            yield ""  # pragma: no cover  — make this an async generator

    def commit(self, text: str) -> None:
        pass

    def reset(self) -> None:
        pass

    def get_messages(self) -> list[dict]:
        return []

    def set_messages(self, messages: list[dict]) -> None:
        pass
