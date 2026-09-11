"""공식 앱과 **한 기기에서 같이 사는가.**

한 기기에서 실제로 본 것들이 이 파일의 출발점이다.

- 앱의 codex 가 `CODEX_HOME=~/.codex` 로 떠서 `auth.json` 을 제자리에서 갱신하고, 자기 세션으로
  되돌려 놓았다. 원장에는 `A -> B` 만 쌓이고 `B -> A` 는 없었다.
- 같은 refresh token 을 두 곳이 쥐어, 앱의 codex 로그에 `401 Encountered invalidated oauth
  token` 이 수만 줄 쌓였다.
- dotfiles wrapper 는 전환을 하긴 했지만 codex 를 앱의 홈으로 띄웠고, 다른 기기의 wrapper 는
  아예 옛 bash 전환기를 부르고 있었다.

여기서 재는 것은 그 각각이 **다시 조용히 일어나지 않는가**다. 실제 codex 도, 실제 토큰도 쓰지
않는다.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

from codex_swap import cli
from codex_swap.core import config, discovery, doctor, selfupdate, wiring


def _auth(path: Path, email: str, refresh: str = "rt-default") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    claims = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    path.write_text(json.dumps({"tokens": {"id_token": f"h.{claims}.s", "refresh_token": refresh}}))


@pytest.fixture
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """임시 HOME 하나. 앱 설치 여부는 테스트가 정한다."""
    home = tmp_path / "home"
    (home / ".local/bin").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{home / '.local/bin'}{os.pathsep}{os.environ['PATH']}")
    for var in ("CODEX_ACCOUNT_DEFAULT_HOME", "CODEX_ACCOUNTS_DIR", "CODEX_HOME"):
        monkeypatch.delenv(var, raising=False)
    app = home / "Applications/ChatGPT.app"
    monkeypatch.setattr(wiring, "APP_PATHS", (str(app),))
    monkeypatch.setattr(wiring, "DEFAULT_HOME", home / ".codex")
    monkeypatch.setattr(wiring, "ISOLATED_PATH", home / ".codex-cli")
    monkeypatch.setattr(wiring, "WRAPPER", home / ".local/bin/codex")
    monkeypatch.setattr(wiring.shutil, "which", lambda _: None)
    monkeypatch.setattr(discovery, "resolve_codex_bin", lambda: Path("/bin/true"))
    monkeypatch.setattr(cli.discovery, "resolve_codex_bin", lambda: Path("/bin/true"))

    class Box:
        pass

    b = Box()
    b.home = home
    b.app = app
    b.mp = monkeypatch
    return b


def _script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


# ── exec: 사용자가 codex 를 칠 때마다 지나는 자리 ─────────────────────────────


@pytest.mark.parametrize("sub", ["login", "logout", "mcp-server"])
def test_exec_does_not_switch_before_login_logout_or_a_long_lived_server(box, sub: str) -> None:
    """dotfiles wrapper 가 오래 지켜 온 규칙이다. 빠뜨리면 로그아웃이 **다른 계정**에 떨어진다."""
    plan = cli.exec_plan(config.load(), [sub], "/opt/x/bin/codex", {"PATH": "/usr/bin"})
    assert plan.rotate is False


@pytest.mark.parametrize("argv", [[], ["exec", "hi"], ["app-server"], ["--version"]])
def test_exec_switches_before_everything_else(box, argv: list[str]) -> None:
    plan = cli.exec_plan(config.load(), argv, "/opt/x/bin/codex", {"PATH": "/usr/bin"})
    assert plan.rotate is True


def test_exec_starts_codex_in_our_home(box) -> None:
    box.app.mkdir(parents=True)
    s = config.load()
    plan = cli.exec_plan(s, ["exec"], "/opt/x/bin/codex", {"PATH": "/usr/bin"})
    assert plan.env["CODEX_HOME"] == str(s.default_home)
    assert s.default_home.name == ".codex-cli"


def test_exec_lets_an_npm_codex_find_node(box) -> None:
    """**npm codex 는 `#!/usr/bin/env node` 스크립트다.** 좁은 PATH 에서 127 로 죽는다.

    프로브는 이 보정을 하고 있었는데 `exec` 만 빠져 있었다 — GUI 나 launchd 가 띄운 호출자에서
    codex 가 아예 안 뜬다.
    """
    plan = cli.exec_plan(config.load(), [], "/opt/homebrew/bin/codex", {"PATH": "/usr/bin:/bin"})
    assert plan.env["PATH"].split(os.pathsep)[0] == "/opt/homebrew/bin", plan.env["PATH"]
    assert plan.argv[0] == "/opt/homebrew/bin/codex"


def test_home_prints_only_the_path(box, capsys) -> None:
    """**명령 치환으로 먹히는 출력이다.** 안내 한 글자라도 섞이면 `CODEX_HOME` 이 깨진다."""
    box.app.mkdir(parents=True)
    assert cli.main(["home"]) == 0
    out = capsys.readouterr().out
    assert out == f"{config.load().default_home}\n", repr(out)


# ── 배선 분류: 무엇이 codex 를 띄우는가 ───────────────────────────────────────


def test_a_wrapper_that_only_rotates_is_incomplete_when_the_app_is_there(box) -> None:
    """**전환은 하는데 codex 는 앱의 홈으로 뜬다.** 한 기기의 dotfiles wrapper 가 이랬다."""
    w = _script(
        box.home / "bin/codex",
        '#!/bin/bash\ncodex-swap rotate >/dev/null || true\nexec real "$@"\n',
    )
    box.mp.setattr(wiring.shutil, "which", lambda _: str(w))
    state = wiring.inspect("bash")
    assert state.kind == wiring.EXTERNAL
    assert not state.complete(isolate=True)
    assert state.complete(isolate=False), "앱이 없으면 전환만으로 충분하다"


def test_a_wrapper_that_passes_our_home_is_complete(box) -> None:
    w = _script(
        box.home / "bin/codex",
        f'#!/bin/bash\n{wiring.HOME_LINE}\ncodex-swap rotate >/dev/null || true\nexec real "$@"\n',
    )
    box.mp.setattr(wiring.shutil, "which", lambda _: str(w))
    assert wiring.inspect("bash").complete(isolate=True)


def test_the_old_bash_switcher_is_recognised(box) -> None:
    """옛 전환기는 **우리와 같은 원장과 락**을 쓴다. 둘이 살아 있으면 서로를 번갈아 덮는다."""
    w = _script(
        box.home / "bin/codex", '#!/bin/bash\n"$script_dir/codex-account" rotate\nexec real "$@"\n'
    )
    box.mp.setattr(wiring.shutil, "which", lambda _: str(w))
    state = wiring.inspect("bash")
    assert state.kind == wiring.LEGACY
    assert not state.complete(isolate=False)


# ── init: 그 분류를 사람에게 어떻게 말하나 ─────────────────────────────────────


def test_init_flags_a_half_wired_machine_instead_of_calling_it_ready(box, capsys) -> None:
    box.app.mkdir(parents=True)
    w = _script(
        box.home / "bin/codex",
        '#!/bin/bash\ncodex-swap rotate >/dev/null || true\nexec real "$@"\n',
    )
    box.mp.setattr(wiring.shutil, "which", lambda _: str(w))
    code = cli.main(["init"])
    out = capsys.readouterr().out
    assert code == 1, out
    assert "ready" not in out, "전환이 codex 에 안 닿는데 준비됐다고 했다"
    assert wiring.HOME_LINE in out, out


def test_init_names_the_old_switcher(box, capsys) -> None:
    w = _script(box.home / "bin/codex", '#!/bin/bash\n"$d/codex-account" rotate\nexec real "$@"\n')
    box.mp.setattr(wiring.shutil, "which", lambda _: str(w))
    assert cli.main(["init"]) == 1
    out = capsys.readouterr().out
    assert "codex-account" in out and "codex-swap rotate" in out, out


def test_init_never_tells_you_to_copy_the_apps_login(box, capsys) -> None:
    """**앱의 `auth.json` 을 베끼면 refresh token 을 앱과 나눠 쥔다.** 그게 이 다툼의 뿌리다.

    예전 안내가 정확히 그 복사를 시켰다(`cp ~/.codex/auth.json …`).
    """
    box.app.mkdir(parents=True)
    _auth(box.home / ".codex/auth.json", "app@example.com")
    cli.main(["init"])
    out = capsys.readouterr().out
    assert "cp " not in out, out
    assert "adopt" not in out, "앱이 있는 기기에서 '지금 로그인' 을 가져오게 했다"
    assert "codex-swap add" in out, out


def test_init_still_offers_adopt_where_there_is_no_app(box, capsys) -> None:
    """앱이 없으면 지금 로그인은 온전히 사용자 것이다. 새로 로그인시킬 이유가 없다."""
    _auth(box.home / ".codex/auth.json", "me@example.com")
    cli.main(["init"])
    assert "codex-swap adopt" in capsys.readouterr().out


# ── doctor: 앱과 토큰 계보를 공유하는 자리 ──────────────────────────────────────


def test_doctor_finds_a_slot_sharing_a_refresh_token_with_the_app(box) -> None:
    box.app.mkdir(parents=True)
    s = config.load()
    _auth(box.home / ".codex/auth.json", "shared@example.com", refresh="rt-LINEAGE-SECRET")
    _auth(s.accounts_dir / "shared/auth.json", "shared@example.com", refresh="rt-LINEAGE-SECRET")
    _auth(s.accounts_dir / "master/auth.json", "master@example.com", refresh="rt-own-login")

    found = doctor.shared_with_app(s)
    assert [f.label for f in found] == ["shared"], found
    assert "add shared --force" in found[0].fix
    rendered = " ".join(f"{f.detail} {f.fix}" for f in found)
    assert "SECRET" not in rendered, "진단 문구에 토큰 조각이 섞였다"


def test_doctor_is_quiet_when_every_slot_has_its_own_login(box) -> None:
    box.app.mkdir(parents=True)
    s = config.load()
    _auth(box.home / ".codex/auth.json", "app@example.com", refresh="rt-app")
    _auth(s.accounts_dir / "work/auth.json", "work@example.com", refresh="rt-work")
    assert doctor.shared_with_app(s) == []


def test_doctor_calls_out_sharing_the_apps_home_outright(box) -> None:
    """분리하지 않은 채 앱과 같은 홈을 쓰면, 토큰을 견줄 것도 없이 그 자체가 원인이다."""
    box.app.mkdir(parents=True)
    box.mp.setenv("CODEX_ACCOUNT_DEFAULT_HOME", str(box.home / ".codex"))
    found = doctor.shared_with_app(config.load())
    assert len(found) == 1 and "both using" in found[0].detail, found


def test_doctor_has_nothing_to_say_about_the_app_when_there_is_none(box) -> None:
    s = config.load()
    _auth(box.home / ".codex/auth.json", "me@example.com", refresh="rt-same")
    _auth(s.accounts_dir / "me/auth.json", "me@example.com", refresh="rt-same")
    assert doctor.shared_with_app(s) == []


# ── update: brew 로 깐 사람을 옛 판에 가두지 않는다 ────────────────────────────


def test_a_brew_head_install_is_told_about_fetch_head() -> None:
    """HEAD formula 는 `brew upgrade` 가 건너뛴다. 앞쪽만 안내하면 성공한 척 옛 판에 갇힌다."""
    install = selfupdate.Install(manager="brew", source="git+https://example/repo.git")
    with pytest.raises(selfupdate.UpdateError, match="--fetch-HEAD"):
        selfupdate.upgrade_command(install)
