import io
import logging
import os

import httpx
import numpy as np
from pydub import AudioSegment
from scipy.io import wavfile

from pipeline.audio import resample_pcm_int16

log = logging.getLogger(__name__)


async def synthesize(text: str, voice: str, config: dict) -> bytes:
    """Convert text to 16kHz mono PCM bytes using the configured TTS backend."""
    backend = config["tts"]["backend"]
    if backend == "elevenlabs":
        return await _elevenlabs(text, voice, config)
    if backend == "local":
        return await _local(text, config)
    if backend == "navai_uz":
        return await _navai_uz(text, config)
    raise ValueError(f"Unknown TTS backend: {backend}")


async def _elevenlabs(text: str, voice_name: str, config: dict) -> bytes:
    el_cfg = config["tts"]["elevenlabs"]
    api_key = el_cfg["api_key"]
    voice_id = el_cfg["voices"].get(voice_name, el_cfg["voices"].get(el_cfg["default_voice"]))

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
    }
    payload = {"text": text}

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        mp3_bytes = resp.content

    audio = AudioSegment.from_mp3(io.BytesIO(mp3_bytes))
    audio = audio.set_channels(1).set_frame_rate(16000).set_sample_width(2)
    log.debug(f"TTS: {len(text)} chars -> {len(audio)}ms audio")
    return audio.raw_data


async def _navai_uz(text: str, config: dict) -> bytes:
    """NavAI Uzbek TTS — returns 24 kHz int16 mono WAV, we down-sample to 16 kHz.

    Request shape matches NavAI's canonical curl:
        curl -X POST /synthesize/<mode>
          -F target_text=...
          -F voice_id=muxlisa
          -F reference_audio=
          -F reference_text=...
          -F output_format=wav

    All parameters go as multipart form fields. `voice_id` selects the
    default voice on the server; `reference_audio` + `reference_text` are
    the voice-cloning slots (left empty when using a pre-loaded voice).
    """
    cfg = config["tts"]["navai_uz"]
    url = cfg["url"].rstrip("/") + cfg.get("path", "/synthesize/local")
    files = {
        "target_text": (None, text),
        "output_format": (None, cfg.get("output_format", "wav")),
    }
    if voice_id := cfg.get("voice_id"):
        files["voice_id"] = (None, voice_id)
    # Voice-cloning slots — sent as empty by default to match the canonical
    # curl. Override via config if you want to clone from a reference clip.
    files["reference_audio"] = (None, cfg.get("reference_audio", ""))
    files["reference_text"] = (None, cfg.get("reference_text", ""))
    timeout = float(cfg.get("timeout", 60))
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, files=files)
        resp.raise_for_status()
        wav_bytes = resp.content

    sr, audio = wavfile.read(io.BytesIO(wav_bytes))
    if audio.dtype != np.int16:
        # The endpoint advertises 16-bit PCM but be defensive.
        if audio.dtype.kind == "f":
            audio = np.clip(audio * 32767.0, -32768, 32767).astype(np.int16)
        else:
            audio = audio.astype(np.int16)
    if audio.ndim > 1:
        audio = audio[:, 0]
    pcm = audio.tobytes()
    target_rate = config["audio"]["sample_rate"]
    if sr != target_rate:
        pcm = resample_pcm_int16(pcm, sr, target_rate)
    log.debug(f"TTS navai_uz: {len(text)} chars -> {len(pcm)//2} samples @ {target_rate} Hz")
    return pcm


async def _local(text: str, config: dict) -> bytes:
    local_cfg = config["tts"]["local"]
    url = local_cfg["url"].rstrip("/") + "/v1/audio/speech"

    payload = {
        "model": local_cfg["model"],
        "input": text,
        "response_format": "wav",
    }

    # Voice cloning: pass ref_audio path (server reads from disk)
    ref_audio = local_cfg.get("ref_audio")
    if ref_audio:
        payload["ref_audio"] = os.path.abspath(ref_audio)
    ref_text = local_cfg.get("ref_text")
    if ref_text:
        payload["ref_text"] = ref_text

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        wav_bytes = resp.content

    audio = AudioSegment.from_wav(io.BytesIO(wav_bytes))
    audio = audio.set_channels(1).set_frame_rate(16000).set_sample_width(2)
    log.debug(f"TTS local: {len(text)} chars -> {len(audio)}ms audio")
    return audio.raw_data
