import logging

import httpx
import numpy as np

from pipeline.audio import pcm_to_wav

log = logging.getLogger(__name__)


async def transcribe(pcm_int16_bytes: bytes, config: dict) -> dict:
    """Send PCM audio to the ASR service and return {"text": ..., "no_speech_prob": ...}.

    Supports two endpoint shapes:
      - parakeet-style:  POST {url}/transcribe with field "audio" (single-file)
      - navai-batch:     POST {url}/transcribe/transcribe-batch with field
        "audio_files" + form "languages=<lang>" returning
        [{"transcription": ..., "language": ..., "segments": [...]}, ...]

    Selected by `asr.endpoint` in config (`"parakeet"` or `"navai-batch"`).
    Defaults to parakeet for backwards compatibility.
    """
    asr_cfg = config["asr"]
    endpoint = asr_cfg.get("endpoint", "parakeet")
    sample_rate = config["audio"]["sample_rate"]

    wav_bytes = pcm_to_wav(np.frombuffer(pcm_int16_bytes, dtype=np.int16), rate=sample_rate)
    base_url = asr_cfg["url"].rstrip("/")

    if endpoint == "navai-batch":
        url = base_url + "/transcribe/transcribe-batch"
        files = {"audio_files": ("audio.wav", wav_bytes, "audio/wav")}
        data = {"languages": asr_cfg.get("language", "uz")}
        timeout = float(asr_cfg.get("timeout", 30))
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, files=files, data=data)
            resp.raise_for_status()
            arr = resp.json()
        # API returns a list (one entry per file); take the first.
        first = arr[0] if isinstance(arr, list) and arr else {}
        text = (first.get("transcription") or "").strip()
        # No per-utterance silence probability from this endpoint — derive a
        # cheap proxy so the rest of the pipeline can still skip empty results.
        no_speech_prob = 0.0 if text else 1.0
        log.info(f"ASR  \"{text}\"  (navai-batch, lang={first.get('language', '?')})")
        return {"text": text, "no_speech_prob": no_speech_prob, "raw": first}

    # Default: parakeet-mlx (embedded asr_server.py)
    url = base_url + "/transcribe"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, files={"audio": ("audio.wav", wav_bytes, "audio/wav")})
        resp.raise_for_status()
        data = resp.json()
    text = data.get("text", "")
    t = data.get("transcribe_time_s", "?")
    log.info(f"ASR  \"{text}\"  ({t}s)")
    return data
