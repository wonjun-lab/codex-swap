"""공식 앱과 **한 기기에서 같이 사는가.**

한 기기에서 실제로 본 것들이 이 파일의 출발점이다.

- 앱의 codex 가 `CODEX_HOME=~/.codex` 로 떠서 `auth.json` 을 제자리에서 갱신하고, 자기 세션으로
  되돌려 놓았다. 원장에는 `A -> B` 만 쌓이고 `B -> A` 는 없었다.
- 같은 refresh token 을 두 곳이 쥐어, 앱의 codex 로그에 `401 Encountered invalidated oauth
  token` 이 수만 줄 쌓였다.
- dotfiles wrapper 는 전환을 하긴 했지만 codex 를 앱의 홈으로 띄웠고, 다른 기기의 wrapper 는
  아예 옛 bash 전환기를 부르고 있었다.

여기서 재는 것은 그 각각이 **다시 조용히 일어나지 않는가**다. 실제 codex 도, 실제 토큰도 쓰지
않는다. 판정에 넣는 입력은 **제품 상수를 되받아 쓰지 않고 글자 그대로 적는다** — 상수를
입력으로 쓰면 그 상수가 틀려도 테스트가 따라 틀려서 아무것도 못 잡는다.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

from codex_swap import cli
from codex_swap.core import config, discovery, doctor, selfupdate, wiring

# 사용자에게 안내하는 한 줄. 상수가 아니라 글자로 적어 둔다.
HOME_LINE = 'export CODEX_HOME="$(codex-swap home)"'

# dotfiles 의 codex/codex.sh 가 실제로 쓰는 모양 (변수를 거쳐 넘긴다).
DOTFILES_WRAPPER = """#!/usr/bin/env bash
set -euo pipefail
rotate_cmd="$(command -v codex-swap 2> /dev/null || true)"
if [[ -z "${CODEX_HOME:-}" && -x "$rotate_cmd" ]]; then
  swap_home="$("$rotate_cmd" home 2> /dev/null || true)"
  if [[ -n "$swap_home" ]]; then
    export CODEX_HOME="$swap_home"
  fi
fi
case "${1:-}" in
  login | logout | mcp-server) ;;
  *) CODEX_ACCOUNT_BIN="$real_codex" "$rotate_cmd" rotate > /dev/null || true ;;
