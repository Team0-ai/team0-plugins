"""Hermes turn hooks backed by the same runtime used by the other native hosts."""

from __future__ import annotations

import os
from collections import defaultdict
from pathlib import Path
from threading import Lock
from uuid import uuid4

from .runtime.team0_agent_runtime import RuntimeConfig, Team0AgentRuntime
from .runtime.team0_agent_runtime.storage import stable_id

_AUTO_CONTRIBUTION_NOTE = (
    "Team0's Hermes plugin handles any permitted completed-turn contribution automatically. "
    "Do not call team0_contribute_conversation for this turn."
)


def _create_runtime():
    key = os.environ.get("TEAM0_HERMES_API_KEY", "").strip()
    if not key:
        return None
    env = dict(os.environ)
    env.pop("PLUGIN_DATA", None)
    env.pop("CLAUDE_PLUGIN_DATA", None)
    env.pop("TEAM0_ACCESS_KEY", None)
    env.pop("TEAM0_CONTRIBUTION_SOURCE_ID", None)
    env.pop("TEAM0_AGENT_SOURCE_ID", None)
    env["TEAM0_API_KEY"] = key
    env["TEAM0_RUNTIME_HOST_ID"] = "hermes"
    env["TEAM0_RUNTIME_DATA_DIR"] = str(
        Path(env.get("HERMES_HOME") or Path.home() / ".hermes") / "team0-agent-runtime"
    )
    return Team0AgentRuntime(RuntimeConfig.from_environ(env))


def _message_text(message):
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        return "\n".join(
            part["text"] for part in message
            if isinstance(part, dict) and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        )
    return ""


class HermesAdapter:
    def __init__(self, runtime):
        self.runtime = runtime
        self._pending = defaultdict(list)
        self._lock = Lock()

    def before_turn(self, session_id="", user_message="", **kwargs):
        if kwargs.get("parent_session_id") or not session_id:
            return None
        prompt = _message_text(user_message)
        if not prompt.strip():
            return None
        host_turn_id = str(kwargs.get("turn_id") or "")
        turn_id = stable_id("hermes-turn", session_id, host_turn_id) if host_turn_id else uuid4().hex
        try:
            context, _warning = self.runtime.before_turn(
                session_id=session_id, turn_id=turn_id, prompt=prompt,
            )
        except Exception:
            return None  # A Team0 outage must not interrupt the Hermes turn.
        with self._lock:
            self._pending[(session_id, prompt)].append((host_turn_id, turn_id))
        return {"context": f"{context}\n\n{_AUTO_CONTRIBUTION_NOTE}" if context else _AUTO_CONTRIBUTION_NOTE}

    def after_turn(self, session_id="", user_message="", assistant_response="", **kwargs):
        if kwargs.get("parent_session_id") or not session_id or not isinstance(assistant_response, str) or not assistant_response.strip():
            return
        prompt = _message_text(user_message)
        host_turn_id = str(kwargs.get("turn_id") or "")
        with self._lock:
            pending = self._pending.get((session_id, prompt))
            if not pending:
                return
            index = (
                next((i for i, (known, _turn) in enumerate(pending) if known == host_turn_id), None)
                if host_turn_id else 0
            )
            if index is None:
                index = next((i for i, (known, _turn) in enumerate(pending) if not known), None)
            if index is None:
                return
            _known, turn_id = pending.pop(index)
            if not pending:
                del self._pending[(session_id, prompt)]
        try:
            self.runtime.after_turn(turn_id=turn_id, assistant_message=assistant_response)
        except Exception:
            pass  # The shared runtime retains retryable writes in its outbox.

    def end_turn(self, session_id="", turn_id="", **_kwargs):
        # post_llm_call is absent for interrupted/failed turns. Drop their
        # correlation so a later identical prompt cannot contribute an old turn.
        with self._lock:
            for key, pending in list(self._pending.items()):
                if key[0] != session_id:
                    continue
                remaining = [item for item in pending if turn_id and item[0] not in {"", str(turn_id)}]
                if remaining:
                    self._pending[key] = remaining
                else:
                    del self._pending[key]


def register(ctx):
    runtime = _create_runtime()
    if runtime is None:
        return
    adapter = HermesAdapter(runtime)
    ctx.register_hook("pre_llm_call", adapter.before_turn)
    ctx.register_hook("post_llm_call", adapter.after_turn)
    ctx.register_hook("on_session_end", adapter.end_turn)
