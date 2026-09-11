"""설치부터 첫 전환까지 — **조합마다 막히지 않는가.**

사용자는 한 가지 순서로 오지 않는다. codex 만 쓰던 사람, 공식 앱과 같이 쓰는 사람,
codex 보다 codex-swap 을 먼저 깐 사람이 있고, 앱을 **나중에** 까는 사람도 있다. 각 조합이
따로 성립하는 것으로는 부족하다 — 한 조합에서 다른 조합으로 넘어가는 순간이 가장 잘
깨지고, 그때 사용자에게는 잘 쓰던 도구가 갑자기 고장난 것으로 보인다.

여기서 재는 것은 "동작하는가" 가 아니라 **"막혔을 때 다음 한 걸음을 말해 주는가"** 다.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from codex_swap import cli
from codex_swap.core import config, wiring


def _auth(path: Path, email: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    claims = base64.urlsafe_b64encode(json.dumps({"email": email}).encode())
    path.write_text(json.dumps({"tokens": {"id_token": f"h.{claims.decode().rstrip('=')}.s"}}))


@pytest.fixture
def no_codex(monkeypatch: pytest.MonkeyPatch):
    def missing():
        raise RuntimeError("not found")

    monkeypatch.setattr(cli.discovery, "resolve_codex_bin", missing)


@pytest.fixture
def has_codex(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cli.discovery, "resolve_codex_bin", lambda: Path("/bin/true"))


def _app(monkeypatch: pytest.MonkeyPatch, installed: bool) -> None:
    monkeypatch.setattr(wiring, "app_installed", lambda *_, **__: installed)


def _unwired(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wiring, "already_wired", lambda *_: None)


def _no_codex_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """PATH 에 이 기기의 진짜 wrapper 가 있으면 테스트가 그것을 잡는다."""
    monkeypatch.setattr(wiring.shutil, "which", lambda _: None)


# ── codex-swap 을 먼저 깐 사람 ─────────────────────────────────────────────


def test_installing_before_codex_stops_and_says_why(_isolated_home: Path, no_codex, capsys) -> None:
    """**여기서 끊는다.** codex 가 없으면 그 뒤 단계는 전부 의미가 없다."""
    assert cli.main(["init"]) == 1
    out = capsys.readouterr().out
    assert "codex was not found" in out, out
    assert "npm install -g @openai/codex" in out, out


def test_the_other_checks_do_not_run_without_codex(_isolated_home: Path, no_codex, capsys) -> None:
    """할 수 없는 일을 줄줄이 늘어놓으면 무엇부터 해야 하는지가 묻힌다."""
    cli.main(["init"])
    out = capsys.readouterr().out
    assert "accounts" not in out.lower(), out


# ── codex 만 쓰는 사람 (앱 없음) ───────────────────────────────────────────


def test_without_the_app_nothing_is_moved(_isolated_home: Path, monkeypatch) -> None:
    """앱이 없으면 다툴 상대도 없다. 공연히 홈을 옮기면 그 자체가 고장이다."""
    _app(monkeypatch, False)
    for var in ("CODEX_ACCOUNT_DEFAULT_HOME", "CODEX_ACCOUNTS_DIR"):
        monkeypatch.delenv(var, raising=False)
    s = config.load()
    assert s.default_home.name == ".codex", s.default_home
    assert s.accounts_dir == s.default_home / "accounts"


# ── 앱과 같이 쓰는 사람 ────────────────────────────────────────────────────


def test_with_the_app_we_step_aside(_isolated_home: Path, monkeypatch) -> None:
    """`~/.codex` 의 주인은 공식 앱이다. **비켜 주는 쪽은 우리다.**"""
    _app(monkeypatch, True)
    for var in ("CODEX_ACCOUNT_DEFAULT_HOME", "CODEX_ACCOUNTS_DIR"):
        monkeypatch.delenv(var, raising=False)
    s = config.load()
    assert s.default_home.name == ".codex-cli", s.default_home


def test_the_accounts_do_not_follow_the_home(_isolated_home: Path, monkeypatch) -> None:
    """**계정까지 옮기면 등록해 둔 것을 통째로 잃는다.**

    앱이 건드리는 것은 `auth.json` 하나이고 `accounts/` 는 앱이 모른다. 활성 자리만
    비켜 주면 되는데 홈을 따라 옮기면, 앱을 깐 날 계정이 전부 사라진 것처럼 보인다.
    """
    _app(monkeypatch, True)
    for var in ("CODEX_ACCOUNT_DEFAULT_HOME", "CODEX_ACCOUNTS_DIR"):
        monkeypatch.delenv(var, raising=False)
    s = config.load()
    assert s.accounts_dir.parent.name == ".codex", s.accounts_dir
    assert s.accounts_dir != s.default_home / "accounts"


def test_an_explicit_home_still_wins(_isolated_home: Path, monkeypatch, tmp_path) -> None:
    """사람이 정한 것이 이긴다. 그때는 계정도 그 홈을 따르던 기존 규칙 그대로다."""
    _app(monkeypatch, True)
    monkeypatch.setenv("CODEX_ACCOUNT_DEFAULT_HOME", str(tmp_path / "mine"))
    monkeypatch.delenv("CODEX_ACCOUNTS_DIR", raising=False)
    s = config.load()
    assert s.default_home == tmp_path / "mine"
    assert s.accounts_dir == tmp_path / "mine" / "accounts"


def test_init_says_it_moved_instead_of_doing_it_silently(
    _isolated_home: Path, has_codex, monkeypatch, capsys
) -> None:
    _app(monkeypatch, True)
    _unwired(monkeypatch)
    cli.main(["init"])
    out = capsys.readouterr().out
    assert "ChatGPT desktop app" in out, out


# ── 앱을 나중에 까는 사람 ──────────────────────────────────────────────────


def test_the_wrapper_stays_thin_so_it_never_goes_stale(_isolated_home: Path) -> None:
    """**한 줄짜리 위임이라 기기에 놓인 파일이 낡지 않는다.**

    여기에 판단을 넣으면 그 사본이 사용자 기기에서 굳는다 — 앱을 나중에 깔아도 옛 파일은
    분리를 모르고, 우리는 그것을 고치라고 알릴 방법이 없다. 판단은 전부 `exec` 안에 있다.
    """
    body = wiring.WRAPPER_BODY
    assert "codex-swap exec" in body
    assert "CODEX_HOME" not in body, "판단이 파일로 새어 나왔다"
    assert len(body.strip().splitlines()) <= 3, body


def test_the_wrapper_is_valid_shell() -> None:
    """**이 파일은 사용자가 codex 를 칠 때마다 실행된다.** 문법이 깨지면 codex 가 안 뜬다."""
    import subprocess

    assert subprocess.run(["sh", "-n"], input=wiring.WRAPPER_BODY, text=True).returncode == 0


def test_moving_to_an_isolated_home_leaves_the_login_behind(
    _isolated_home: Path, tmp_path: Path
) -> None:
    """분리로 넘어가는 순간에만 생기는 상태 — 잘 쓰던 로그인이 사라진 것처럼 보인다."""
    original = tmp_path / ".codex"
    _auth(original / "auth.json", "a@example.com")
    fresh = tmp_path / ".codex-cli"

    import codex_swap.core.wiring as w

    old = w.DEFAULT_HOME
    try:
        w.DEFAULT_HOME = original
        assert w.seed_source(fresh) == original
        _auth(fresh / "auth.json", "a@example.com")
        assert w.seed_source(fresh) is None
    finally:
        w.DEFAULT_HOME = old


def test_an_unseparated_install_is_never_told_to_move(_isolated_home: Path, tmp_path) -> None:
    """같은 자리를 가리키는데 "옮겨라" 라고 하면 사용자는 무한히 같은 일을 한다."""
    import codex_swap.core.wiring as w

    old = w.DEFAULT_HOME
    try:
        w.DEFAULT_HOME = tmp_path / ".codex"
        (w.DEFAULT_HOME / "auth.json").parent.mkdir(parents=True)
        _auth(w.DEFAULT_HOME / "auth.json", "a@example.com")
        assert w.seed_source(w.DEFAULT_HOME) is None
    finally:
        w.DEFAULT_HOME = old


# ── 이미 손으로 배선해 둔 사람 ─────────────────────────────────────────────


def test_hand_written_wiring_counts(_isolated_home: Path, tmp_path, monkeypatch) -> None:
    """우리가 시킨 적 없는 것을 "빠졌다" 고 하면 자기가 뭘 잘못했나 찾게 된다."""
    _no_codex_on_path(monkeypatch)
    profile = tmp_path / ".bashrc"
    profile.write_text("codex() {\n  command codex-swap rotate >/dev/null || true\n}\n")
    monkeypatch.setattr(wiring, "profile_for", lambda _: str(profile))
    assert wiring.already_wired("bash") == profile


def test_a_missing_profile_is_not_an_error(_isolated_home: Path, monkeypatch) -> None:
    _no_codex_on_path(monkeypatch)
    monkeypatch.setattr(wiring, "profile_for", lambda _: "/nonexistent/profile")
    monkeypatch.setattr(Path, "home", lambda: Path("/nonexistent"))
    assert wiring.already_wired("bash") is None


# ── 배선 자체가 셸에서 성립하는가 ──────────────────────────────────────────


def test_we_never_overwrite_someone_elses_wrapper(_isolated_home: Path, tmp_path) -> None:
    """dotfiles 로 자기 wrapper 를 심어 둔 사람이 있다. 그것도 제 몫을 한다."""
    theirs = tmp_path / "codex"
    theirs.write_text('#!/bin/sh\nexec /somewhere/codex "$@"\n')
    assert wiring.occupied_by_other(theirs)
    with pytest.raises(FileExistsError):
        wiring.install_wrapper(theirs)
    assert "somewhere" in theirs.read_text(), "남의 파일을 덮었다"


def test_our_own_wrapper_is_replaced_not_duplicated(_isolated_home: Path, tmp_path) -> None:
    """`init` 은 여러 번 부르는 명령이다. 우리 것은 그대로 다시 써도 된다."""
    target = tmp_path / "codex"
    wiring.install_wrapper(target)
    assert wiring.wrapper_here(target)
    wiring.install_wrapper(target)
    assert target.read_text() == wiring.WRAPPER_BODY


def test_a_wrapper_that_calls_us_counts_as_wired(
    _isolated_home: Path, tmp_path, monkeypatch
) -> None:
    """**이 오탐이 실제로 났다.**

    프로필만 뒤지면 PATH 에 놓인 wrapper 를 못 본다. 멀쩡히 돌아가는 기기에 "배선이
    빠졌다" 고 말하게 되고, 사용자는 이미 있는 것을 또 넣게 된다.
    """
    theirs = tmp_path / "codex"
    theirs.write_text('#!/usr/bin/env bash\ncodex-swap rotate || true\nexec real-codex "$@"\n')
    theirs.chmod(0o755)
    monkeypatch.setattr(wiring.shutil, "which", lambda _: str(theirs))
    assert wiring.already_wired("bash") == theirs
