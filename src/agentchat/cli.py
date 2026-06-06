from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
import typer
import uvicorn

from .server import Settings, create_app

app = typer.Typer(add_completion=False)


def load_bootstrap(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


async def fetch_check(payload: dict[str, Any], timeout_seconds: float = 3.0) -> dict[str, Any]:
    server_url = str(payload["server_url"]).rstrip("/")
    async with httpx.AsyncClient(base_url=server_url, timeout=timeout_seconds) as client:
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


def format_notify_message(check_result: dict[str, Any]) -> str:
    lines = [
        "=== AGENTCHAT ===",
        f"{check_result['direct_unread']} unread direct messages",
        f"{check_result['mention_unread']} new mentions in project feed",
        "Call agentchat_fetch_inbox() or agentchat_fetch_project_feed()",
        "=================",
    ]
    return "\n".join(lines)


def state_fingerprint(check_result: dict[str, Any]) -> str:
    digest_input = json.dumps(
        {
            "direct_unread": check_result["direct_unread"],
            "mention_unread": check_result["mention_unread"],
            "total_unread": check_result["total_unread"],
        },
        sort_keys=True,
    )
    return hashlib.sha256(digest_input.encode("utf-8")).hexdigest()


def should_emit_notification(
    state_file: Path | None,
    fingerprint: str,
    min_interval_seconds: int,
) -> bool:
    if state_file is None:
        return True
    state_file.parent.mkdir(parents=True, exist_ok=True)
    now = int(time.monotonic())
    if state_file.exists():
        try:
            previous = json.loads(state_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            previous = {}
        if previous.get("fingerprint") == fingerprint and (now - int(previous.get("at", 0))) < min_interval_seconds:
            return False
    state_file.write_text(json.dumps({"fingerprint": fingerprint, "at": now}), encoding="utf-8")
    return True


@app.command()
def serve(
    host: str = "127.0.0.1",
    port: int = 8787,
    db_path: Path = Path("./data/agentchat.db"),
    stale_after_seconds: int = 1800,
) -> None:
    settings = Settings(db_path=db_path.resolve(), host=host, port=port, stale_after_seconds=stale_after_seconds)
    uvicorn.run(create_app(settings), host=host, port=port)


@app.command()
def health(url: str = "http://127.0.0.1:8787") -> None:
    async def run() -> None:
        async with httpx.AsyncClient(base_url=url.rstrip("/")) as client:
            response = await client.get("/healthz")
            response.raise_for_status()
            typer.echo(json.dumps(response.json(), indent=2, sort_keys=True))

    asyncio.run(run())


@app.command()
def check(
    bootstrap: Path,
    as_json: bool = typer.Option(False, "--json"),
    notify: bool = False,
    state_file: Path | None = None,
    min_interval_seconds: int = 120,
) -> None:
    payload = load_bootstrap(bootstrap)

    async def run() -> dict[str, Any]:
        return await fetch_check(payload)

    result = asyncio.run(run())
    if as_json:
        typer.echo(json.dumps(result, indent=2, sort_keys=True))
        return
    if notify:
        if result["total_unread"] <= 0:
            return
        fingerprint = state_fingerprint(result)
        if should_emit_notification(state_file, fingerprint, min_interval_seconds):
            typer.echo(format_notify_message(result))
        return
    typer.echo(format_notify_message(result) if result["total_unread"] else "No unread messages.")


@app.command()
def bootstrap_path(project_key: str, agent_name: str) -> None:
    from .server import bootstrap_path as compute_bootstrap_path
    from .store import canonicalize_project_key, project_slug

    canonical = canonicalize_project_key(project_key)
    project_dir = Path(canonical)
    path = compute_bootstrap_path(
        type("ProjectStub", (), {"project_key": str(project_dir), "project_slug": project_slug(canonical)})(),
        agent_name,
    )
    typer.echo(str(path))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
