from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

import httpx
import pytest
from fastmcp import Client
from typer.testing import CliRunner

from agentchat import cli as cli_module
from agentchat.bootstrap import default_notify_state_path, resolve_bootstrap_path, write_bootstrap_payload
from agentchat.cli import app as cli_app, fetch_check
from agentchat.server import Settings, create_app
from agentchat.store import Store, canonicalize_project_key

runner = CliRunner()


@pytest.fixture()
def repo_project(tmp_path: Path) -> Path:
    project = tmp_path / "repo"
    project.mkdir()
    (project / "nested").mkdir()
    return project


@pytest.fixture(autouse=True)
def state_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(root))
    return root


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(db_path=tmp_path / "agentchat.db", host="testserver", port=80)


@pytest.fixture()
def mcp_server(settings: Settings):
    from agentchat.server import build_mcp_server

    return build_mcp_server(settings)


@pytest.mark.asyncio
async def test_register_canonicalizes_git_root(repo_project: Path, mcp_server) -> None:
    child = repo_project / "nested"

    subprocess.run(["git", "init"], cwd=repo_project, check=True, capture_output=True)
    async with Client(mcp_server) as client:
        result = await client.call_tool(
            "agentchat_register",
            {
                "project_key": str(child),
                "agent_name": "alice",
                "program": "codex-cli",
                "model": "gpt-5",
            },
        )
        payload = result.data
        agents = await client.call_tool(
            "agentchat_list_agents",
            {
                "project_key": str(repo_project),
            },
        )
    expected_project_key = canonicalize_project_key(str(repo_project))
    expected_bootstrap = resolve_bootstrap_path(expected_project_key, "alice")
    assert payload["project_key"] == expected_project_key
    assert Path(payload["bootstrap_path"]) == expected_bootstrap
    assert agents.structured_content["result"][0]["agent_name"] == "alice"
    assert stat.S_IMODE(Path(payload["bootstrap_path"]).stat().st_mode) == 0o600


def test_bootstrap_path_cli_resolves_local_state_path(repo_project: Path) -> None:
    subprocess.run(["git", "init"], cwd=repo_project, check=True, capture_output=True)
    nested = repo_project / "nested"
    result = runner.invoke(cli_app, ["bootstrap-path", str(nested), "alice"])
    assert result.exit_code == 0
    assert result.stdout.strip() == str(resolve_bootstrap_path(str(repo_project), "alice"))


