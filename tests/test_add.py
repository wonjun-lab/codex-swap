"""`add` 명령 테스트.

실제 로그인은 브라우저를 끼므로 사람 없이 재현할 수 없다. 대신 **무엇을 어떤 환경으로
부르는지**를 고정한다 — 그 조립이 틀리면 자격증명이 엉뚱한 홈에 떨어지거나 wrapper
재귀가 생기고, 둘 다 조용히 일어난다.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

from codex_swap import cli
from codex_swap.core import config, store


def _write_auth(path: Path, email: str, *, refresh: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    tokens = {"id_token": f"h.{payload}.s"}
    if refresh is not None:
        tokens["refresh_token"] = refresh
    path.write_text(json.dumps({"tokens": tokens}))
    os.chmod(path, 0o600)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    (home / ".codex/accounts").mkdir(parents=True)
    (home / ".claude").mkdir(parents=True)
    for k in ("CODEX_ROTATE_SKIP", "CODEX_HOME"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_ACCOUNT_DEFAULT_HOME", str(home / ".codex"))
    monkeypatch.setenv("CODEX_ACCOUNTS_DIR", str(home / ".codex/accounts"))
    monkeypatch.setenv("CODEX_ACCOUNT_BIN", str(tmp_path / "codex"))
    (tmp_path / "codex").write_text("#!/bin/sh\nexit 0\n")
    os.chmod(tmp_path / "codex", 0o755)
    return config.load()


def capture_runner(calls: list[tuple[list[str], dict[str, str]]], *, rc: int = 0, writes=None):
    def run(argv: list[str], env: dict[str, str]) -> int:
        calls.append((argv, env))
        if writes is not None:
            writes()
        return rc

    return run


def test_add_invokes_codex_login_with_the_slot_as_home(env, capsys) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []
    slot_auth = store.slot_auth(env, "second")

    def write_staged_auth() -> None:
        login_home = Path(calls[-1][1]["CODEX_HOME"])
        _write_auth(login_home / "auth.json", "b@example.com")

    rc = cli.cmd_add(
        env,
        "second",
        runner=capture_runner(calls, writes=write_staged_auth),
    )

    assert rc == 0
    argv, run_env = calls[0]
    assert argv == [
        str(Path(os.environ["CODEX_ACCOUNT_BIN"])),
        "-c",
        'cli_auth_credentials_store="file"',
        "login",
    ]
    # 기존 슬롯에 바로 로그인하면 실패한 --force 가 마지막 정상 자격증명까지 덮는다.
    # 같은 0700 루트의 staging home 에 로그인한 뒤 성공한 파일만 설치해야 한다.
    login_home = Path(run_env["CODEX_HOME"])
    assert login_home.parent == env.accounts_dir
    assert login_home != store.slot_dir(env, "second")
    assert not login_home.exists(), "성공한 로그인 staging home 이 남았다"
    # 이게 없으면 로그인이 띄우는 codex 가 wrapper 를 거쳐 다시 rotate 를 부른다.
    assert run_env["CODEX_ROTATE_SKIP"] == "1"
    assert oct(os.stat(slot_auth).st_mode & 0o777) == "0o600"
    assert "adopted second" in capsys.readouterr().out


def test_add_refuses_an_existing_label(env, capsys) -> None:
    _write_auth(store.slot_auth(env, "taken"), "a@example.com")
    assert cli.main(["add", "taken"]) == 1
    assert "label already exists" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["../evil", "a/b", ".hidden", ".."])
def test_add_refuses_a_bad_label(env, capsys, bad: str) -> None:
    assert cli.main(["add", bad]) == 1
    assert "not a usable label" in capsys.readouterr().err


def test_add_reports_a_failed_login(env, capsys) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []
    with pytest.raises(cli.CliError, match="login failed"):
        cli.cmd_add(env, "second", runner=capture_runner(calls, rc=1))


def test_add_notices_a_login_that_left_no_credentials(env) -> None:
    """`codex login` 이 0 으로 끝나도 auth.json 이 없으면 실패다.

    로그인이 중간에 끊기면 실제로 이 상태가 된다 — 성공을 보고했는데 슬롯은 비어 있다.
    """
    calls: list[tuple[list[str], dict[str, str]]] = []
    with pytest.raises(cli.CliError, match=r"auth\.json"):
        cli.cmd_add(env, "second", runner=capture_runner(calls, rc=0))


def test_add_force_does_not_accept_the_old_auth_as_a_new_login(env) -> None:
    """keyring 로그인은 0 이어도 auth.json 을 새로 쓰지 않는다.

    기존 슬롯을 로그인 홈으로 그대로 쓰면 오래된 auth.json 이 이미 있어서 새 로그인이
    파일을 남긴 것처럼 보인다. 성공은 빈 staging home 에 새 파일이 생긴 것으로만 증명한다.
    """
    old = store.slot_auth(env, "second")
    _write_auth(old, "old@example.com")
    before = old.read_bytes()
    calls: list[tuple[list[str], dict[str, str]]] = []

    with pytest.raises(cli.CliError, match=r"no auth\.json"):
        cli.cmd_add(env, "second", force=True, runner=capture_runner(calls, rc=0))

    assert old.read_bytes() == before


def test_add_force_keeps_old_auth_when_login_fails_after_writing(env) -> None:
    old = store.slot_auth(env, "second")
    _write_auth(old, "old@example.com")
    before = old.read_bytes()
    calls: list[tuple[list[str], dict[str, str]]] = []

    def failed_login(argv: list[str], run_env: dict[str, str]) -> int:
        calls.append((argv, run_env))
        _write_auth(Path(run_env["CODEX_HOME"]) / "auth.json", "unfinished@example.com")
        return 1

    with pytest.raises(cli.CliError, match="login failed"):
        cli.cmd_add(env, "second", force=True, runner=failed_login)

    assert old.read_bytes() == before


def test_add_does_not_touch_the_active_account(env) -> None:
    _write_auth(store.active_auth(env), "live@example.com")
    before = store.active_auth(env).read_bytes()
    calls: list[tuple[list[str], dict[str, str]]] = []

    def write_staged_auth() -> None:
        _write_auth(Path(calls[-1][1]["CODEX_HOME"]) / "auth.json", "b@example.com")

    cli.cmd_add(env, "second", runner=capture_runner(calls, writes=write_staged_auth))
    assert store.active_auth(env).read_bytes() == before


def test_add_force_replaces_live_copy_when_reauthenticating_active_slot(env) -> None:
    """A later switch-away must not sync the old live token over the new login."""
    active = store.active_auth(env)
    first = store.slot_auth(env, "first")
    second = store.slot_auth(env, "second")
    _write_auth(active, "same@example.com", refresh="old")
    _write_auth(first, "same@example.com", refresh="old")
    _write_auth(second, "other@example.com", refresh="other")

    def successful_login(_argv: list[str], run_env: dict[str, str]) -> int:
        _write_auth(
            Path(run_env["CODEX_HOME"]) / "auth.json",
            "same@example.com",
            refresh="new",
        )
        return 0

    cli.cmd_add(env, "first", force=True, runner=successful_login)
    with store.switch_lock(env):
        store.switch(env, "second")

    assert json.loads(first.read_text())["tokens"]["refresh_token"] == "new"


def test_add_force_cannot_replace_the_active_slot_with_a_different_identity(env) -> None:
    active = store.active_auth(env)
    first = store.slot_auth(env, "first")
    _write_auth(active, "old@example.com", refresh="old-live")
    _write_auth(first, "old@example.com", refresh="old-slot")

    def different_login(_argv: list[str], run_env: dict[str, str]) -> int:
        _write_auth(Path(run_env["CODEX_HOME"]) / "auth.json", "new@example.com")
        return 0

    with pytest.raises(cli.CliError, match=r"active slot.*same account"):
        cli.cmd_add(env, "first", force=True, runner=different_login)

    assert json.loads(active.read_text())["tokens"]["refresh_token"] == "old-live"
    assert json.loads(first.read_text())["tokens"]["refresh_token"] == "old-slot"


def test_add_without_force_does_not_overwrite_a_slot_created_during_login(env) -> None:
    concurrent = store.slot_auth(env, "second")

    def concurrent_login(_argv: list[str], run_env: dict[str, str]) -> int:
        _write_auth(Path(run_env["CODEX_HOME"]) / "auth.json", "new@example.com")
        _write_auth(concurrent, "winner@example.com")
        return 0

    with pytest.raises(cli.CliError, match="label already exists"):
        cli.cmd_add(env, "second", runner=concurrent_login)

    assert cli.identity.email_of(concurrent) == "winner@example.com"


def test_add_active_reauth_live_commit_survives_slot_write_failure(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = store.active_auth(env)
    first = store.slot_auth(env, "first")
    second = store.slot_auth(env, "second")
    _write_auth(active, "same@example.com", refresh="old")
    _write_auth(first, "same@example.com", refresh="old")
    _write_auth(second, "other@example.com", refresh="other")

    def successful_login(_argv: list[str], run_env: dict[str, str]) -> int:
        _write_auth(
            Path(run_env["CODEX_HOME"]) / "auth.json",
            "same@example.com",
            refresh="new",
        )
        return 0

    original = store._install
    calls = 0

    def fail_slot(src: Path, dst: Path, *, keep_mtime: bool) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PermissionError("slot read-only")
        original(src, dst, keep_mtime=keep_mtime)

    monkeypatch.setattr(store, "_install", fail_slot)
    with pytest.raises(PermissionError, match="slot read-only"):
        cli.cmd_add(env, "first", force=True, runner=successful_login)
    monkeypatch.setattr(store, "_install", original)

    assert json.loads(active.read_text())["tokens"]["refresh_token"] == "new"
    with store.switch_lock(env):
        store.switch(env, "second")
    assert json.loads(first.read_text())["tokens"]["refresh_token"] == "new"


def test_add_rejects_a_malformed_staged_identity(env) -> None:
    old = store.slot_auth(env, "second")
    _write_auth(old, "old@example.com")
    before = old.read_bytes()

    def malformed(_argv: list[str], run_env: dict[str, str]) -> int:
        staged = Path(run_env["CODEX_HOME"]) / "auth.json"
        staged.write_text("{}")
        return 0

    with pytest.raises(cli.CliError, match="usable identity"):
        cli.cmd_add(env, "second", force=True, runner=malformed)

    assert old.read_bytes() == before


def test_add_refuses_a_symlink_slot_before_login(env, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    store.slot_dir(env, "second").symlink_to(outside, target_is_directory=True)
    calls: list[tuple[list[str], dict[str, str]]] = []

    with pytest.raises(cli.CliError, match="not a usable label"):
        cli.cmd_add(env, "second", force=True, runner=capture_runner(calls))

    assert calls == []
    assert list(outside.iterdir()) == []
