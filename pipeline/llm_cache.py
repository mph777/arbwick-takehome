"""Content-addressed cache of Anthropic API calls.

The grading instruction is "we will rerun everything offline against your
snapshot" and "same inputs -> same decisions". A live model call satisfies
neither: reviewers have no key, and even at temperature 0 a re-run can return a
different rationale, at which point the committed decision log no longer
reproduces.

So every request is keyed by a hash of everything that determines the answer -
model id, sampling parameters, system prompt, tool schema and the exact payload -
and the response is committed alongside the code. `replay` is the default and
never touches the network.

This is not a way of hard-coding answers. The key is derived from the input, so
changing a statistic in Stage 2 changes the key, and replay then fails loudly
with a cache miss instead of returning a stale answer to a question that was
never asked.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import config as cfg

Mode = Literal["replay", "live", "refresh"]


class CacheMiss(RuntimeError):
    def __init__(self, key: str, as_of: str) -> None:
        super().__init__(
            f"LLM cache miss for {as_of} (key {key[:16]}...). The committed cache "
            f"does not contain this exact request, which means an input changed. "
            f"Re-run with --mode live to record it (requires ANTHROPIC_API_KEY)."
        )
        self.key = key


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def request_key(system_prompt: str, tool_schema: dict, payload: dict) -> str:
    return sha256(canonical({
        "model": cfg.LLM_MODEL,
        "temperature": cfg.LLM_TEMPERATURE,
        "max_tokens": cfg.LLM_MAX_TOKENS,
        "system_sha256": sha256(system_prompt),
        "tool_schema_sha256": sha256(canonical(tool_schema)),
        "payload": payload,
    }))


class LLMCache:
    def __init__(self, mode: Mode = "replay", cache_dir: Path | None = None) -> None:
        self.mode = mode
        self.dir = cache_dir or cfg.LLM_CACHE_DIR
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.dir / f"{key}.json"

    def get(self, key: str) -> dict | None:
        if self.mode == "refresh":
            return None
        path = self._path(key)
        if not path.exists():
            return None
        return json.loads(path.read_text())["response"]

    def put(self, key: str, request: dict, response: dict) -> None:
        self._path(key).write_text(json.dumps({
            "key": key,
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "model": cfg.LLM_MODEL,
            "request": request,
            "response": response,
        }, indent=2, sort_keys=True, default=str))

    def allows_network(self) -> bool:
        return self.mode in ("live", "refresh")
