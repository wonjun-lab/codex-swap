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


def _write_auth(path: Path, email: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    path.write_text(json.dumps({"tokens": {"id_token": f"h.{payload}.s"}}))
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

    rc = cli.cmd_add(
        env,
        "second",
        runner=capture_runner(calls, writes=lambda: _write_auth(slot_auth, "b@example.com")),
    )

    assert rc == 0
    argv, run_env = calls[0]
    assert argv[1] == "login"
    # 자격증명이 슬롯에 떨어져야 한다. 이게 틀리면 전역 홈을 덮어써 활성 계정을 잃는다.
    assert run_env["CODEX_HOME"] == str(store.slot_dir(env, "second"))
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


def test_add_does_not_touch_the_active_account(env) -> None:
    _write_auth(store.active_auth(env), "live@example.com")
    before = store.active_auth(env).read_bytes()
    calls: list[tuple[list[str], dict[str, str]]] = []
    cli.cmd_add(
        env,
        "second",
        runner=capture_runner(
            calls, writes=lambda: _write_auth(store.slot_auth(env, "second"), "b@example.com")
        ),
    )
    assert store.active_auth(env).read_bytes() == before