def test_resolve_bootstrap_path_ignores_relative_xdg_state_home(
    repo_project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", ".codex")

    path = resolve_bootstrap_path(str(repo_project), "alice")

    assert path.is_relative_to(home / ".local" / "state" / "agentchat" / "bootstrap")


def test_resolve_bootstrap_path_honors_absolute_xdg_state_home(
    repo_project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "custom-state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))

    path = resolve_bootstrap_path(str(repo_project), "alice")

    assert path.is_relative_to(state_root / "agentchat" / "bootstrap")


def test_resolve_bootstrap_path_refuses_project_local_state_root(
    repo_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(repo_project / ".state"))

    with pytest.raises(ValueError, match="project tree"):
        resolve_bootstrap_path(str(repo_project), "alice")


@pytest.mark.asyncio
async def test_project_message_direct_message_mentions_and_ack(repo_project: Path, mcp_server) -> None:
    async with Client(mcp_server) as alice, Client(mcp_server) as bob:
        alice_registration = (
            await alice.call_tool(
                "agentchat_register",
                {
                    "project_key": str(repo_project),
                    "agent_name": "alice",
                    "program": "codex-cli",
                    "model": "gpt-5",
                },
            )
        ).data
        bob_registration = (
            await bob.call_tool(
                "agentchat_register",
                {
                    "project_key": str(repo_project),
                    "agent_name": "bob",
                    "program": "codex-cli",
                    "model": "gpt-5",
                },
            )
        ).data

        await alice.call_tool(
            "agentchat_send_project_message",
            {
                "project_key": str(repo_project),
                "sender_name": "alice",
                "body_md": "Status update for @bob",
                "thread_id": "THREAD-1",
            },
        )

        feed = (
            await alice.call_tool(
                "agentchat_fetch_project_feed",
                {
                    "project_key": str(repo_project),
                    "agent_name": "alice",
                },
            )
        ).data
        assert feed["messages"][0]["scope"] == "project"

        inbox_after_mention = (
            await bob.call_tool(
                "agentchat_fetch_inbox",
                {
                    "project_key": str(repo_project),
                    "agent_name": "bob",
                },
            )
        ).data
        assert inbox_after_mention["messages"][0]["entry_kind"] == "mention"

        await bob.call_tool(
            "agentchat_mark_read",
            {
                "project_key": str(repo_project),
                "agent_name": "bob",
                "message_id": inbox_after_mention["messages"][0]["message_id"],
            },
        )

        dm = (
            await alice.call_tool(
                "agentchat_send_direct_message",
                {
                    "project_key": str(repo_project),
                    "sender_name": "alice",
                    "to": ["bob"],
                    "body_md": "Please review this change.",
                    "thread_id": "THREAD-1",
                    "ack_requested": True,
                },
            )
        ).data

        inbox_after_dm = (
            await bob.call_tool(
                "agentchat_fetch_inbox",
                {
                    "project_key": str(repo_project),
                    "agent_name": "bob",
                    "unread_only": True,
                },
            )
        ).data
        assert inbox_after_dm["messages"][0]["entry_kind"] == "dm"
        assert inbox_after_dm["messages"][0]["ack_requested"] is True

        await bob.call_tool(
            "agentchat_ack",
            {
                "project_key": str(repo_project),
                "agent_name": "bob",
                "message_id": dm["message_id"],
            },
        )

        thread_resource = await bob.read_resource(
            f"resource://agentchat/thread/THREAD-1?project={alice_registration['project_slug']}"
        )
        thread_payload = json.loads(thread_resource[0].text)
        assert len(thread_payload["messages"]) == 2
        assert bob_registration["agent_name"] == "bob"


@pytest.mark.asyncio
async def test_presence_stays_sticky_across_reads_and_check(repo_project: Path, settings: Settings) -> None:
    from agentchat.server import build_mcp_server

    store = Store(settings.db_path, stale_after_seconds=settings.stale_after_seconds)
    server = build_mcp_server(settings, store=store)
    app = create_app(settings)
    async with Client(server) as alice, Client(server) as bob:
        alice_registration = (
            await alice.call_tool(
                "agentchat_register",
                {
                    "project_key": str(repo_project),
                    "agent_name": "alice",
                    "program": "codex-cli",
                    "model": "gpt-5",
                },
            )
        ).data
        await bob.call_tool(
            "agentchat_register",
            {
                "project_key": str(repo_project),
                "agent_name": "bob",
                "program": "codex-cli",
                "model": "gpt-5",
            },
        )
        await alice.call_tool(
            "agentchat_set_presence",
            {
                "project_key": str(repo_project),
                "agent_name": "alice",
                "status": "busy",
            },
        )
        await alice.call_tool(
            "agentchat_fetch_inbox",
            {
                "project_key": str(repo_project),
                "agent_name": "alice",
            },
        )
        await alice.call_tool(
            "agentchat_fetch_project_feed",
            {
                "project_key": str(repo_project),
                "agent_name": "alice",
            },
        )
        await alice.call_tool(
            "agentchat_list_agents",
            {
                "project_key": str(repo_project),
                "agent_name": "alice",
            },
        )

    payload = json.loads(Path(alice_registration["bootstrap_path"]).read_text(encoding="utf-8"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/check",
            json={
                "project_key": payload["project_key"],
                "agent_name": payload["agent_name"],
                "registration_token": payload["registration_token"],
            },
        )
        response.raise_for_status()

    project = store.require_project(str(repo_project))
    agent = store.get_agent(project.id, "alice")
    assert agent is not None
    assert agent.status == "busy"


@pytest.mark.asyncio
async def test_register_refresh_sets_status_online(repo_project: Path, settings: Settings) -> None:
    from agentchat.server import build_mcp_server

    store = Store(settings.db_path, stale_after_seconds=settings.stale_after_seconds)
    server = build_mcp_server(settings, store=store)
    async with Client(server) as alice:
        registration = (
            await alice.call_tool(
                "agentchat_register",
                {
                    "project_key": str(repo_project),
                    "agent_name": "alice",
                    "program": "codex-cli",
                    "model": "gpt-5",
                },
            )
        ).data
        await alice.call_tool(
            "agentchat_set_presence",
            {
                "project_key": str(repo_project),
                "agent_name": "alice",
                "status": "away",
            },
        )
        await alice.call_tool(
            "agentchat_register",
            {
                "project_key": str(repo_project),
                "agent_name": "alice",
                "program": "codex-cli",
                "model": "gpt-5",
                "registration_token": registration["registration_token"],
            },
        )

    project = store.require_project(str(repo_project))
    agent = store.get_agent(project.id, "alice")
    assert agent is not None
    assert agent.status == "online"


@pytest.mark.asyncio
async def test_refresh_failure_restores_previous_agent_state(
    repo_project: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentchat import server as server_module

    store = Store(settings.db_path, stale_after_seconds=settings.stale_after_seconds)
    server = server_module.build_mcp_server(settings, store=store)
    async with Client(server) as alice:
        registration = (
            await alice.call_tool(
                "agentchat_register",
                {
                    "project_key": str(repo_project),
                    "agent_name": "alice",
                    "program": "codex-cli",
                    "model": "gpt-5",
                },
            )
        ).data
        await alice.call_tool(
            "agentchat_set_presence",
            {
                "project_key": str(repo_project),
                "agent_name": "alice",
                "status": "away",
            },
        )

    project = store.require_project(str(repo_project))
    before = store.get_agent(project.id, "alice")
    assert before is not None

    def fail_write_bootstrap(*args: object, **kwargs: object) -> str:
        raise RuntimeError("bootstrap write failed")

    monkeypatch.setattr(server_module, "write_bootstrap", fail_write_bootstrap)
    async with Client(server) as resumed:
        with pytest.raises(Exception, match="bootstrap write failed"):
            await resumed.call_tool(
                "agentchat_register",
                {
                    "project_key": str(repo_project),
                    "agent_name": "alice",
                    "program": "codex-cli-resumed",
                    "model": "gpt-5-mini",
                    "registration_token": registration["registration_token"],
                },
            )

    after = store.get_agent(project.id, "alice")
    assert after is not None
    assert after.registration_token == before.registration_token
    assert after.session_id == before.session_id
    assert after.status == before.status
    assert after.last_seen_at == before.last_seen_at
    assert after.program == before.program
    assert after.model == before.model


@pytest.mark.asyncio
async def test_successful_refresh_rotates_token_and_invalidates_old_token(
    repo_project: Path,
    settings: Settings,
) -> None:
    from agentchat.server import build_mcp_server

    server = build_mcp_server(settings)
    app = create_app(settings)
    async with Client(server) as alice:
        initial = (
            await alice.call_tool(
                "agentchat_register",
                {
                    "project_key": str(repo_project),
                    "agent_name": "alice",
                    "program": "codex-cli",
                    "model": "gpt-5",
                },
            )
        ).data

    old_token = initial["registration_token"]
    old_bootstrap = json.loads(Path(initial["bootstrap_path"]).read_text(encoding="utf-8"))

    async with Client(server) as resumed:
        refreshed = (
            await resumed.call_tool(
                "agentchat_register",
                {
                    "project_key": str(repo_project),
                    "agent_name": "alice",
                    "program": "codex-cli",
                    "model": "gpt-5",
                    "registration_token": old_token,
                },
            )
        ).data

    assert refreshed["registration_token"] != old_token
    new_bootstrap = json.loads(Path(refreshed["bootstrap_path"]).read_text(encoding="utf-8"))
    assert new_bootstrap["registration_token"] == refreshed["registration_token"]

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        old_response = await client.post(
            "/check",
            json={
                "project_key": old_bootstrap["project_key"],
                "agent_name": old_bootstrap["agent_name"],
                "registration_token": old_token,
            },
        )
        assert old_response.status_code == 403

        new_response = await client.post(
            "/check",
            json={
                "project_key": new_bootstrap["project_key"],
                "agent_name": new_bootstrap["agent_name"],
                "registration_token": new_bootstrap["registration_token"],
            },
        )
        new_response.raise_for_status()


@pytest.mark.asyncio
async def test_cursor_polling_and_auth_rejection(repo_project: Path, mcp_server) -> None:
    async with Client(mcp_server) as alice, Client(mcp_server) as intruder:
        await alice.call_tool(
            "agentchat_register",
            {
                "project_key": str(repo_project),
                "agent_name": "alice",
                "program": "codex-cli",
                "model": "gpt-5",
            },
        )
        await intruder.call_tool(
            "agentchat_register",
            {
                "project_key": str(repo_project),
                "agent_name": "mallory",
                "program": "codex-cli",
                "model": "gpt-5",
            },
        )
        for index in range(3):
            await alice.call_tool(
                "agentchat_send_project_message",
                {
                    "project_key": str(repo_project),
                    "sender_name": "alice",
                    "body_md": f"message {index}",
                },
            )
        first_page = (
            await alice.call_tool(
                "agentchat_fetch_project_feed",
                {
                    "project_key": str(repo_project),
                    "agent_name": "alice",
                    "limit": 1,
                },
            )
        ).data
        second_page = (
            await alice.call_tool(
                "agentchat_fetch_project_feed",
                {
                    "project_key": str(repo_project),
                    "agent_name": "alice",
                    "limit": 5,
                    "cursor": first_page["next_cursor"],
                },
            )
        ).data
        assert len(first_page["messages"]) == 1
        assert len(second_page["messages"]) == 2

        with pytest.raises(Exception, match="authenticated as 'mallory'"):
            await intruder.call_tool(
                "agentchat_fetch_project_feed",
                {
                    "project_key": str(repo_project),
                    "agent_name": "alice",
                },
            )
    async with Client(mcp_server) as anonymous:
        with pytest.raises(Exception, match="requires agent_name plus registration_token"):
            await anonymous.call_tool(
                "agentchat_list_agents",
                {
                    "project_key": str(repo_project),
                },
            )


@pytest.mark.asyncio
async def test_check_endpoint_uses_bootstrap_artifact(repo_project: Path, settings: Settings) -> None:
    app = create_app(settings)
    from agentchat.server import build_mcp_server

    server = build_mcp_server(settings)
    async with Client(server) as alice, Client(server) as bob:
        alice_registration = (
            await alice.call_tool(
                "agentchat_register",
                {
                    "project_key": str(repo_project),
                    "agent_name": "alice",
                    "program": "codex-cli",
                    "model": "gpt-5",
                },
            )
        ).data
        await bob.call_tool(
            "agentchat_register",
            {
                "project_key": str(repo_project),
                "agent_name": "bob",
                "program": "codex-cli",
                "model": "gpt-5",
            },
        )
        await bob.call_tool(
            "agentchat_send_direct_message",
            {
                "project_key": str(repo_project),
                "sender_name": "bob",
                "to": ["alice"],
                "body_md": "Needs your input.",
                "ack_requested": True,
            },
        )

    transport = httpx.ASGITransport(app=app)
    payload = json.loads(Path(alice_registration["bootstrap_path"]).read_text(encoding="utf-8"))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        original = fetch_check

        async def fake_fetch_check(_: dict[str, object], timeout_seconds: float = 3.0) -> dict[str, object]:
            response = await client.post(
                "/check",
                json={
                    "project_key": payload["project_key"],
                    "agent_name": payload["agent_name"],
                    "registration_token": payload["registration_token"],
                },
            )
            response.raise_for_status()
            return response.json()

        result = await fake_fetch_check(payload)
        assert result["direct_unread"] == 1
        assert result["mention_unread"] == 0
        assert original is fetch_check


def test_notify_re_emits_after_unread_resets_with_default_state_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap = tmp_path / "alice.json"
    bootstrap.write_text(
        json.dumps(
            {
                "project_key": "/tmp/repo",
                "project_slug": "repo-1234",
                "agent_name": "alice",
                "registration_token": "token",
                "server_url": "http://127.0.0.1:8787",
            }
        ),
        encoding="utf-8",
    )
    results = iter(
        [
            {
                "ok": True,
                "project_key": "/tmp/repo",
                "project_slug": "repo-1234",
                "agent_name": "alice",
                "direct_unread": 1,
                "mention_unread": 0,
                "total_unread": 1,
            },
            {
                "ok": True,
                "project_key": "/tmp/repo",
                "project_slug": "repo-1234",
                "agent_name": "alice",
                "direct_unread": 0,
                "mention_unread": 0,
                "total_unread": 0,
            },
            {
                "ok": True,
                "project_key": "/tmp/repo",
                "project_slug": "repo-1234",
                "agent_name": "alice",
                "direct_unread": 1,
                "mention_unread": 0,
                "total_unread": 1,
            },
        ]
    )

    async def fake_fetch_check(_: dict[str, object], timeout_seconds: float = 3.0) -> dict[str, object]:
        return next(results)

    monkeypatch.setattr(cli_module, "fetch_check", fake_fetch_check)
    state_file = default_notify_state_path(bootstrap)
    args = ["check", str(bootstrap), "--notify", "--min-interval-seconds", "120"]
    first = runner.invoke(cli_app, args)
    second = runner.invoke(cli_app, args)
    third = runner.invoke(cli_app, args)

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert third.exit_code == 0
    assert "1 unread direct messages" in first.stdout
    assert second.stdout == ""
    assert "1 unread direct messages" in third.stdout
    assert state_file.exists()
    assert stat.S_IMODE(state_file.stat().st_mode) == 0o600


def test_write_bootstrap_payload_is_atomic_for_regular_files(repo_project: Path) -> None:
    path = resolve_bootstrap_path(str(repo_project), "alice")
    write_bootstrap_payload(path, {"agent_name": "alice", "version": 1})
    write_bootstrap_payload(path, {"agent_name": "alice", "version": 2})

    assert json.loads(path.read_text(encoding="utf-8")) == {"agent_name": "alice", "version": 2}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []


def test_write_bootstrap_payload_refuses_symlinked_managed_ancestor(
    repo_project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state-home"
    redirect = tmp_path / "redirected-state"
    state_root.mkdir()
    redirect.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))
    (state_root / "agentchat").symlink_to(redirect, target_is_directory=True)

    path = resolve_bootstrap_path(str(repo_project), "alice")
    with pytest.raises(ValueError, match="symlink"):
        write_bootstrap_payload(path, {"agent_name": "alice"})

    assert not (redirect / "bootstrap").exists()


@pytest.mark.asyncio
async def test_register_refuses_symlink_bootstrap_path(
    repo_project: Path,
    settings: Settings,
    tmp_path: Path,
) -> None:
    from agentchat.server import build_mcp_server

    store = Store(settings.db_path, stale_after_seconds=settings.stale_after_seconds)
    server = build_mcp_server(settings, store=store)
    path = resolve_bootstrap_path(str(repo_project), "alice")
    path.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "outside.json"
    target.write_text("leave me alone", encoding="utf-8")
    path.symlink_to(target)

    async with Client(server) as alice:
        with pytest.raises(Exception, match="symlink"):
            await alice.call_tool(
                "agentchat_register",
                {
                    "project_key": str(repo_project),
                    "agent_name": "alice",
                    "program": "codex-cli",
                    "model": "gpt-5",
                },
            )

    assert target.read_text(encoding="utf-8") == "leave me alone"
    project = store.find_project(str(repo_project))
    assert project is not None
    assert store.get_agent(project.id, "alice") is None


@pytest.mark.asyncio
async def test_duplicate_direct_recipients_do_not_break_delivery(repo_project: Path, mcp_server) -> None:
    async with Client(mcp_server) as alice, Client(mcp_server) as bob:
        await alice.call_tool(
            "agentchat_register",
            {
                "project_key": str(repo_project),
                "agent_name": "alice",
                "program": "codex-cli",
                "model": "gpt-5",
            },
        )
        await bob.call_tool(
            "agentchat_register",
            {
                "project_key": str(repo_project),
                "agent_name": "bob",
                "program": "codex-cli",
                "model": "gpt-5",
            },
        )
        await alice.call_tool(
            "agentchat_send_direct_message",
            {
                "project_key": str(repo_project),
                "sender_name": "alice",
                "to": ["bob", "bob"],
                "body_md": "one copy only",
            },
        )
        inbox = (
            await bob.call_tool(
                "agentchat_fetch_inbox",
                {
                    "project_key": str(repo_project),
                    "agent_name": "bob",
                },
            )
        ).data
        assert len(inbox["messages"]) == 1
