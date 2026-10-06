"""Host-neutral before-turn, after-turn and typed outcome lifecycle."""

from __future__ import annotations

import time
import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .client import ApiError, Team0ApiClient
from .config import RuntimeConfig
from .storage import RuntimeStore, stable_id
from .tool_outcomes import EnvelopeToolOutcomeAdapter, ToolOutcomeAdapter
from .turn_context import AgentTurnContext, build_agent_turn_context


CONVERSATION_SCHEMA = "team0.agent_conversation.turn.v1"
TOOL_ACTIVITY_SCHEMA = "team0.agent_tool_activity.v1"
MAX_MESSAGE_CHARS = 16_000


def _fit_message(text: str) -> str:
    """Shorten a message over MAX_MESSAGE_CHARS to its beginning and end, marked."""
    if len(text) <= MAX_MESSAGE_CHARS:
        return text
    marker = f"\n\n[... {{}} characters omitted: the message was longer than {MAX_MESSAGE_CHARS:,} ...]\n\n"
    keep = MAX_MESSAGE_CHARS - len(marker.format(len(text)))
    head = keep * 3 // 4
    tail = keep - head
    return text[:head] + marker.format(len(text) - head - tail) + text[len(text) - tail:]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")



#: Turns the host generates about its own background work (Claude Code's task notices).
_HOST_NOTICE_TAGS = ("<task-notification>",)


def _is_host_notice(prompt: str) -> bool:
    return str(prompt or "").lstrip().startswith(_HOST_NOTICE_TAGS)