esac
exec "$real_codex" "$@"
"""


def _auth(path: Path, email: str, refresh: str = "rt-default") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    claims = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    body = {"tokens": {"id_token": f"h.{claims}.s", "refresh_token": refresh}}
    path.write_text(json.dumps(body))


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


def _on_path(box, body: str) -> Path:
    w = _script(box.home / "bin/codex", body)
    box.mp.setattr(wiring.shutil, "which", lambda _: str(w))
    return w


def _register_two(box) -> config.Settings:
    s = config.load()
    _auth(s.accounts_dir / "work/auth.json", "work@example.com", refresh="rt-work")
    _auth(s.accounts_dir / "personal/auth.json", "personal@example.com", refresh="rt-personal")
    _auth(s.default_home / "auth.json", "work@example.com", refresh="rt-work")
    return s


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
    assert plan.env["CODEX_HOME"] == str(box.home / ".codex-cli")


def test_exec_lets_an_npm_codex_find_node(box) -> None:
    """**npm codex 는 `#!/usr/bin/env node` 스크립트다.** 좁은 PATH 에서 127 로 죽는다."""
    plan = cli.exec_plan(config.load(), [], "/opt/homebrew/bin/codex", {"PATH": "/usr/bin:/bin"})
    assert plan.env["PATH"].split(os.pathsep)[0] == "/opt/homebrew/bin", plan.env["PATH"]
    assert plan.argv[0] == "/opt/homebrew/bin/codex"


def test_an_inherited_app_home_does_not_stop_switching(box) -> None:
    """**물려받은 `CODEX_HOME=~/.codex` 때문에 전환이 매번 조용히 멈췄다.**

    앱과 나뉜 기기에서 그 값은 앱의 홈이다. 따르면 codex 는 앱의 계정으로 뜨고, 전환 가드는
    "호출자가 다른 홈을 골랐다" 며 멈춘다. 우리 자리로 되돌리고, 판단도 그 홈으로 한다.
    """
    box.app.mkdir(parents=True)
    s = config.load()
    env = {"PATH": "/usr/bin", "CODEX_HOME": str(box.home / ".codex")}
    plan = cli.exec_plan(s, ["exec"], "/opt/x/bin/codex", env)
    assert plan.env["CODEX_HOME"] == str(box.home / ".codex-cli")
    assert plan.rotate is True


def test_the_switch_is_judged_against_the_home_codex_will_use(box) -> None:
    box.app.mkdir(parents=True)
    s = config.load()
    box.mp.setenv("CODEX_HOME", str(box.home / ".codex"))
    plan = cli.exec_plan(s, ["exec"], "/opt/x/bin/codex", dict(os.environ))
    seen: list[str | None] = []
    box.mp.setattr(
        cli.rotate, "rotate", lambda _s, dry_run: seen.append(os.environ.get("CODEX_HOME"))
    )
    cli.rotate_for_exec(s, plan)
    assert seen == [str(box.home / ".codex-cli")], seen
    assert os.environ["CODEX_HOME"] == str(box.home / ".codex"), "판단 뒤에 환경을 되돌리지 않았다"


def test_a_home_the_caller_chose_on_purpose_is_respected(box) -> None:
    """일부러 다른 홈에서 일하는 사람이 있다.

    그 홈의 자격증명은 우리 것이 아니므로 전환하지 않는다.
    """
    box.app.mkdir(parents=True)
    env = {"PATH": "/usr/bin", "CODEX_HOME": "/work/sandbox-home"}
    plan = cli.exec_plan(config.load(), ["exec"], "/opt/x/bin/codex", env)
    assert plan.env["CODEX_HOME"] == "/work/sandbox-home"
    assert plan.rotate is False


def test_home_prints_only_the_path(box, capsys) -> None:
    """**명령 치환으로 먹히는 출력이다.** 안내 한 글자라도 섞이면 `CODEX_HOME` 이 깨진다."""
    box.app.mkdir(parents=True)
    assert cli.main(["home"]) == 0
    assert capsys.readouterr().out == f"{box.home / '.codex-cli'}\n"


def test_discovery_never_mistakes_our_own_wrapper_for_codex(box, tmp_path) -> None:
    """**못 알아보면 `exec` 가 자기 자신을 끝없이 다시 띄운다.**

    discovery 는 dotfiles wrapper 의 표시만 알던 시절이 있었다. upstream 이 사라진 기기에서
    codex-swap 이 놓은 wrapper 가 진짜 codex 로 뽑혔다.
    """
    ours = _script(tmp_path / "codex", wiring.WRAPPER_BODY)
    assert discovery.is_wrapper(ours)


# ── 배선 판정: "홈을 넘기는가" 는 실행되는 줄로만 본다 ──────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        HOME_LINE + "\ncodex-swap rotate >/dev/null || true\n",
        DOTFILES_WRAPPER,
        'exec codex-swap exec "$@"\n',
    ],
    ids=["canonical-line", "dotfiles-via-variable", "delegates-to-exec"],
)
def test_real_ways_of_passing_the_home_are_recognised(body: str) -> None:
    assert wiring.passes_home("#!/bin/sh\n" + body)


@pytest.mark.parametrize(
    "body",
    [
        "# CODEX_HOME is set elsewhere\ncodex-swap rotate || true\n",
        "unset CODEX_HOME\ncodex-swap rotate || true\n",
        'export CODEX_HOME="$HOME/.codex"\ncodex-swap rotate || true\n',
        "codex-swap rotate || true\n" + HOME_LINE + "\n",
        HOME_LINE + "\ncodex-swap rotate || true\nunset CODEX_HOME\n",
        "# " + HOME_LINE + "\ncodex-swap rotate || true\n",
    ],
    ids=["comment", "unset", "old-home", "after-rotate", "unset-later", "commented-out"],
)
def test_lookalikes_do_not_count_as_passing_the_home(body: str) -> None:
    """설명 주석·`unset`·옛 홈을 명시한 줄이 전부 "넘긴다" 로 읽혔었다."""
    assert not wiring.passes_home("#!/bin/sh\n" + body)


def test_a_wrapper_that_only_rotates_is_incomplete_when_apart(box) -> None:
    _on_path(box, '#!/bin/bash\ncodex-swap rotate >/dev/null || true\nexec real "$@"\n')
    state = wiring.inspect("bash")
    assert state.kind == wiring.EXTERNAL
    assert not state.complete(isolate=True)
    assert state.complete(isolate=False), "앱과 나뉘지 않았으면 전환만으로 충분하다"


def test_the_dotfiles_wrapper_is_complete(box) -> None:
    _on_path(box, DOTFILES_WRAPPER)
    assert wiring.inspect("bash").complete(isolate=True)


def test_the_old_bash_switcher_is_recognised(box) -> None:
    _on_path(box, '#!/bin/bash\n"$script_dir/codex-account" rotate\nexec real "$@"\n')
    state = wiring.inspect("bash")
    assert state.kind == wiring.LEGACY
    assert not state.complete(isolate=False)


def test_a_migration_comment_does_not_hide_the_old_switcher(box) -> None:
    """옛 전환기를 부르는 줄은 그대로인데, 주석 하나에 판정이 사라졌다.

    `# codex-swap 으로 옮길 것` 같은 설명만으로 옛 전환기가 아닌 것으로 읽혔다.
    """
    _on_path(box, '#!/bin/bash\n# migrate to codex-swap home someday\n"$d/codex-account" rotate\n')
    assert wiring.inspect("bash").kind == wiring.LEGACY


# ── init: 그 판정을 사람에게 어떻게 말하나 ─────────────────────────────────────


def test_init_flags_a_half_wired_machine_instead_of_calling_it_ready(box, capsys) -> None:
    """계정을 둘 등록해 둔다 — 종료 코드 1 이 **배선 때문**이라는 것을 가려내려고."""
    box.app.mkdir(parents=True)
    _register_two(box)
    _on_path(box, '#!/bin/bash\ncodex-swap rotate >/dev/null || true\nexec real "$@"\n')
    code = cli.main(["init"])
    out = capsys.readouterr().out
    assert code == 1, out
    assert "ready" not in out, "전환이 codex 에 안 닿는데 준비됐다고 했다"
    assert 'CODEX_HOME="$(codex-swap home)"' in out, out
    assert "before it runs codex-swap rotate" in out, out


def test_init_puts_the_home_line_before_the_rotate_line(box, capsys) -> None:
    box.app.mkdir(parents=True)
    _register_two(box)
    _on_path(box, '#!/bin/bash\n"$d/codex-account" rotate\nexec real "$@"\n')
    cli.main(["init"])
    out = capsys.readouterr().out
    assert "codex-account" in out, out
    assert out.index("codex-swap home") < out.index("codex-swap rotate"), out


def test_init_is_ready_on_a_machine_without_the_app(box, capsys) -> None:
    """ml-main 모양: 앱 없음, dotfiles wrapper 는 `rotate` 만 부른다. 이것은 **완성된** 배선이다."""
    _register_two(box)
    _on_path(box, '#!/bin/bash\ncodex-swap rotate >/dev/null || true\nexec real "$@"\n')
    assert cli.main(["init"]) == 0
    assert "ready" in capsys.readouterr().out


def test_init_does_not_claim_apart_when_pointed_at_the_apps_home(box, capsys) -> None:
    """**설치돼 있다와 나뉘어 있다는 다르다.**

    앱의 홈을 직접 가리킨 설정에 "나눠 뒀다" 고 말했었다.
    """
    box.app.mkdir(parents=True)
    box.mp.setenv("CODEX_ACCOUNT_DEFAULT_HOME", str(box.home / ".codex"))
    _register_two(box)
    code = cli.main(["init"])
    out = capsys.readouterr().out
    assert code == 1, out
    assert "kept apart" not in out, out
    assert "both using" in out, out


def test_init_flags_an_exported_app_home(box, capsys) -> None:
    box.app.mkdir(parents=True)
    _register_two(box)
    box.mp.setenv("CODEX_HOME", str(box.home / ".codex"))
    assert cli.main(["init"]) == 1
    assert "exports CODEX_HOME" in capsys.readouterr().out


def test_init_never_tells_you_to_copy_the_apps_login(box, capsys) -> None:
    """**앱의 `auth.json` 을 베끼면 refresh token 을 앱과 나눠 쥔다.**

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
    _auth(box.home / ".codex/auth.json", "me@example.com")
    cli.main(["init"])
    assert "codex-swap adopt" in capsys.readouterr().out


