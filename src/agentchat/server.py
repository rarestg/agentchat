from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from fastmcp import Context, FastMCP

from .bootstrap import resolve_bootstrap_path, write_bootstrap_payload
from .store import Agent, Project, Store


@dataclass(frozen=True)
class Settings:
    db_path: Path
    host: str = "127.0.0.1"
    port: int = 8787
    stale_after_seconds: int = 1800
    session_binding_ttl_seconds: int = 86400

    @property
    def server_url(self) -> str:
        return f"http://{self.host}:{self.port}"


class CheckRequest(BaseModel):
    project_key: str
    agent_name: str
    registration_token: str


class CheckResponse(BaseModel):
    ok: bool
    project_key: str
    project_slug: str
    agent_name: str
    direct_unread: int
    mention_unread: int
    total_unread: int


def _session_key(ctx: Context) -> str:
    with suppress(Exception):
        if ctx.session_id:
            return str(ctx.session_id)
    fallback = getattr(ctx, "_agentchat_fallback_session", None)
    if isinstance(fallback, str):
        return fallback
    value = f"session:{id(ctx)}"
    with suppress(Exception):
        ctx._agentchat_fallback_session = value  # type: ignore[attr-defined]
    return value


def bootstrap_path(project: Project, agent_name: str) -> Path:
    return resolve_bootstrap_path(project.project_key, agent_name)


def write_bootstrap(project: Project, agent: Agent, settings: Settings) -> str:
    path = bootstrap_path(project, agent.agent_name)
    payload = {
        "project_key": project.project_key,
        "project_slug": project.project_slug,
        "agent_name": agent.agent_name,
        "agent_id": agent.id,
        "registration_token": agent.registration_token,
        "server_url": settings.server_url,
    }
    return str(write_bootstrap_payload(path, payload))


