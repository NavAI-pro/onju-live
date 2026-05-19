"""Standalone smoke test for the find_timezone tool.

Two modes:
  python tests/test_tools.py            # call the tool directly (no LLM, no key)
  python tests/test_tools.py --live     # full LLM round-trip (needs GEMINI_API_KEY)

Run from the repo root with the venv activated.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from pipeline.conversation import tools
from pipeline.conversation.conversational import ConversationalBackend


async def direct():
    for loc in ["Tashkent", "New York", "Japan", "uzbekistan", "asdfqwer"]:
        print(f"  {loc:>14}  ->  {await tools.find_timezone({'location': loc})}")


async def live():
    # Load .env into os.environ so ${GEMINI_API_KEY} in config resolves.
    env_path = os.path.join(os.path.dirname(__file__), os.pardir, ".env")
    if os.path.exists(env_path):
        for line in open(env_path):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v)
    if not os.environ.get("GEMINI_API_KEY"):
        sys.exit("GEMINI_API_KEY not set (in env or .env)")

    import yaml
    cfg_path = os.path.join(os.path.dirname(__file__), os.pardir, "pipeline", "config.yaml")
    full = yaml.safe_load(open(cfg_path))
    cfg = full["conversation"]["conversational"]
    cfg.setdefault("tools", ["find_timezone"])
    cfg.setdefault("persist_dir", None)
    cfg["persist_dir"] = None  # don't pollute real device history
    backend = ConversationalBackend(cfg, "_test")
    prompt = "What time is it right now in Tashkent?"
    print(f"user: {prompt}")
    print("assistant: ", end="", flush=True)
    chunks: list[str] = []
    async for chunk in backend.stream(prompt):
        print(chunk, end="", flush=True)
        chunks.append(chunk)
    print()
    backend.commit("".join(chunks))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--live", action="store_true", help="hit the real Gemini API")
    args = p.parse_args()
    asyncio.run(live() if args.live else direct())
