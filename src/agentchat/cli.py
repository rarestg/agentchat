from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx
import typer
import uvicorn

from .bootstrap import default_notify_state_path, resolve_bootstrap_path
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


def load_notify_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def write_notify_state(path: Path, fingerprint: str, at: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fingerprint": fingerprint, "at": at}), encoding="utf-8")
    with suppress(OSError):
        os.chmod(path, 0o600)


def resolve_notify_state_file(bootstrap: Path, state_file: Path | None) -> Path:
    return state_file or default_notify_state_path(bootstrap)


def should_emit_notification(
    state_file: Path,
    fingerprint: str,
    min_interval_seconds: int,
    now: int,
) -> bool:
    previous = load_notify_state(state_file)
    try:
        previous_at = int(previous.get("at", 0))
    except (TypeError, ValueError):
        previous_at = 0
    if previous.get("fingerprint") == fingerprint and (now - previous_at) < min_interval_seconds:
        return False
    write_notify_state(state_file, fingerprint, now)
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
        notify_state_file = resolve_notify_state_file(bootstrap, state_file)
        fingerprint = state_fingerprint(result)
        now = int(time.time())
        if result["total_unread"] <= 0:
            # Persist the cleared fingerprint too, otherwise a 1 -> 0 -> 1
            # cycle inside the rate-limit window gets suppressed incorrectly.
            write_notify_state(notify_state_file, fingerprint, now)
            return
        if should_emit_notification(notify_state_file, fingerprint, min_interval_seconds, now):
            typer.echo(format_notify_message(result))
        return
    typer.echo(format_notify_message(result) if result["total_unread"] else "No unread messages.")


@app.command()
def bootstrap_path(project_key: str, agent_name: str) -> None:
    path = resolve_bootstrap_path(project_key, agent_name)
    typer.echo(str(path))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