class Team0AgentRuntime:
    def __init__(
        self,
        config: RuntimeConfig,
        *,
        client: Team0ApiClient | None = None,
        store: RuntimeStore | None = None,
        tool_outcome_adapters: Sequence[ToolOutcomeAdapter] | None = None,
        clock=time.monotonic,
    ) -> None:
        self.config = config
        self.store = store or RuntimeStore(config.data_dir)
        self.store.declare_connection(
            host_id=config.host_id,
            configured=config.configured,
            connection_id=stable_id("connection", config.host_id, config.api_key or ""),
        )
        self._clock = clock
        self._tool_outcome_adapters = tuple(
            tool_outcome_adapters or (EnvelopeToolOutcomeAdapter(),)
        )
        self.client = client or (
            Team0ApiClient(
                base_url=config.api_base_url,
                api_key=config.api_key or "",
                read_timeout_seconds=config.context_timeout_seconds,
                write_timeout_seconds=config.write_timeout_seconds,
                host_name=config.host_id,
            )
            if config.configured
            else None
        )

    def before_turn(
        self, *, session_id: str, turn_id: str, prompt: str,
        host_turn_id: str | None = None,
    ) -> tuple[str | None, str | None]:
        prepared, warning = self.prepare_turn(
            session_id=session_id, turn_id=turn_id, prompt=prompt,
            host_turn_id=host_turn_id,
        )
        return (prepared.render() if prepared is not None else None), warning

    def bind_work(self, *, session_id: str, action_id: str | None) -> None:
        """Host/agent explicitly selects a task; prose is never parsed into authority."""
        if action_id:
            if not self.client:
                raise ApiError('work.not_configured', status=403)
            self.client.work_tool('work_read', {'action_id': action_id}, request_id=stable_id('workbind', session_id, action_id))
        self.store.bind_work(session_id, action_id, stable_id('connection', self.config.host_id, self.config.api_key or ''))

    def work_command(self, *, action_id: str | None, command: Mapping[str, Any], manage: bool = False,
                     session_id: str, session_label: str = "") -> Mapping[str, Any]:
        """Explicit structured reports only. Completion is never inferred from a turn ending."""
        if not self.client:
            raise ApiError('work.not_configured', status=403)
        if not session_id:
            raise ApiError('work.session_required', status=422)
        result = self.client.work_tool('work_manage' if manage else 'work_report',
            {'action_id': action_id or '', 'command': dict(command),
             'session_id': stable_id('worksession', self.config.host_id, session_id),
             'session_label': session_label}, request_id=str(command['idempotency_key']))
        if action_id and command.get('command') in {'accept', 'start'}:
            self.store.bind_work(session_id, action_id, stable_id('connection', self.config.host_id, self.config.api_key or ''))
        return result

    def prepare_turn(
        self, *, session_id: str, turn_id: str, prompt: str,
        host_turn_id: str | None = None,
    ) -> tuple[AgentTurnContext | None, str | None]:
        """Return policy and retrieved data separately for any capable host adapter."""

        if not self.config.enabled:
            return None, None
        self.store.mark_lifecycle_event("before_turn")
        self.store.start_turn(
            session_id=session_id, turn_id=turn_id, prompt=prompt, occurred_at=utc_now()
        )
        if host_turn_id is not None:
            # Persist callback correlation before any network wait. The host can
            # kill this process on timeout and still return the completed turn.
            self.store.bind_active_turn(
                session_id=session_id, turn_id=turn_id, host_turn_id=host_turn_id,
            )
        if not self.client:
            return self._degraded("not_configured", "Team0 access is not configured.")
        if _is_host_notice(prompt):
            # The host reporting its own background work, not a person asking something.
            return None, None
        started = self._clock()
        active_work = self.store.active_work(session_id, stable_id('connection', self.config.host_id, self.config.api_key or ''))
        try:
            read = self.client.create_understanding_read(
                query=prompt,
                idempotency_key=stable_id("rtread", session_id, turn_id),
            )
        except ApiError as error:
            self.store.update_health(
                state="revoked" if error.status in {401, 403, 410} else "degraded",
                last_read_error=error.code,
                last_read_status=error.status,
                last_read_latency_ms=round((self._clock() - started) * 1000, 1),
            )
            if active_work:
                return AgentTurnContext('unavailable', 'degraded',
                    'STOP bound-task execution: fresh Team0 context is unavailable. Obtain successful understanding and work_read before continuing.',
                    '', maximum=self.config.context_max_chars), None
            return (
                None,
                "Team0 context was unavailable for this turn."
                if self.config.surface_errors
                else None,
            )
        elapsed_ms = round((self._clock() - started) * 1000, 1)
        prepared = build_agent_turn_context(
            read, maximum=self.config.context_max_chars
        )
        this_session = stable_id('worksession', self.config.host_id, session_id)
        prepared = replace(prepared, policy=f'When using shared work tools, this conversation\'s session_id is {this_session}; retain it for claims and updates.\n' + prepared.policy)
        if active_work:
            try:
                work = self.client.work_tool('work_read', {'action_id': active_work},
                    request_id=stable_id('workread', session_id, turn_id, active_work))
                assignment = work.get('state', {}).get('assignment') or {}
                attempt = work.get('state', {}).get('attempt') or {}
                if (assignment.get('session_id') and assignment['session_id'] != this_session
                        or assignment.get('agent_id') and work.get('actor_id')
                        and assignment['agent_id'] != work['actor_id']
                        or attempt and not assignment.get('session_id')):
                    raise ApiError('work.session_mismatch', status=409)
                # Reserve a bounded task slice; never inject the whole work roster.
                task_budget = min(6000, self.config.context_max_chars // 3)
                encoded = json.dumps(work, ensure_ascii=False, default=str)
                data = 'Current shared task (quoted operational data):\n' + encoded[:task_budget]
                if len(encoded) > task_budget:
                    data += '\nTask context truncated; read work_read for the complete checkpoint/questions before acting.'
                prepared = replace(prepared, data=data + '\n\n' + prepared.data,
                    policy=f'For the bound task, your session_id is {this_session}. Use only this identity with work_report/work_manage (the runtime work helper supplies it automatically). Never copy a different claimant session ID. Acceptance and start are required before execution. Use the exact revision, assignment generation and attempt ID. Resolve blocking questions and observe stop requests before effects. Never infer completion from a finished chat turn.\n' + prepared.policy)
            except ApiError as error:
                # Ordinary understanding may be usable, but stale task context must not
                # silently authorize continued execution after revocation or handoff.
                reason = ('Another session or agent owns this assignment, or its older execution has no session identity. Request an explicit stopped/reconciled handoff before continuing.'
                    if error.code == 'work.session_mismatch' else 'Its current authority/state could not be read. Obtain a successful work_read before continuing this task.')
                prepared = replace(prepared, policy='STOP bound-task execution: ' + reason + '\n' + prepared.policy)
                self.store.update_health(last_work_error=error.code)
        rendered = prepared.render()
        self.store.attach_understanding_read(
            turn_id=turn_id,
            read_id=prepared.read_id,
            read_status=prepared.read_status,
            context_contract=prepared.contract,
            context_chars=len(rendered),
            latency_ms=elapsed_ms,
        )
        self.store.update_health(
            state="healthy",
            last_read_id=read.get("id"),
            last_read_latency_ms=elapsed_ms,
            last_read_status=read.get("status"),
            last_read_at=utc_now(),
            last_read_error=None,
            last_read_turn_id=turn_id,
            last_context_contract=prepared.contract,
            last_context_chars=len(rendered),
        )
        return prepared, None

    def after_turn(self, *, turn_id: str, assistant_message: str | None) -> str | None:
        if not self.config.enabled:
            return None
        turn = self.store.get_turn(turn_id)
        if not turn:
            return None
        self.store.mark_lifecycle_event("after_turn")
        if not self.client:
            return self._write_error("not_configured", "Team0 access is not configured.")
        source_id = self._contribution_source_id()
        if not source_id:
            # The owner may stop conversation contribution without revoking the
            # agent's read access.  That is a successful privacy control, not a
            # failed sync to retain and retry.
            self.store.delete_turn(turn_id)
            self.store.update_health(
                last_write_error=None, last_write_at=utc_now()
            )
            return None
        # Over the limit: keep the beginning and the end, visibly marked. Retaining it
        # instead stuck the turn and repeated a warning on every later turn.
        prompt = _fit_message(str(turn.get("prompt") or ""))
        answer = _fit_message(str(assistant_message or ""))
        session_id = str(turn["session_id"])
        record_id = stable_id("agentturn", source_id, session_id, turn_id)
        observed_at = utc_now()
        payload: dict[str, Any] = {
            "conversation_id": session_id[:500],
            "turn_id": turn_id[:500],
            "sequence": int(turn["sequence"]),
            "user_message": {
                "message_id": stable_id("msg", session_id, turn_id, "user")[:500],
                "content": prompt,
                "occurred_at": turn["occurred_at"],
            },
        }
        previous = turn.get("previous_turn_id")
        if previous:
            payload["previous_turn_id"] = str(previous)[:500]
        if answer:
            payload["agent_message"] = {
                "message_id": stable_id("msg", session_id, turn_id, "agent")[:500],
                "content": answer,
                "occurred_at": observed_at,
                "reply_to_message_id": payload["user_message"]["message_id"],
            }
        event = {
            "registered_source_id": source_id,
            "external_event_id": record_id[:500],
            "schema_version": CONVERSATION_SCHEMA,
            "occurred_at": turn["occurred_at"],
            "observed_at": observed_at,
            "payload": payload,
        }
        self.store.enqueue(
            record_id=record_id,
            kind="wm_event",
            payload=event,
            idempotency_key=stable_id("rtwrite", record_id),
        )
        self.store.delete_turn(turn_id)
        result = self.flush_pending()
        return result.get("message")

    def after_tool(self, event: Mapping[str, Any]) -> str | None:
        if not self.config.enabled or not self.client:
            return None
        outcomes = tuple(
            outcome
            for adapter in self._tool_outcome_adapters
            if (outcome := adapter.normalize(event)) is not None
        )
        if not outcomes:
            return None
        tool_name = str(event.get("tool_name") or "")
        source_id = self._contribution_source_id()
        for outcome in outcomes:
            activity = outcome.activity
            if not (self.config.report_tool_activity and source_id and activity):
                continue
            activity_id = str(activity.get("activity_id") or "")
            if activity_id:
                record_id = stable_id("agentactivity", source_id, tool_name, activity_id)
                now = utc_now()
                self.store.enqueue(
                    record_id=record_id,
                    kind="wm_event",
                    payload={
                        "registered_source_id": source_id,
                        "external_event_id": record_id[:500],
                        "schema_version": TOOL_ACTIVITY_SCHEMA,
                        "occurred_at": activity.get("occurred_at") or now,
                        "observed_at": now,
                        "payload": dict(activity),
                    },
                    idempotency_key=stable_id("rtwrite", record_id),
                )
        completed_action_ids = {
            outcome.completed_action_id
            for outcome in outcomes
            if outcome.completed_action_id
        }
        if tool_name in self.config.trusted_action_tools:
            for action_id in completed_action_ids:
                record_id = stable_id(
                    "actionoutcome",
                    tool_name,
                    event.get("tool_use_id"),
                    action_id,
                    "completed",
                )
                self.store.enqueue(
                    record_id=record_id,
                    kind="action_complete",
                    payload={"action_item_id": action_id},
                    idempotency_key=stable_id("rtaction", record_id),
                )
        result = self.flush_pending()
        return result.get("message")

    def _contribution_source_id(self) -> str | None:
        if not self.client:
            return self.config.contribution_source_id
        try:
            binding = self.client.get_agent_runtime_binding()
        except ApiError as error:
            if error.code == "runtime.binding_unavailable" and error.status == 409:
                return None
            return self.config.contribution_source_id
        source_id = binding.get("contribution_source_id")
        if self.config.contribution_source_id:
            return self.config.contribution_source_id
        return source_id if isinstance(source_id, str) and source_id else None

    def flush_pending(self, *, limit: int = 8) -> Mapping[str, Any]:
        if not self.client:
            return {"accepted": 0, "failed": 0, "message": None}
        accepted = failed = 0
        last_message = None
        for record in self.store.due(limit=limit):
            try:
                if record["kind"] == "wm_event":
                    self.client.ingest_event(
                        event=record["payload"],
                        idempotency_key=str(record["idempotency_key"]),
                    )
                elif record["kind"] == "action_complete":
                    self.client.complete_action(
                        action_id=str(record["payload"]["action_item_id"]),
                        idempotency_key=str(record["idempotency_key"]),
                    )
                else:
                    raise ApiError("runtime.unknown_outbox_kind", status=422)
            except ApiError as error:
                failed += 1
                updated = self.store.failed(
                    str(record["id"]), error=error.code, retryable=error.retryable
                )
                terminal = bool(updated and updated.get("status") == "failed")
                self.store.update_health(
                    state="failed" if terminal else "delayed",
                    last_write_error=error.code,
                    last_write_at=utc_now(),
                )
                if terminal and self.config.surface_errors:
                    last_message = "Team0 sync failed; the turn remains available for recovery."
            else:
                accepted += 1
                self.store.accepted(str(record["id"]))
                self.store.update_health(
                    state="healthy", last_write_error=None, last_write_at=utc_now()
                )
        return {"accepted": accepted, "failed": failed, "message": last_message}

    def _degraded(self, code: str, message: str) -> tuple[None, str | None]:
        self.store.update_health(state="degraded", last_read_error=code)
        return None, message if self.config.surface_errors else None

    def _write_error(self, code: str, message: str) -> str | None:
        self.store.update_health(state="failed", last_write_error=code)
        return message if self.config.surface_errors else None
