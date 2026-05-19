import json
import logging
import os
import re
from datetime import datetime
from typing import AsyncIterator
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI

from pipeline.conversation import tools as tool_lib

log = logging.getLogger(__name__)

# Cap so a runaway tool loop can't drain budget.
_MAX_TOOL_ROUNDS = 4


def _resolve_env(value: str) -> str:
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), value)


class ConversationalBackend:
    """Simple chat-completions backend: manages conversation history on the
    client and sends the full context to any OpenAI-compatible endpoint.
    Supports OpenAI-style tool calls when `tools: [...]` is set in config."""

    def __init__(self, cfg: dict, device_id: str):
        self.cfg = cfg
        self.device_id = device_id
        api_key = _resolve_env(cfg.get("api_key", "none"))
        if api_key.startswith("${"):
            log.warning(f"LLM api_key env var not resolved: {api_key} — is it exported?")
        self.client = AsyncOpenAI(
            base_url=cfg["base_url"],
            api_key=api_key,
        )
        self.max_messages = cfg.get("max_messages", 20)
        self.tool_schemas = tool_lib.schemas_for(cfg.get("tools") or [])
        if self.tool_schemas:
            log.info(f"[{device_id}] tools enabled: {[s['function']['name'] for s in self.tool_schemas]}")

        self.persist_path = None
        if persist_dir := cfg.get("persist_dir"):
            os.makedirs(persist_dir, exist_ok=True)
            self.persist_path = os.path.join(persist_dir, f"{device_id}.json")

        self.system_prompt = self._render_system_prompt(cfg["system_prompt"])
        loaded = self._load()
        if loaded:
            # Refresh the system message in case the system_prompt template or
            # the substituted date moved on since the file was written.
            if loaded and loaded[0].get("role") == "system":
                loaded[0]["content"] = self.system_prompt
            else:
                loaded.insert(0, {"role": "system", "content": self.system_prompt})
            self.messages: list[dict] = loaded
        else:
            self.messages = [{"role": "system", "content": self.system_prompt}]

    @staticmethod
    def _render_system_prompt(template: str) -> str:
        """Substitute runtime placeholders ({today}, {weekday}) in the
        system prompt so the model has accurate temporal grounding."""
        if "{today}" not in template and "{weekday}" not in template:
            return template
        now = datetime.now(ZoneInfo("Asia/Tashkent"))
        return template.replace("{today}", now.strftime("%Y-%m-%d")).replace(
            "{weekday}", now.strftime("%A")
        )

    def _build_kwargs(self, messages: list[dict]) -> dict:
        kwargs = dict(
            model=self.cfg["model"],
            messages=messages,
            max_tokens=self.cfg.get("max_tokens", 300),
        )
        if self.tool_schemas:
            kwargs["tools"] = self.tool_schemas
        # Gemini 2.5 via OpenAI-compat: disable thinking with reasoning_effort.
        # https://ai.google.dev/gemini-api/docs/openai
        if self.cfg.get("reasoning_effort"):
            kwargs["reasoning_effort"] = self.cfg["reasoning_effort"]
        return kwargs

    async def _run_tool_calls(self, tool_calls: list[dict], working: list[dict]) -> None:
        """Append assistant(tool_calls) + tool results to `working` in place.

        Gemini 3.x via OpenAI-compat returns a `thought_signature` in each
        tool_call's `extra_content`; the API rejects the echoed call without
        it. We forward whatever provider-specific blob came back."""
        tc_payloads = []
        for tc in tool_calls:
            entry = {
                "id": tc["id"],
                "type": "function",
                "function": {"name": tc["name"], "arguments": tc["arguments"]},
            }
            if tc.get("extra_content"):
                entry["extra_content"] = tc["extra_content"]
            tc_payloads.append(entry)
        working.append({"role": "assistant", "content": None, "tool_calls": tc_payloads})
        for tc in tool_calls:
            result = await tool_lib.call(tc["name"], tc["arguments"])
            working.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })

    def _finalize(self, text: str) -> None:
        self.messages.append({"role": "assistant", "content": text})
        self._prune()
        self.save()

    def _wrap_user(self, user_text: str, extra_context: str | None) -> str:
        return f"{extra_context}\n\n{user_text}" if extra_context else user_text

    async def send(self, user_text: str, extra_context: str | None = None) -> str:
        self._sanitize()
        self.messages.append({"role": "user", "content": self._wrap_user(user_text, extra_context)})

        # working = persisted history + intermediate tool-loop messages.
        # The intermediates never make it back into self.messages; only the
        # final assistant text gets persisted via _finalize/commit.
        working = list(self.messages)

        text = ""
        for _ in range(_MAX_TOOL_ROUNDS):
            response = await self.client.chat.completions.create(**self._build_kwargs(working))
            msg = response.choices[0].message
            if msg.tool_calls:
                await self._run_tool_calls(
                    [
                        {
                            "id": tc.id,
                            "name": tc.function.name,
                            "arguments": tc.function.arguments or "{}",
                            "extra_content": (tc.model_extra or {}).get("extra_content"),
                        }
                        for tc in msg.tool_calls
                    ],
                    working,
                )
                continue
            text = msg.content or ""
            break

        self._finalize(text)
        log.debug(f"[{self.device_id}] LLM: {text}")
        return text

    async def stream(self, user_text: str, extra_context: str | None = None) -> AsyncIterator[str]:
        self._sanitize()
        self.messages.append({"role": "user", "content": self._wrap_user(user_text, extra_context)})

        working = list(self.messages)

        for _ in range(_MAX_TOOL_ROUNDS):
            kwargs = self._build_kwargs(working)
            kwargs["stream"] = True
            stream = await self.client.chat.completions.create(**kwargs)

            # idx -> partial tool call being assembled across deltas
            tool_buf: dict[int, dict] = {}
            next_idx = 0
            yielded_text = False

            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta.content:
                    yielded_text = True
                    yield delta.content
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        # Gemini OpenAI-compat sometimes leaves index=None.
                        idx = tc.index if tc.index is not None else next_idx
                        slot = tool_buf.get(idx)
                        if slot is None:
                            slot = {"id": "", "name": "", "arguments": "", "extra_content": None}
                            tool_buf[idx] = slot
                            next_idx = max(next_idx, idx + 1)
                        if tc.id:
                            slot["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                slot["name"] += tc.function.name
                            if tc.function.arguments:
                                slot["arguments"] += tc.function.arguments
                        extra = (tc.model_extra or {}).get("extra_content")
                        if extra:
                            slot["extra_content"] = extra

            if not tool_buf:
                return

            if yielded_text:
                # Model spoke before deciding to call a tool — unusual but
                # legal. We've already yielded the text; bail out rather than
                # mix prose with tool round-trips mid-utterance.
                log.warning(f"[{self.device_id}] tool_calls after content; skipping tool round")
                return

            await self._run_tool_calls(
                [
                    {
                        "id": v["id"],
                        "name": v["name"],
                        "arguments": v["arguments"] or "{}",
                        "extra_content": v["extra_content"],
                    }
                    for _, v in sorted(tool_buf.items())
                ],
                working,
            )

        log.warning(f"[{self.device_id}] tool loop exceeded {_MAX_TOOL_ROUNDS} rounds")

    def commit(self, text: str) -> None:
        """Persist the assistant response to history after successful
        delivery. The caller joins whatever was actually sent to TTS."""
        self._finalize(text)

    def reset(self) -> None:
        self.messages = [{"role": "system", "content": self.cfg["system_prompt"]}]
        self.save()

    def get_messages(self) -> list[dict]:
        return self.messages

    def set_messages(self, messages: list[dict]) -> None:
        self.messages = messages
        self._sanitize()

    def save(self):
        if not self.persist_path:
            return
        with open(self.persist_path, "w") as f:
            json.dump(self.messages, f, indent=2)

    def _load(self) -> list[dict] | None:
        if not self.persist_path or not os.path.exists(self.persist_path):
            return None
        try:
            with open(self.persist_path) as f:
                messages = json.load(f)
            log.info(f"[{self.device_id}] loaded {len(messages)-1} messages from {self.persist_path}")
            return messages
        except Exception as e:
            log.warning(f"[{self.device_id}] failed to load {self.persist_path}: {e}")
            return None

    def _prune(self):
        while len(self.messages) > self.max_messages:
            self.messages.pop(1)

    def _sanitize(self):
        """Ensure messages alternate user/assistant after the system prompt."""
        cleaned = [self.messages[0]] if self.messages and self.messages[0]["role"] == "system" else []
        expected = "user"
        start = 1 if cleaned else 0
        for msg in self.messages[start:]:
            if msg["role"] == "system":
                continue
            if msg["role"] == expected:
                cleaned.append(msg)
                expected = "assistant" if expected == "user" else "user"
        if len(cleaned) > 1 and cleaned[-1]["role"] == "user":
            cleaned.pop()
        self.messages = cleaned