# ── doctor: 앱과 토큰을 나눠 쥔 자리 ────────────────────────────────────────────


def _shared_setup(box) -> config.Settings:
    box.app.mkdir(parents=True)
    s = config.load()
    _auth(box.home / ".codex/auth.json", "shared@example.com", refresh="rt-LINEAGE-SECRET")
    _auth(s.accounts_dir / "shared/auth.json", "shared@example.com", refresh="rt-LINEAGE-SECRET")
    _auth(s.accounts_dir / "master/auth.json", "master@example.com", refresh="rt-own-login")
    return s


def test_doctor_finds_a_slot_sharing_a_refresh_token_with_the_app(box) -> None:
    s = _shared_setup(box)
    found = doctor.shared_with_app(s)
    assert [f.label for f in found] == ["shared"], found
    assert "add shared --force" in found[0].fix
    assert "SECRET" not in " ".join(f"{f.detail} {f.fix}" for f in found)


def test_doctor_never_probes_a_slot_that_shares_the_apps_token(box) -> None:
    """**프로브가 그 토큰을 갱신하면 앱이 로그아웃된다.** 진단하다 앱을 망가뜨리면 안 된다.

    게다가 프로브를 먼저 돌리면 그 갱신이 공유의 증거까지 지워, 경고할 슬롯이 "reachable" 로
    나왔다.
    """
    s = _shared_setup(box)
    probed: list[str] = []

    def fake_check(_settings, label, _active):
        probed.append(label)
        return doctor.Finding(label, doctor.OK, "reachable")

    box.mp.setattr(doctor, "check", fake_check)
    box.mp.setattr(doctor, "drifted", lambda _s: None)
    found = doctor.run(s)
    assert "shared" not in probed, probed
    assert found[0].state == doctor.SHARED


def test_a_malformed_token_cannot_leak_through_an_exception(box, tmp_path) -> None:
    """짝 없는 surrogate 를 그대로 `encode()` 하면 예외 객체에 토큰 원문이 실린다."""
    p = tmp_path / "auth.json"
    p.write_text(json.dumps({"tokens": {"refresh_token": "rt-\ud800-SECRET"}}))
    assert isinstance(doctor._refresh_fingerprint(p), str)


def test_doctor_is_quiet_when_every_slot_has_its_own_login(box) -> None:
    box.app.mkdir(parents=True)
    s = config.load()
    _auth(box.home / ".codex/auth.json", "app@example.com", refresh="rt-app")
    _auth(s.accounts_dir / "work/auth.json", "work@example.com", refresh="rt-work")
    assert doctor.shared_with_app(s) == []


def test_doctor_calls_out_sharing_the_apps_home_outright(box) -> None:
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
    install = selfupdate.Install(manager="brew", source="git+https://example/repo.git")
    with pytest.raises(selfupdate.UpdateError, match="--fetch-HEAD"):
        selfupdate.upgrade_command(install)
