"""Tests for LLM backend selection and the claude-cli (subscription) backend."""

import json
import subprocess

import pytest

from readme2demo import llm
from readme2demo.engines.base import EngineError
from readme2demo.engines.claude_code import ClaudeCodeEngine
from readme2demo.llm import LLMError


@pytest.fixture(autouse=True)
def reset_backend():
    llm.set_backend("auto")
    yield
    llm.set_backend("auto")


# -- resolve_backend ------------------------------------------------------------


def test_auto_prefers_api_when_key_set(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert llm.resolve_backend() == "api"


def test_auto_falls_back_to_cli(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("readme2demo.llm.shutil.which", lambda _: "/usr/local/bin/claude")
    assert llm.resolve_backend() == "claude-cli"


def test_auto_raises_when_nothing_available(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("readme2demo.llm.shutil.which", lambda _: None)
    with pytest.raises(LLMError, match="No LLM backend available"):
        llm.resolve_backend()


def test_explicit_backend_wins(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    llm.set_backend("claude-cli")
    assert llm.resolve_backend() == "claude-cli"


def test_set_backend_rejects_unknown():
    with pytest.raises(LLMError, match="Unknown LLM backend"):
        llm.set_backend("gpt")


# -- claude-cli backend -----------------------------------------------------------


def _fake_cli_envelope(result_text: str, cost: float = 0.0123, is_error: bool = False):
    return json.dumps(
        {"type": "result", "result": result_text, "total_cost_usd": cost,
         "is_error": is_error, "num_turns": 1}
    )


def test_cli_backend_parses_envelope(monkeypatch):
    monkeypatch.setattr("readme2demo.llm.shutil.which", lambda _: "/bin/claude")
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input", "")
        return subprocess.CompletedProcess(
            cmd, 0, stdout=_fake_cli_envelope('{"feasible": true}'), stderr=""
        )

    monkeypatch.setattr("readme2demo.llm.subprocess.run", fake_run)
    resp = llm._complete_cli("be a planner", "plan this", "claude-sonnet-5")
    assert resp.text == '{"feasible": true}'
    assert resp.cost_usd == 0.0123
    assert captured["cmd"][:4] == ["claude", "-p", "--output-format", "json"]
    assert "--model" in captured["cmd"]
    assert "be a planner" in captured["input"]  # system prompt travels via stdin
    assert "plan this" in captured["input"]


def test_cli_backend_error_envelope(monkeypatch):
    monkeypatch.setattr("readme2demo.llm.shutil.which", lambda _: "/bin/claude")
    monkeypatch.setattr(
        "readme2demo.llm.subprocess.run",
        lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 0, stdout=_fake_cli_envelope("rate limited", is_error=True), stderr=""
        ),
    )
    with pytest.raises(LLMError, match="reported an error"):
        llm._complete_cli("s", "u", "m")


def test_cli_backend_nonzero_exit(monkeypatch):
    monkeypatch.setattr("readme2demo.llm.shutil.which", lambda _: "/bin/claude")
    monkeypatch.setattr(
        "readme2demo.llm.subprocess.run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom"),
    )
    with pytest.raises(LLMError, match="failed"):
        llm._complete_cli("s", "u", "m")


def test_cli_backend_missing_binary(monkeypatch):
    monkeypatch.setattr("readme2demo.llm.shutil.which", lambda _: None)
    with pytest.raises(LLMError, match="claude CLI not found"):
        llm._complete_cli("s", "u", "m")


def test_complete_dispatches_to_cli(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("readme2demo.llm.shutil.which", lambda _: "/bin/claude")
    monkeypatch.setattr(
        "readme2demo.llm.subprocess.run",
        lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 0, stdout=_fake_cli_envelope("hello"), stderr=""
        ),
    )
    resp = llm.complete("s", "u", "claude-sonnet-5")
    assert resp.text == "hello"


# -- engine auth: API key OR OAuth token --------------------------------------------


def test_engine_env_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-abcdefghijklmnop")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    assert ClaudeCodeEngine().resolve_env() == {
        "ANTHROPIC_API_KEY": "sk-ant-test-abcdefghijklmnop"
    }


def test_engine_env_oauth_fallback(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-abcdefghijklmnop")
    assert ClaudeCodeEngine().resolve_env() == {
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-abcdefghijklmnop"
    }


def test_engine_env_neither_raises(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    with pytest.raises(EngineError, match="setup-token"):
        ClaudeCodeEngine().resolve_env()


CORRUPTED_TOKEN = (
    "\x1b[?2004hWelcome to Claude Code v2.1.198\n · Opening browser to sign in…"
)


def test_engine_env_rejects_corrupted_token(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", CORRUPTED_TOKEN)
    with pytest.raises(EngineError, match="doesn't look like a credential"):
        ClaudeCodeEngine().resolve_env()


def test_engine_env_strips_valid_token(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", " sk-ant-oat01-abcdefghijkl \n")
    env = ClaudeCodeEngine().resolve_env()
    assert env == {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-abcdefghijkl"}


# -- host claude -p env sanitation ------------------------------------------------


def test_sanitized_env_drops_corrupted_credential(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", CORRUPTED_TOKEN)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-valid-key-abcdefgh")
    env = llm._sanitized_env()
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env  # garbage dropped
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-valid-key-abcdefgh"  # valid kept


def test_cli_subprocess_gets_sanitized_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", CORRUPTED_TOKEN)
    monkeypatch.setattr("readme2demo.llm.shutil.which", lambda _: "/bin/claude")
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(
            cmd, 0, stdout=_fake_cli_envelope("ok"), stderr=""
        )

    monkeypatch.setattr("readme2demo.llm.subprocess.run", fake_run)
    llm._complete_cli("s", "u", "m")
    assert captured["env"] is not None
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in captured["env"]


# -- --allow-docker-socket plumbing --------------------------------------------------


def test_agent_prompt_guide_mode_demands_every_step(tmp_path):
    from pathlib import Path as P

    from readme2demo.agent import render_agent_prompt
    from readme2demo.types import Plan, SuccessCriteria

    plan = Plan(
        quickstart_summary="q",
        success_criteria=SuccessCriteria(command="./run.sh"),
        guide_path="step_by_step.md",
    )
    rendered = render_agent_prompt(plan, P("src/readme2demo/prompts/agent.md"))
    assert "execute EVERY step" in rendered
    assert "SKIPPED_STEP:" in rendered
    assert "does NOT finish the run" in rendered


def test_docker_socket_mounted_when_enabled(tmp_path, monkeypatch):
    from readme2demo import agent as agent_mod
    from readme2demo.config import Config
    from readme2demo.types import Plan, SuccessCriteria

    captured: dict = {}

    class FakeSandbox:
        def __init__(self, **kw):
            captured.update(kw)

        def start(self):
            raise RuntimeError("stop here")  # abort after construction

        def destroy(self):
            pass

    monkeypatch.setattr(agent_mod, "Sandbox", FakeSandbox)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-abcdefghijklmnop")
    (tmp_path / "repo").mkdir()
    plan = Plan(quickstart_summary="q", success_criteria=SuccessCriteria(command="x"))
    from readme2demo.engines import get_engine

    for enabled, expected in ((True, True), (False, False)):
        captured.clear()
        cfg = Config(allow_docker_socket=enabled)
        try:
            agent_mod.run_agent(tmp_path, plan, get_engine("claude-code"), cfg)
        except RuntimeError:
            pass
        has_socket = any(
            src == "/var/run/docker.sock" for src, _, _ in captured["mounts"]
        )
        assert has_socket is expected


def test_render_cmd_includes_socket_when_enabled(tmp_path, monkeypatch):
    import subprocess

    from readme2demo import render as render_mod
    from readme2demo.config import Config

    (tmp_path / "demo.tape").write_text("Output demo.mp4\nType \"x\"\nEnter\nSleep 2s\n")
    captured: dict = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(render_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(render_mod, "check_render_image", lambda img: None)
    monkeypatch.setattr(render_mod, "_generate_gif_preview", lambda *a: None)
    monkeypatch.setattr(render_mod, "validate_outputs", lambda *a, **k: [])

    render_mod.run_render(tmp_path, Config(allow_docker_socket=True))
    assert "/var/run/docker.sock:/var/run/docker.sock" in captured["cmd"]
    render_mod.run_render(tmp_path, Config(allow_docker_socket=False))
    assert "/var/run/docker.sock:/var/run/docker.sock" not in captured["cmd"]


def test_docker_socket_gid_probe(monkeypatch):
    import subprocess

    from readme2demo import sandbox as sandbox_mod

    monkeypatch.setattr(
        sandbox_mod.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="999\n", stderr=""),
    )
    assert sandbox_mod.docker_socket_gid("img") == "999"
    # probe failure falls back to root group
    monkeypatch.setattr(
        sandbox_mod.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="err"),
    )
    assert sandbox_mod.docker_socket_gid("img") == "0"


def test_sandbox_start_includes_group_add(monkeypatch):
    from readme2demo.sandbox import ExecResult, Sandbox

    captured: dict = {}

    def fake_run(cmd, timeout=None, stream_to=None):
        captured["cmd"] = cmd
        return ExecResult(0, "cid")

    sb = Sandbox(image="img", group_add="999")
    monkeypatch.setattr(sb, "_run", fake_run)
    sb.start()
    assert "--group-add" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--group-add") + 1] == "999"
    sb2 = Sandbox(image="img")
    monkeypatch.setattr(sb2, "_run", fake_run)
    sb2.start()
    assert "--group-add" not in captured["cmd"]


def test_render_socket_includes_group_add(tmp_path, monkeypatch):
    import subprocess

    from readme2demo import render as render_mod
    from readme2demo import sandbox as sandbox_mod
    from readme2demo.config import Config

    (tmp_path / "demo.tape").write_text('Output demo.mp4\nType "x"\nEnter\nSleep 2s\n')
    captured: dict = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(render_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(render_mod, "check_render_image", lambda img: None)
    monkeypatch.setattr(render_mod, "_generate_gif_preview", lambda *a: None)
    monkeypatch.setattr(render_mod, "validate_outputs", lambda *a, **k: [])
    monkeypatch.setattr(sandbox_mod, "docker_socket_gid", lambda img: "999")

    render_mod.run_render(tmp_path, Config(allow_docker_socket=True))
    assert "--group-add" in captured["cmd"]
    assert "999" in captured["cmd"]
