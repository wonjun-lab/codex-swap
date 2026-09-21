"""codex-swap 이 관리하는 Codex 자식의 자격증명 저장 계약."""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
from collections.abc import Sequence
from os import PathLike, fspath
from pathlib import Path

STORE_KEY = "cli_auth_credentials_store"
FILE_STORE_OVERRIDE = f'{STORE_KEY}="file"'
NON_FILE_STORES = frozenset({"auto", "ephemeral", "keyring"})


def refresh_fingerprint(auth_file: Path) -> str | None:
    """Return a one-way refresh-token fingerprint without exposing token text."""
    try:
        data = json.loads(auth_file.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    tokens = data.get("tokens") if isinstance(data, dict) else None
    token = tokens.get("refresh_token") if isinstance(tokens, dict) else None
    if not isinstance(token, str) or not token:
        return None
    return hashlib.sha256(token.encode("utf-8", "surrogatepass")).hexdigest()


def app_refresh_fingerprint() -> str | None:
    """Fingerprint held by the installed ChatGPT app, if readable."""
    from codex_swap.core import wiring

    if not wiring.app_installed():
        return None
    return refresh_fingerprint(wiring.DEFAULT_HOME.expanduser() / "auth.json")


def shares_app_login(home: str | PathLike[str] | None) -> bool:
    """Whether probing ``home`` could refresh the ChatGPT app's current login."""
    from codex_swap.core import wiring

    app_fingerprint = app_refresh_fingerprint()
    if app_fingerprint is None:
        return False
    raw_home = home if home is not None else os.environ.get("CODEX_HOME")
    probe_home = Path(raw_home) if raw_home else wiring.DEFAULT_HOME.expanduser()
    return refresh_fingerprint(probe_home.expanduser() / "auth.json") == app_fingerprint


def configured_store(codex_home: Path) -> str | None:
    """Codex 홈의 최상위 저장소 설정. 없거나 읽을 수 없으면 알 수 없다."""
    try:
        loaded = tomllib.loads((codex_home / "config.toml").read_text())
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return None
    selected = loaded.get(STORE_KEY)
    return selected if isinstance(selected, str) else None


def _assignment(arg: str) -> str | None:
    key, separator, value = arg.partition("=")
    if separator and key.strip() == STORE_KEY:
        return value.strip().strip("\"'")
    return None


def explicit_store(argv: Sequence[str]) -> str | None:
    """CLI ``-c`` 로 마지막에 명시한 저장소. 없으면 ``None``."""
    selected: str | None = None
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            break
        raw: str | None = None
        if arg in ("-c", "--config"):
            if index + 1 < len(argv):
                raw = argv[index + 1]
                index += 1
        elif arg.startswith("--config="):
            raw = arg.removeprefix("--config=")
        elif arg.startswith("-c") and len(arg) > 2:
            raw = arg[2:]
        if raw is not None and (value := _assignment(raw)) is not None:
            selected = value
        index += 1
    return selected


def uses_file_store(argv: Sequence[str]) -> bool:
    """관리 기본값 또는 사용자가 명시한 ``file`` 인가."""
    selected = explicit_store(argv)
    return selected is None or selected == "file"


def managed_argv(binary: str | PathLike[str], argv: Sequence[str]) -> tuple[str, ...]:
    """명시 선택은 보존하고, 선택이 없을 때만 file backend 를 고정한다."""
    command = (fspath(binary), *argv)
    if explicit_store(argv) is not None:
        return command
    return (fspath(binary), "-c", FILE_STORE_OVERRIDE, *argv)
