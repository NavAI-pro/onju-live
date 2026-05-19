"""Diagnostic monitor for the mic / PTT path.

Run alongside `./run.sh` in a second terminal. It polls the control server
for the device list and reports state transitions. Use it when the M5 Echo
button press isn't doing anything on the server side.

  python tests/test_mic_monitor.py                 # poll localhost:3002
  python tests/test_mic_monitor.py --host 1.2.3.4  # poll a remote server
  python tests/test_mic_monitor.py --register 192.168.1.15 m5-echo-XX --ptt
                                                   # one-shot manual register

What it tells you, and what to do about it:

* No devices registered for >5s:
  The server isn't seeing multicast announcements. The M5 Echo's 2.4 GHz
  band usually doesn't bridge multicast to your laptop's 5 GHz band — this
  is the most common cause of "I press the button and nothing happens".
  Fix: read the device's IP off the serial monitor and POST it manually
  (this script can do that with --register).

* Device appears, but button press produces no `PTT end ...` line in the
  server log: the device sees the server (you got a greeting/LED blink
  on boot) but UDP audio isn't reaching back. Check that your laptop
  firewall is not dropping UDP :3000, and that the device's serial
  output shows it sending audio on press.

* Device appears + you see `PTT end (X.Ys)` in the server log but no
  `ASR  "..."`: the ASR host is unreachable or rejecting the request.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

import httpx


async def _get(host: str, port: int) -> list[dict] | None:
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"http://{host}:{port}/devices")
            r.raise_for_status()
            data = json.loads(r.text)
            return list(data.values()) if isinstance(data, dict) else data
    except Exception as e:
        print(f"\r[!] control server {host}:{port} unreachable: {e}", flush=True)
        return None


async def watch(host: str, port: int, interval: float) -> None:
    print(f"Watching control server at http://{host}:{port}/devices  (Ctrl+C to stop)")
    print()
    last: dict[str, dict] = {}
    last_empty_warning = 0.0
    while True:
        devices = await _get(host, port)
        if devices is not None:
            current = {d["hostname"]: d for d in devices}
            for hn, info in current.items():
                prev = last.get(hn)
                if not prev:
                    mode = "PTT" if info.get("ptt") else "VOX"
                    print(f"[+] {hn:<24} {info['ip']:<16} {mode}")
                elif prev["ip"] != info["ip"]:
                    print(f"[~] {hn} ip changed {prev['ip']} -> {info['ip']}")
            for hn in last:
                if hn not in current:
                    print(f"[-] {hn} disappeared")
            last = current

            if not current:
                now = time.time()
                if now - last_empty_warning > 5:
                    print(
                        f"[?] no devices registered yet — check the M5 Echo's serial "
                        f"output for an IP, then run:\n"
                        f"    python tests/test_mic_monitor.py --register <ip> <hostname> --ptt"
                    )
                    last_empty_warning = now
        await asyncio.sleep(interval)


async def register(host: str, port: int, ip: str, hostname: str, ptt: bool) -> None:
    payload = {"ip": ip, "hostname": hostname, "ptt": ptt}
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.post(f"http://{host}:{port}/devices", json=payload)
        print(f"POST /devices -> {r.status_code}  {r.text.strip()}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1", help="server host")
    p.add_argument("--port", type=int, default=3002, help="control port (default 3002)")
    p.add_argument("--interval", type=float, default=1.0, help="poll interval seconds")
    p.add_argument("--register", nargs=2, metavar=("IP", "HOSTNAME"),
                   help="one-shot: register a device by IP+hostname and exit")
    p.add_argument("--ptt", action="store_true", help="mark the registered device as PTT")
    args = p.parse_args()

    try:
        if args.register:
            ip, hostname = args.register
            asyncio.run(register(args.host, args.port, ip, hostname, args.ptt))
        else:
            asyncio.run(watch(args.host, args.port, args.interval))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
