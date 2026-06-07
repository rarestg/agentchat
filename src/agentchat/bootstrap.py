from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

from .store import canonicalize_project_key, validate_agent_name


def state_home_root() -> Path:
    raw = os.environ.get("XDG_STATE_HOME", "").strip()
    if raw:
        base = Path(os.path.expanduser(raw))
        if base.is_absolute():
            return base
    return Path.home() / ".local" / "state"


def bootstrap_state_root() -> Path:
    return state_home_root() / "agentchat" / "bootstrap"


def project_bootstrap_key(project_key: str) -> str:
    return hashlib.sha256(project_key.encode("utf-8")).hexdigest()


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath([str(path), str(root)]) == str(root)
    except ValueError:
        return False


def resolve_bootstrap_path(project_key: str, agent_name: str) -> Path:
    canonical_project_key = canonicalize_project_key(project_key)
    name = validate_agent_name(agent_name)
    path = bootstrap_state_root() / project_bootstrap_key(canonical_project_key) / f"{name}.json"
    real_bootstrap = Path(os.path.realpath(path))
    real_project = Path(os.path.realpath(canonical_project_key))
    if _path_is_within(real_bootstrap, real_project):
        raise ValueError("Bootstrap path must not resolve inside the project tree.")
    return path


def default_notify_state_path(bootstrap_path: Path) -> Path:
    return bootstrap_path.with_suffix(".notify.json")


def _validate_managed_subtree(path: Path) -> None:
    base = state_home_root()
    try:
        relative = path.relative_to(base)
    except ValueError as exc:
        raise ValueError("Bootstrap path must live under the configured state root.") from exc

    current = base
    for part in relative.parts[:-1]:
        current = current / part
        if current.exists():
            if current.is_symlink():
                raise ValueError(f"Refusing to write bootstrap through symlinked directory: {current}")
            if not current.is_dir():
                raise ValueError(f"Refusing to use non-directory bootstrap component: {current}")

    if path.exists():
        if path.is_symlink():
            raise ValueError(f"Refusing to overwrite symlinked bootstrap path: {path}")
        if not path.is_file():
            raise ValueError(f"Refusing to overwrite non-file bootstrap path: {path}")


def _chmod_managed_dirs(path: Path) -> None:
    base = state_home_root()
    current = base
    for part in path.relative_to(base).parts[:-1]:
        current = current / part
        if current.is_dir():
            with suppress(OSError):
                os.chmod(current, 0o700)


def write_bootstrap_payload(path: Path, payload: dict[str, Any]) -> Path:
    _validate_managed_subtree(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _chmod_managed_dirs(path)

    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            with suppress(OSError):
                os.fchmod(handle.fileno(), 0o600)
            with suppress(OSError):
                os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        with suppress(OSError):
            temp_path.unlink()
        raise
    return path
