import asyncio
import logging
import time

from pipeline.conversation import ConversationBackend, create_backend
from pipeline.vad import VAD

log = logging.getLogger(__name__)


class Device:
    def __init__(self, hostname: str, ip: str, config: dict, conversation: ConversationBackend,
                 voice: str | None = None, ptt: bool = False):
        self.hostname = hostname
        self.ip = ip
        self.config = config
        self.conversation = conversation
        el_cfg = config["tts"].get("elevenlabs", {})
        if voice:
            self.voice = voice
        elif ptt and el_cfg.get("default_voice_ptt"):
            self.voice = el_cfg["default_voice_ptt"]
        else:
            self.voice = el_cfg.get("default_voice", "Emma")
        self.ptt = ptt
        self.vad = None if ptt else VAD(config)
        self.last_user_text: str | None = None
        self.last_response: str | None = None
        self.led_power = 0
        self.led_update_time = 0.0
        self.vad_writer: asyncio.StreamWriter | None = None  # persistent TCP for LED blinks
        # User-facing volume on a 0..100 scale, mutable via the set_volume tool.
        # Default seeded from config.device.default_volume (0..20 firmware scale) × 5.
        self.volume: int = max(0, min(100, int(config["device"].get("default_volume", 15)) * 5))
        # Wake-word override: epoch timestamp until which the wake-word filter
        # is bypassed. Set to +inf while the PTT button is held; on release,
        # bumped to now+2.0s so any utterance Silero VAD is still chunking at
        # the trailing edge of the press still gets the bypass.
        self.ptt_override_until: float = 0.0
        # Follow-up window: after each assistant reply we briefly accept the
        # next utterance without requiring the wake-word, so the user can say
        # "ha yubor" / "yes send" / a quick clarification naturally.
        self.followup_until: float = 0.0

        # PTT state
        self.ptt_buffer: list = []  # raw PCM frames during PTT
        self.processing = False     # True while ASR/LLM/TTS pipeline is running
        self.interrupted = asyncio.Event()

    @property
    def ptt_override(self) -> bool:
        return time.time() < self.ptt_override_until

    @property
    def in_followup(self) -> bool:
        return time.time() < self.followup_until

    def to_dict(self) -> dict:
        return {
            "hostname": self.hostname,
            "ip": self.ip,
            "voice": self.voice,
            "ptt": self.ptt,
        }

    def __repr__(self):
        mode = "PTT" if self.ptt else "VOX"
        return f"<Device {self.hostname} {self.ip} {mode}>"


class DeviceManager:
    def __init__(self, config: dict):
        self.config = config
        self.devices: dict[str, Device] = {}

    def create_device(self, hostname: str, ip: str, ptt: bool = False) -> Device:
        device = self.devices.get(hostname)
        if device is None:
            conv = create_backend(self.config, hostname)
            device = Device(hostname, ip, self.config, conversation=conv, ptt=ptt)
            self.devices[hostname] = device
            log.debug(f"New device: {device}")
        elif device.ip != ip:
            device.ip = ip
            log.debug(f"Updated {hostname} IP to {ip}")
        else:
            log.debug(f"Device {hostname} reconnected ({ip})")
        return device

    def get_by_ip(self, ip: str) -> Device | None:
        for d in self.devices.values():
            if d.ip == ip:
                return d
        return None

    def get_most_recent(self) -> Device | None:
        if self.devices:
            return next(reversed(self.devices.values()))
        return None