def build_mcp_server(settings: Settings, store: Store | None = None) -> FastMCP:
    state = store or Store(settings.db_path, stale_after_seconds=settings.stale_after_seconds)
    mcp = FastMCP(
        name="agentchat",
        instructions=(
            "Local project-scoped agent messaging. Register once per session, then use project feed, "
            "direct messages, inbox reads, acknowledgements, and resources for project state."
        ),
    )

    session_bindings: dict[str, dict[int, int]] = {}
    session_last_access: dict[str, float] = {}

    def touch_session(session_key: str) -> None:
        session_last_access[session_key] = asyncio.get_event_loop().time()
        expiry = session_last_access[session_key] - settings.session_binding_ttl_seconds
        stale_keys = [key for key, last_seen in session_last_access.items() if last_seen < expiry]
        for key in stale_keys:
            session_last_access.pop(key, None)
            session_bindings.pop(key, None)

    def bind_session(ctx: Context, project: Project, agent: Agent) -> None:
        key = _session_key(ctx)
        touch_session(key)
        project_map = session_bindings.setdefault(key, {})
        project_map[project.id] = agent.id

    def resolve_session_agent(ctx: Context, project: Project) -> Agent | None:
        key = _session_key(ctx)
        touch_session(key)
        agent_id = session_bindings.get(key, {}).get(project.id)
        if agent_id is None:
            return None
        return state.get_agent_by_id(agent_id)

    def visible_project_ids_for_session(ctx: Context) -> set[int]:
        key = _session_key(ctx)
        touch_session(key)
        return set(session_bindings.get(key, {}).values())

    def parse_embedded_query(path_value: str) -> tuple[str, dict[str, list[str]]]:
        if "?" not in path_value:
            return path_value, {}
        path_part, _, query = path_value.partition("?")
        return path_part, parse_qs(query, keep_blank_values=False)

    def ensure_project(raw_project_key: str) -> Project:
        return state.ensure_project(raw_project_key)

    def require_project(raw_project_key: str) -> Project:
        return state.require_project(raw_project_key)

    def authenticate(
        ctx: Context,
        project: Project,
        agent_name: str | None,
        registration_token: str | None,
        action: str,
    ) -> Agent:
        session_agent = resolve_session_agent(ctx, project)
        if session_agent is not None:
            if agent_name and session_agent.agent_name.lower() != agent_name.lower():
                raise ValueError(
                    f"{action} is authenticated as '{session_agent.agent_name}', not '{agent_name}'."
                )
            state.touch_agent(session_agent.id, session_id=_session_key(ctx))
            return session_agent
        if not agent_name:
            raise ValueError(f"{action} requires agent_name plus registration_token or an authenticated session.")
        agent = state.authenticate_agent(project, agent_name, registration_token)
        bind_session(ctx, project, agent)
        state.touch_agent(agent.id, session_id=_session_key(ctx))
        return agent

    @mcp.tool(name="agentchat_register")
    async def agentchat_register(
        ctx: Context,
        project_key: str,
        agent_name: str,
        program: str,
        model: str,
        registration_token: str | None = None,
    ) -> dict[str, Any]:
        project = ensure_project(project_key)
        session_id = _session_key(ctx)
        previous_agent = state.get_agent(project.id, agent_name)
        if previous_agent is None:
            agent = state.create_agent(
                project,
                agent_name,
                program,
                model,
                session_id,
                registration_token,
            )
        else:
            previous_agent, agent = state.refresh_agent_registration(
                project,
                agent_name,
                program,
                model,
                session_id,
                registration_token,
            )
        try:
            bootstrap = write_bootstrap(project, agent, settings)
        except Exception:
            # This recovers ordinary write failures, but it is not a full transaction across
            # SQLite and the filesystem. A process crash after the DB update but before the
            # bootstrap replace can still leave the rotated token without a durable artifact.
            if previous_agent is None:
                state.delete_agent(agent.id)
            else:
                state.restore_agent(previous_agent)
            raise
        bind_session(ctx, project, agent)
        return {
            "agent_id": agent.id,
            "agent_name": agent.agent_name,
            "project_key": project.project_key,
            "project_slug": project.project_slug,
            "registration_token": agent.registration_token,
            "bootstrap_path": bootstrap,
        }

    @mcp.tool(name="agentchat_list_agents")
    async def agentchat_list_agents(
        ctx: Context,
        project_key: str,
        agent_name: str | None = None,
        registration_token: str | None = None,
    ) -> list[dict[str, Any]]:
        project = require_project(project_key)
        authenticate(ctx, project, agent_name, registration_token, "agentchat_list_agents")
        return state.list_active_agents(project)

    @mcp.tool(name="agentchat_send_project_message")
    async def agentchat_send_project_message(
        ctx: Context,
        project_key: str,
        sender_name: str,
        body_md: str,
        thread_id: str | None = None,
        registration_token: str | None = None,
    ) -> dict[str, int]:
        project = require_project(project_key)
        sender = authenticate(ctx, project, sender_name, registration_token, "agentchat_send_project_message")
        message_id = state.send_project_message(project, sender, body_md, thread_id=thread_id)
        return {"message_id": message_id}

    @mcp.tool(name="agentchat_send_direct_message")
    async def agentchat_send_direct_message(
        ctx: Context,
        project_key: str,
        sender_name: str,
        to: list[str],
        body_md: str,
        thread_id: str | None = None,
        ack_requested: bool = False,
        registration_token: str | None = None,
    ) -> dict[str, int]:
        project = require_project(project_key)
        sender = authenticate(ctx, project, sender_name, registration_token, "agentchat_send_direct_message")
        message_id = state.send_direct_message(
            project,
            sender,
            recipients=to,
            body_md=body_md,
            thread_id=thread_id,
            ack_requested=ack_requested,
        )
        return {"message_id": message_id}

    @mcp.tool(name="agentchat_fetch_inbox")
    async def agentchat_fetch_inbox(
        ctx: Context,
        project_key: str,
        agent_name: str | None = None,
        cursor: str | None = None,
        since_ts: str | None = None,
        limit: int = 50,
        unread_only: bool = False,
        registration_token: str | None = None,
    ) -> dict[str, Any]:
        project = require_project(project_key)
        agent = authenticate(ctx, project, agent_name, registration_token, "agentchat_fetch_inbox")
        return state.fetch_inbox(project, agent, cursor, since_ts, max(1, min(limit, 200)), unread_only)

    @mcp.tool(name="agentchat_fetch_project_feed")
    async def agentchat_fetch_project_feed(
        ctx: Context,
        project_key: str,
        cursor: str | None = None,
        since_ts: str | None = None,
        limit: int = 50,
        agent_name: str | None = None,
        registration_token: str | None = None,
    ) -> dict[str, Any]:
        project = require_project(project_key)
        authenticate(ctx, project, agent_name, registration_token, "agentchat_fetch_project_feed")
        return state.fetch_project_feed(project, cursor, since_ts, max(1, min(limit, 200)))

    @mcp.tool(name="agentchat_mark_read")
    async def agentchat_mark_read(
        ctx: Context,
        project_key: str,
        agent_name: str,
        message_id: int,
        registration_token: str | None = None,
    ) -> dict[str, bool]:
        project = require_project(project_key)
        agent = authenticate(ctx, project, agent_name, registration_token, "agentchat_mark_read")
        state.mark_read(project, agent, message_id)
        return {"ok": True}

    @mcp.tool(name="agentchat_ack")
    async def agentchat_ack(
        ctx: Context,
        project_key: str,
        agent_name: str,
        message_id: int,
        registration_token: str | None = None,
    ) -> dict[str, bool]:
        project = require_project(project_key)
        agent = authenticate(ctx, project, agent_name, registration_token, "agentchat_ack")
        state.ack(project, agent, message_id)
        return {"ok": True}

    @mcp.tool(name="agentchat_set_presence")
    async def agentchat_set_presence(
        ctx: Context,
        project_key: str,
        agent_name: str,
        status: str,
        registration_token: str | None = None,
    ) -> dict[str, Any]:
        project = require_project(project_key)
        authenticate(ctx, project, agent_name, registration_token, "agentchat_set_presence")
        agent = state.set_presence(project, agent_name, status)
        return {
            "agent_name": agent.agent_name,
            "status": agent.status,
            "last_seen_at": agent.last_seen_at,
        }

    @mcp.tool(name="agentchat_ping")
    async def agentchat_ping() -> dict[str, str]:
        return {"status": "ok"}

    @mcp.resource("resource://agentchat/projects")
    async def projects_resource(ctx: Context) -> dict[str, Any]:
        return {"projects": state.projects_for_agent_ids(visible_project_ids_for_session(ctx))}

    @mcp.resource("resource://agentchat/project/{project_slug}")
    async def project_resource(ctx: Context, project_slug: str) -> dict[str, Any]:
        project = state.get_project_by_slug(project_slug)
        authenticate(ctx, project, None, None, "project resource")
        return {
            "project_key": project.project_key,
            "project_slug": project.project_slug,
            "created_at": project.created_at,
        }

    @mcp.resource("resource://agentchat/agents/{project_slug}")
    async def agents_resource(ctx: Context, project_slug: str) -> dict[str, Any]:
        project = state.get_project_by_slug(project_slug)
        authenticate(ctx, project, None, None, "agents resource")
        return {"agents": state.list_active_agents(project)}

    @mcp.resource("resource://agentchat/inbox/{agent_id}{?project,agent,agent_token}")
    async def inbox_resource(
        ctx: Context,
        agent_id: int | str,
        project: str | None = None,
        agent: str | None = None,
        agent_token: str | None = None,
    ) -> dict[str, Any]:
        if isinstance(agent_id, str):
            raw_agent_id, parsed = parse_embedded_query(agent_id)
            if project is None and parsed.get("project"):
                project = parsed["project"][0]
            if agent is None and parsed.get("agent"):
                agent = parsed["agent"][0]
            if agent_token is None and parsed.get("agent_token"):
                agent_token = parsed["agent_token"][0]
            agent_id = int(raw_agent_id)
        if project is None:
            raise ValueError("Inbox resource requires project=<project_slug>.")
        project_row = state.get_project_by_slug(project)
        viewer = authenticate(ctx, project_row, agent, agent_token, "inbox resource")
        if viewer.id != agent_id:
            raise ValueError("Inbox resource is only readable by that authenticated agent.")
        return state.fetch_inbox(project_row, viewer, cursor=None, since_ts=None, limit=200, unread_only=False)

    @mcp.resource("resource://agentchat/feed/{project_slug}")
    async def feed_resource(ctx: Context, project_slug: str) -> dict[str, Any]:
        project = state.get_project_by_slug(project_slug)
        authenticate(ctx, project, None, None, "feed resource")
        return state.fetch_project_feed(project, cursor=None, since_ts=None, limit=200)

    @mcp.resource("resource://agentchat/thread/{thread_id}{?project,agent,agent_token}")
    async def thread_resource(
        ctx: Context,
        thread_id: str,
        project: str | None = None,
        agent: str | None = None,
        agent_token: str | None = None,
    ) -> dict[str, Any]:
        thread_lookup = thread_id
        thread_lookup, parsed = parse_embedded_query(thread_lookup)
        if project is None and parsed.get("project"):
            project = parsed["project"][0]
        if agent is None and parsed.get("agent"):
            agent = parsed["agent"][0]
        if agent_token is None and parsed.get("agent_token"):
            agent_token = parsed["agent_token"][0]
        if project is None:
            raise ValueError("Thread resource requires project=<project_slug>.")
        project_row = state.get_project_by_slug(project)
        viewer = authenticate(ctx, project_row, agent, agent_token, "thread resource")
        return {"messages": state.visible_thread_messages(project_row, viewer, thread_lookup)}

    return mcp


def create_app(settings: Settings) -> FastAPI:
    store = Store(settings.db_path, stale_after_seconds=settings.stale_after_seconds)
    mcp = build_mcp_server(settings, store=store)
    mcp_app = mcp.http_app(path="/mcp")
    app = FastAPI(lifespan=mcp_app.lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/check", response_model=CheckResponse)
    async def check_endpoint(payload: CheckRequest) -> CheckResponse:
        try:
            project = store.require_project(payload.project_key)
            agent = store.authenticate_agent(project, payload.agent_name, payload.registration_token)
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        store.touch_agent(agent.id)
        counts = store.unread_counts(project, agent)
        return CheckResponse(
            ok=True,
            project_key=project.project_key,
            project_slug=project.project_slug,
            agent_name=agent.agent_name,
            direct_unread=counts["direct_unread"],
            mention_unread=counts["mention_unread"],
            total_unread=counts["total_unread"],
        )

    app.mount("/", mcp_app)
    return app
