import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from team0_agent_runtime import RuntimeConfig, Team0AgentRuntime, ApiError


class Client:
    def __init__(self):
        self.calls = []
        self.denied = False

    def create_understanding_read(self, **kwargs):
        return {'id': 'read', 'status': 'ready', 'context': 'Relevant understanding'}

    def work_tool(self, name, arguments, **kwargs):
        self.calls.append((name, arguments))
        if self.denied:
            raise ApiError('work.grant_required', status=403)
        return {'id': arguments.get('action_id'), 'state': {'revision': 4, 'checkpoint': {'plan': 'Preserve this accepted plan'}, 'questions': {'q': {'state': 'answered', 'answer': 'Use version B'}}}}


@pytest.mark.parametrize('host', ['codex', 'claude-code', 'openclaw', 'custom-agent'])
def test_session_handoff_reads_only_bound_task_and_preserves_questions(tmp_path, host):
    client = Client()
    config = RuntimeConfig(api_key='test-key', data_dir=tmp_path, host_id=host)
    runtime = Team0AgentRuntime(config, client=client)
    runtime.bind_work(session_id='session-A', action_id='task-A')
    restarted = Team0AgentRuntime(config, client=client)
    value, error = restarted.before_turn(session_id='session-A', turn_id='turn-1', prompt='Continue')
    assert not error and 'Preserve this accepted plan' in value and 'Use version B' in value
    assert all(call[1]['action_id'] == 'task-A' for call in client.calls)
    before = len(client.calls)
    value, _ = restarted.before_turn(session_id='session-B', turn_id='turn-2', prompt='Unrelated work')
    assert len(client.calls) == before and 'task-A' not in value
    client.denied = True
    value, _ = restarted.before_turn(session_id='session-A', turn_id='turn-3', prompt='Continue')
    assert 'STOP bound-task execution' in value and 'Preserve this accepted plan' not in value


def test_binding_does_not_survive_a_different_credential(tmp_path):
    client = Client()
    original = Team0AgentRuntime(RuntimeConfig(api_key='key-A', data_dir=tmp_path), client=client)
    original.bind_work(session_id='same', action_id='private-task')
    replacement = Team0AgentRuntime(RuntimeConfig(api_key='key-B', data_dir=tmp_path), client=client)
    client.calls.clear()
    value, _ = replacement.before_turn(session_id='same', turn_id='new', prompt='Hi')
    assert not client.calls and 'private-task' not in value


def test_understanding_outage_cannot_silently_drop_bound_task_fence(tmp_path):
    client = Client()
    runtime = Team0AgentRuntime(RuntimeConfig(api_key='key', data_dir=tmp_path, context_max_chars=1500), client=client)
    runtime.bind_work(session_id='session', action_id='task')
    def unavailable(**kwargs):
        raise ApiError('unavailable', status=503)
    client.create_understanding_read = unavailable
    value, _ = runtime.before_turn(session_id='session', turn_id='turn', prompt='Continue')
    assert 'STOP bound-task execution' in value
    assert len(value) <= 1500


def test_work_claim_uses_stable_distinct_host_sessions_and_binds_on_success(tmp_path):
    config = RuntimeConfig(api_key='key', data_dir=tmp_path, host_id='codex')
    client = Client()
    runtime = Team0AgentRuntime(config, client=client)
    command = {'command':'accept', 'idempotency_key':'accept-A'}
    runtime.work_command(action_id='task', command=command, session_id='A', session_label='Website')
    first = client.calls[-1][1]['session_id']
    restarted = Team0AgentRuntime(config, client=client)
    restarted.work_command(action_id='task', command=command, session_id='A')
    assert client.calls[-1][1]['session_id'] == first
    restarted.work_command(action_id='other-task', command=command, session_id='B')
    assert client.calls[-1][1]['session_id'] != first
    other_host = Team0AgentRuntime(RuntimeConfig(api_key='key', data_dir=tmp_path / 'claude', host_id='claude-code'), client=client)
    other_host.work_command(action_id='task', command=command, session_id='A')
    assert client.calls[-1][1]['session_id'] != first
    value, _ = restarted.before_turn(session_id='A', turn_id='new', prompt='Continue')
    assert 'Preserve this accepted plan' in value and first in value


@pytest.mark.parametrize('assignment,attempt', [
    ({'agent_id':'me','session_id':'different'}, {}),
    ({'agent_id':'another-agent'}, {}),
    ({'agent_id':'me'}, {'id':'legacy-attempt'}),
])
def test_runtime_stops_after_session_handoff_or_unidentified_legacy_attempt(tmp_path, assignment, attempt):
    client = Client()
    runtime = Team0AgentRuntime(RuntimeConfig(api_key='key', data_dir=tmp_path), client=client)
    runtime.bind_work(session_id='A', action_id='task')
    client.work_tool = lambda *a, **kw: {'actor_id':'me', 'state':{'assignment':assignment, 'attempt':attempt}}
    value, _ = runtime.before_turn(session_id='A', turn_id='next', prompt='Continue')
    assert 'STOP bound-task execution' in value and 'handoff' in value


def test_failed_claim_does_not_bind_session(tmp_path):
    client = Client()
    runtime = Team0AgentRuntime(RuntimeConfig(api_key='key', data_dir=tmp_path), client=client)
    client.denied = True
    with pytest.raises(ApiError):
        runtime.work_command(action_id='task', command={'command':'accept','idempotency_key':'claim'}, session_id='A')
    client.denied = False
    client.calls.clear()
    runtime.before_turn(session_id='A', turn_id='next', prompt='Continue')
    assert not client.calls
