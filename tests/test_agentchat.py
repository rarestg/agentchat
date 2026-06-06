from __future__ import annotations

import json
import stat
from pathlib import Path

import httpx
import pytest
from fastmcp import Client

from agentchat.cli import fetch_check
from agentchat.server import Settings, create_app
from agentchat.store import canonicalize_project_key


@pytest.fixture()
def repo_project(tmp_path: Path) -> Path:
    project = tmp_path / "repo"
    project.mkdir()
    (project / "nested").mkdir()
    return project


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
    import subprocess

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
    assert payload["project_key"] == canonicalize_project_key(str(repo_project))
    assert payload["bootstrap_path"].endswith(".codex/agentchat/alice.json")
    assert agents.structured_content["result"][0]["agent_name"] == "alice"
    assert stat.S_IMODE(Path(payload["bootstrap_path"]).stat().st_mode) == 0o600


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
