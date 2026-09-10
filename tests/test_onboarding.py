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
from codex_swap.core import wiring


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
    text = wiring.snippet("bash", isolate=wiring.app_installed())
    assert "CODEX_HOME" not in text, text
    assert "codex-swap rotate" in text, text


def test_the_wiring_still_carries_automatic_switching(_isolated_home: Path) -> None:
    """분리를 안 해도 배선은 필요하다 — 자동 전환이 거기 걸려 있다."""
    assert "codex-swap rotate" in wiring.snippet("bash", isolate=False)


# ── 앱과 같이 쓰는 사람 ────────────────────────────────────────────────────


def test_with_the_app_we_step_aside(_isolated_home: Path) -> None:
    """`~/.codex` 의 주인은 공식 앱이다. **비켜 주는 쪽은 우리다.**"""
    text = wiring.snippet("bash", isolate=True)
    assert wiring.ISOLATED_HOME in text
    assert "CODEX_ACCOUNT_DEFAULT_HOME" in text


def test_the_isolated_home_is_not_exported_into_the_whole_shell(_isolated_home: Path) -> None:
    """`CODEX_HOME` 이 셸에 남으면 사용자가 여는 다른 도구까지 따라온다 — 앱이 띄운 것도."""
    text = wiring.snippet("bash", isolate=True)
    assert "export CODEX_HOME" not in text, text
    assert "CODEX_HOME=" in text, "codex 를 부르는 그 한 번에는 걸려야 한다"


def test_init_explains_the_move_instead_of_doing_it_silently(
    _isolated_home: Path, has_codex, monkeypatch, capsys
) -> None:
    _app(monkeypatch, True)
    _unwired(monkeypatch)
    cli.main(["init"])
    out = capsys.readouterr().out
    assert "ChatGPT desktop app" in out, out
    assert wiring.ISOLATED_HOME in out, out


# ── 앱을 나중에 까는 사람 ──────────────────────────────────────────────────


def test_the_wiring_re_decides_every_shell(_isolated_home: Path) -> None:
    """**한 줄만 넣게 하는 이유다.**

    배선을 통째로 붙여 넣게 하면 그 사본이 낡는다. 앱을 나중에 깔아도 프로필의 옛 판은
    분리하지 않은 채로 남고, 우리는 그것을 고치라고 알릴 방법이 없다.
    """
    assert "shell-init" in wiring.line_for("bash")
    assert "shell-init" in wiring.line_for("fish")


def test_moving_to_an_isolated_home_leaves_the_login_behind(
    _isolated_home: Path, tmp_path: Path
) -> None:
    """분리로 넘어가는 순간에만 생기는 상태 — 잘 쓰던 로그인이 사라진 것처럼 보인다."""
    original = tmp_path / ".codex"
    _auth(original / "auth.json", "a@example.com")
    fresh = tmp_path / ".codex-cli"
    assert wiring.seed_source.__doc__  # 의도가 적혀 있어야 한다

    import codex_swap.core.wiring as w

    old = w.DEFAULT_HOME
    try:
        w.DEFAULT_HOME = original
        assert w.seed_source(fresh) == original
        # 옮기고 나면 더는 말하지 않는다.
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
    profile = tmp_path / ".bashrc"
    profile.write_text("codex() {\n  command codex-swap rotate >/dev/null || true\n}\n")
    monkeypatch.setattr(wiring, "profile_for", lambda _: str(profile))
    assert wiring.already_wired("bash") == profile


def test_a_missing_profile_is_not_an_error(_isolated_home: Path, monkeypatch) -> None:
    monkeypatch.setattr(wiring, "profile_for", lambda _: "/nonexistent/profile")
    monkeypatch.setattr(Path, "home", lambda: Path("/nonexistent"))
    assert wiring.already_wired("bash") is None


# ── 배선 자체가 셸에서 성립하는가 ──────────────────────────────────────────


@pytest.mark.parametrize("isolate", [True, False])
def test_the_posix_snippet_parses(isolate: bool) -> None:
    """**이 출력은 사용자의 셸이 그대로 실행한다.** 문법이 깨지면 프로필이 그 자리에서 깨진다."""
    import subprocess

    text = wiring.snippet("bash", isolate=isolate)
    assert subprocess.run(["bash", "-n"], input=text, text=True).returncode == 0


def test_shell_init_prints_nothing_but_wiring(_isolated_home: Path, capsys) -> None:
    """안내 한 줄이라도 섞이면 `eval` 이 그것을 명령으로 읽는다."""
    cli.main(["shell-init", "--shell", "bash", "--no-isolate"])
    out = capsys.readouterr().out
    assert out.startswith("codex()"), out
    assert "ok" not in out and "TODO" not in out, out
