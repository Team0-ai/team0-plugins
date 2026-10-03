from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "team0_hermes_plugin", ROOT / "__init__.py",
    submodule_search_locations=[str(ROOT)],
)
PLUGIN = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PLUGIN
SPEC.loader.exec_module(PLUGIN)
adapter_module = sys.modules["team0_hermes_plugin.hermes_adapter"]


class RuntimeDouble:
    def __init__(self):
        self.prepared = []
        self.completed = []

    def before_turn(self, **kwargs):
        self.prepared.append(kwargs)
        return "Team0 context", None

    def after_turn(self, **kwargs):
        self.completed.append(kwargs)


def test_hermes_hooks_read_before_and_contribute_only_completed_turns():
    runtime = RuntimeDouble()
    adapter = adapter_module.HermesAdapter(runtime)

    injected = adapter.before_turn("session-a", "What next?", turn_id="host-1")
    assert injected["context"].startswith("Team0 context")
    assert "Do not call team0_contribute_conversation" in injected["context"]
    assert runtime.prepared[0]["prompt"] == "What next?"
    adapter.after_turn("session-a", "What next?", "Do this.", turn_id="host-1")
    assert runtime.completed == [{
        "turn_id": runtime.prepared[0]["turn_id"],
        "assistant_message": "Do this.",
    }]
    adapter.after_turn("session-a", "What next?", "Duplicate.", turn_id="host-1")
    assert len(runtime.completed) == 1


def test_hermes_hooks_skip_subagents_empty_turns_and_unpaired_responses():
    runtime = RuntimeDouble()
    adapter = adapter_module.HermesAdapter(runtime)

    assert adapter.before_turn("child", "A task", parent_session_id="parent") is None
    assert adapter.before_turn("session-a", [{"type": "image_url", "image_url": "private"}]) is None
    adapter.after_turn("session-a", "Never read", "Do not contribute")
    assert runtime.prepared == []
    assert runtime.completed == []


def test_hermes_multimodal_text_and_repeated_prompts_remain_correlated():
    runtime = RuntimeDouble()
    adapter = adapter_module.HermesAdapter(runtime)
    message = [{"type": "text", "text": "First"}, {"type": "image_url", "image_url": "private"}]

    adapter.before_turn("session-a", message)
    adapter.before_turn("session-a", message)
    adapter.after_turn("session-a", message, "One")
    adapter.after_turn("session-a", message, "Two")

    assert [call["prompt"] for call in runtime.prepared] == ["First", "First"]
    assert [call["turn_id"] for call in runtime.completed] == [
        call["turn_id"] for call in runtime.prepared
    ]


def test_hermes_correlates_when_one_hook_omits_turn_id():
    runtime = RuntimeDouble()
    adapter = adapter_module.HermesAdapter(runtime)

    adapter.before_turn("session-a", "Question", turn_id="host-turn")
    adapter.after_turn("session-a", "Question", "Answer")

    assert runtime.completed[0]["turn_id"] == runtime.prepared[0]["turn_id"]


def test_interrupted_turn_cannot_be_contributed_by_later_identical_prompt():
    runtime = RuntimeDouble()
    adapter = adapter_module.HermesAdapter(runtime)

    adapter.before_turn("session-a", "Question", turn_id="interrupted")
    adapter.end_turn(session_id="session-a", turn_id="interrupted", interrupted=True)
    adapter.before_turn("session-a", "Question", turn_id="new")
    adapter.after_turn("session-a", "Question", "Answer", turn_id="new")

    assert runtime.completed == [{
        "turn_id": runtime.prepared[1]["turn_id"],
        "assistant_message": "Answer",
    }]


def test_hermes_uses_only_its_own_key_and_profile_state(monkeypatch, tmp_path):
    created = []
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TEAM0_HERMES_API_KEY", "hermes-key")
    monkeypatch.setenv("TEAM0_API_KEY", "other-host-key")
    monkeypatch.setenv("PLUGIN_DATA", str(tmp_path / "other-host"))
    monkeypatch.setenv("TEAM0_CONTRIBUTION_SOURCE_ID", "other-source")
    monkeypatch.setattr(adapter_module, "Team0AgentRuntime", lambda config: created.append(config))

    adapter_module._create_runtime()

    assert created[0].api_key == "hermes-key"
    assert created[0].host_id == "hermes"
    assert created[0].data_dir == tmp_path / "team0-agent-runtime"
    assert created[0].contribution_source_id is None


def test_plugin_registers_both_documented_hermes_hooks(monkeypatch):
    runtime = RuntimeDouble()
    hooks = {}

    class Context:
        def register_hook(self, name, callback):
            hooks[name] = callback

    monkeypatch.setattr(adapter_module, "_create_runtime", lambda: runtime)
    PLUGIN.register(Context())

    assert set(hooks) == {"pre_llm_call", "post_llm_call", "on_session_end"}
    hooks["pre_llm_call"](session_id="s", user_message="Question", turn_id="t")
    hooks["post_llm_call"](session_id="s", user_message="Question", assistant_response="Answer", turn_id="t")
    assert len(runtime.prepared) == len(runtime.completed) == 1


def test_unconfigured_plugin_registers_no_hooks(monkeypatch):
    monkeypatch.setattr(adapter_module, "_create_runtime", lambda: None)

    class Context:
        def register_hook(self, *_args):
            raise AssertionError("No hook should be registered without this host's key")

    PLUGIN.register(Context())
