"""Probe the gateway user's exact Keeper read and write paths."""
from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
from contextlib import closing
from pathlib import Path


def _private_write(path: Path) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise PermissionError("group or world write is enabled")


def _read(path: Path, *, secret: bool = False) -> None:
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError("required regular file is missing")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise PermissionError("config file is group or world writable")
    if secret and mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IXGRP | stat.S_IXOTH):
        raise PermissionError("secret is accessible to group or world")
    with path.open("rb") as stream:
        stream.read(1)


def _write_dir(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise FileNotFoundError("required directory is missing")
    _private_write(path)
    with tempfile.TemporaryDirectory(prefix=".keeper-health-", dir=path) as directory:
        probe = Path(directory) / "probe"
        with probe.open("xb") as stream:
            stream.write(b"ok")


def _soul_write(path: Path) -> None:
    _read(path)
    _private_write(path.parent)
    # r+b requires permission to write this exact file, without changing it.
    with path.open("r+b"):
        pass


def check_gateway_access(
    hermes_home: Path, data_dir: Path, *,
    secrets_dir: Path, soul_files: tuple[Path, ...],
) -> dict[str, str]:
    """Return PASS/FAIL detail; execute as the actual gateway UID."""
    results: dict[str, str] = {}

    def check(name: str, action) -> None:
        try:
            action()
        except (OSError, sqlite3.Error, ValueError) as exc:
            results[name] = f"FAIL: {type(exc).__name__}"
        else:
            results[name] = "PASS"

    hermes_home = Path(hermes_home)
    data_dir = Path(data_dir)
    secrets_dir = Path(secrets_dir)
    def config_read() -> None:
        path = hermes_home / "config.yaml"
        _read(path)
        content = path.read_text(encoding="utf-8")
        if ("skills teach" not in content or "skills keep" not in content
                or "keeper apps telegram" not in content):
            raise ValueError("required Keeper quick command is not configured")

    check("gateway config and quick commands", config_read)
    check("gateway environment", lambda: _read(hermes_home / ".env", secret=True))

    def state_read() -> None:
        path = Path(os.environ.get("KEEPER_HERMES_STATE_DB", hermes_home / "runtime" / "state.db"))
        _read(path, secret=True)
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
            conn.execute("SELECT 1").fetchone()

    check("session log read for /teach", state_read)

    def apps_read() -> None:
        path = data_dir / "keeper.db"
        _read(path, secret=True)
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            conn.execute("SELECT 1 FROM applications LIMIT 1").fetchone()

    check("Keeper database read for /apps", apps_read)
    check("Keeper data write for /teach", lambda: _write_dir(data_dir))
    drafts = data_dir / "skill-drafts"
    if drafts.exists():
        check("draft write for /teach", lambda: _write_dir(drafts))
    else:
        # /teach creates this directory; parent write probe above establishes access.
        results["draft write for /teach"] = "PASS (directory will be created)" if results[
            "Keeper data write for /teach"] == "PASS" else "FAIL: parent inaccessible"
    check("learned skill write for /keep", lambda: _write_dir(hermes_home / "skills" / "keeper"))
    for index, path in enumerate(soul_files, 1):
        check(f"SOUL write for /keep #{index}", lambda p=path: _soul_write(Path(p)))

    def secrets() -> None:
        if secrets_dir.is_symlink() or not secrets_dir.is_dir():
            raise FileNotFoundError("secrets directory missing")
        mode = stat.S_IMODE(secrets_dir.stat().st_mode)
        if mode & 0o077:
            raise PermissionError("secrets directory must be mode 700 or stricter")
        for path in secrets_dir.iterdir():
            if path.is_file():
                _read(path, secret=True)

    check("secrets least privilege", secrets)
    return results
