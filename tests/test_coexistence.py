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
if [[ -x "$rotate_cmd" ]]; then
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


def test_home_gives_wrappers_the_answer_exec_uses(box, capsys) -> None:
    """**외부 wrapper 가 따로 판단하면 `exec` 와 갈린다.**

    `home` 이 설정값만 내던 때, dotfiles wrapper 는 "이미 정해져 있으면 둔다" 는 조건을 달았고
    그 조건이 물려받은 앱의 홈까지 지켜 codex 를 앱의 계정으로 띄웠다(교차 검토). `exec` 는 같은
    상황을 바로잡는다. 판단을 `home` 에 두면 wrapper 는 조건 없이 받아 쓰기만 하면 된다.
    """
    box.app.mkdir(parents=True)
    ours = f"{box.home / '.codex-cli'}\n"
    box.mp.setenv("CODEX_HOME", str(box.home / ".codex"))
    assert cli.main(["home"]) == 0
    assert capsys.readouterr().out == ours, "물려받은 앱의 홈을 그대로 돌려줬다"
    box.mp.setenv("CODEX_HOME", "/work/sandbox-home")
    assert cli.main(["home"]) == 0
    assert capsys.readouterr().out == "/work/sandbox-home\n", "일부러 고른 홈을 덮었다"


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
        'CODEX_HOME="$(codex-swap home)"\nexport CODEX_HOME\ncodex-swap rotate || true\n',
        'export CODEX_HOME="$("codex-swap" home)"\ncodex-swap rotate || true\n',
        'codex() { codex-swap exec "$@"; }\n',
        "# don't rotate before the home is set\n" + HOME_LINE + "\ncodex-swap rotate || true\n",
        HOME_LINE + "; codex-swap rotate || true\n",
        'command -v codex-swap >/dev/null && export CODEX_HOME="$(codex-swap home)"\n'
        "codex-swap rotate || true\n",
        'command -- codex-swap exec "$@"\n',
        "main() {\n  "
        + HOME_LINE
        + '\n  codex-swap rotate || true\n  exec real "$@"\n}\nmain "$@"\n',
        "alias codex='codex-swap exec'\n",
        "$'codex-swap' exec \"$@\"\n",
    ],
    ids=[
        "canonical-line",
        "dotfiles-via-variable",
        "delegates-to-exec",
        "export-on-its-own-line",
        "quoted-program-name",
        "shell-function",
        "apostrophe-in-a-comment",
        "one-line",
        "guarded-one-liner",
        "command-double-dash",
        "helper-function-that-is-called",
        "alias-named-codex",
        "ansi-c-quoted-program-name",
    ],
)
def test_real_ways_of_passing_the_home_are_recognised(body: str) -> None:
    """놓치면 멀쩡한 기기에 "배선이 반쪽" 이라고 말한다. 주석 속 `don't` 도 흔하다."""
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
        'CODEX_HOME="$(codex-swap home)"\ncodex-swap rotate || true\n',
        'export CODEX_HOME\nCODEX_HOME="$(codex-swap home)" codex-swap rotate\nexec real "$@"\n',
        "cat <<'EOF'\n" + HOME_LINE + "\nEOF\ncodex-swap rotate || true\n",
        HOME_LINE + "\nCODEX_HOME=/somewhere/else\ncodex-swap rotate || true\n",
        'h="$(codex-swap home)"\nh=/somewhere/else\nexport CODEX_HOME="$h"\ncodex-swap rotate\n',
        HOME_LINE + "; unset CODEX_HOME\ncodex-swap rotate || true\n",
        'echo "codex-swap exec"\ncodex-swap rotate || true\nexec real "$@"\n',
        'export CODEX_HOME="$(codex-swap home)/sub"\ncodex-swap rotate || true\n',
        ': "${CODEX_HOME:=$(codex-swap home)}"\nexport CODEX_HOME\ncodex-swap rotate\n',
        'codex-swap rotate || true\nexec "$real_codex" exec "$@"\n',
        HOME_LINE + '\nunset CODEX_HOME\nCODEX_HOME="$(codex-swap home)"\ncodex-swap rotate\n',
        HOME_LINE + "\nexport -n CODEX_HOME\ncodex-swap rotate || true\n",
        "export CODEX_HOME='$(codex-swap home)'\ncodex-swap rotate || true\n",
        'export CODEX_HOME="$(codex-swap home | sed s,a,b,)"\ncodex-swap rotate\n',
        'helper=printf\nexport CODEX_HOME="$("$helper" home)"\ncodex-swap rotate\n',
        HOME_LINE + ' | cat\ncodex-swap rotate || true\nexec real "$@"\n',
        HOME_LINE + '\ncodex-swap rotate || true\nCODEX_HOME=/other exec real "$@"\n',
        HOME_LINE + '\ncodex-swap rotate || true\nexec env -i real "$@"\n',
        HOME_LINE + '\ncodex-swap rotate || true\nexec env -u CODEX_HOME real "$@"\n',
        'export CODEX_HOME="$(codex-swap home || true; echo /x)"\ncodex-swap rotate\n',
        'swap=codex-swap\nswap=printf\nexport CODEX_HOME="$("$swap" home)"\ncodex-swap rotate\n',
        "alias other='codex-swap exec'\ncodex-swap rotate || true\n",
        'h="$(codex-swap home)" | cat\nexport CODEX_HOME="$h"\ncodex-swap rotate\n',
    ],
    ids=[
        "comment",
        "unset",
        "old-home",
        "after-rotate",
        "unset-later",
        "commented-out",
        "never-exported",
        "only-for-one-command",
        "inside-a-heredoc",
        "overwritten",
        "variable-overwritten",
        "unset-on-the-same-line",
        "exec-only-printed",
        "path-appended",
        "default-keeps-an-inherited-home",
        "codex-own-exec-subcommand",
        "set-again-after-unset-without-export",
        "export-attribute-removed",
        "single-quoted-literal",
        "output-rewritten-by-a-pipe",
        "helper-is-not-codex-swap",
        "export-inside-a-pipeline",
        "child-given-another-home",
        "child-given-no-environment",
        "child-loses-the-home",
        "extra-output-after-the-fallback",
        "helper-reassigned-away",
        "alias-with-another-name",
        "assignment-inside-a-pipeline",
    ],
)
def test_lookalikes_do_not_count_as_passing_the_home(body: str) -> None:
    """설명 주석·`unset`·옛 홈을 명시한 줄이 전부 "넘긴다" 로 읽혔었다.

    줄 단위 정규식으로 고친 뒤에도 export 없는 대입·heredoc 본문·재대입·같은 줄의 unset·찍기만
    한 `exec` 가 통과했다(교차 검토 재현). 하나하나가 codex 를 앱의 홈으로 띄우는 wrapper 다.
    """
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


@pytest.mark.parametrize(
    "body",
    [
        '#!/bin/bash\n"$script_dir/codex-account" rotate\nexec real "$@"\n',
        '#!/bin/bash\nrotate_cmd="$script_dir/codex-account"\n"$rotate_cmd" rotate\n',
        '#!/bin/bash\necho "moving to codex-swap"\n"$d/codex-account" rotate\n',
        "#!/bin/bash\necho '$(codex-swap rotate)'\n\"$d/codex-account\" rotate\n",
        '#!/bin/bash\ncmd=codex-swap\ncmd=printf\n"$cmd" exec\n"$d/codex-account" rotate\n',
        "#!/bin/bash\nalias other='codex-swap exec'\n\"$d/codex-account\" rotate\n",
        '#!/bin/bash\n( "$d/codex-account" rotate )\nexec real "$@"\n',
        '#!/bin/bash\nexport CODEX_HOME="$(codex-swap home)"\n"$d/codex-account" rotate\n',
    ],
    ids=[
        "direct",
        "through-a-variable",
        "codex-swap-only-printed",
        "inside-single-quotes",
        "variable-reassigned-away",
        "alias-with-another-name",
        "inside-a-subshell",
        "asks-codex-swap-only-for-the-home",
    ],
)
def test_the_old_bash_switcher_is_recognised(box, body: str) -> None:
    """글자로 찍힌 `codex-swap` 하나에 옛 전환기 판정이 사라졌었다(교차 검토 재현)."""
    _on_path(box, body)
    state = wiring.inspect("bash")
    assert state.kind == wiring.LEGACY
    assert not state.complete(isolate=False)


def test_a_wrapper_that_only_asks_for_the_home_does_not_switch(box) -> None:
    """홈만 받아 쓰고 `rotate` 를 안 부르면 codex 를 칠 때 전환이 일어나지 않는다 — 배선이 아니다.

    그런데도 "codex-swap 을 부른다" 로 읽어 완성된 배선으로 쳤다(교차 검토).
    """
    _on_path(box, '#!/bin/bash\nexport CODEX_HOME="$(codex-swap home)"\nexec real "$@"\n')
    assert wiring.inspect("bash").kind == wiring.NONE


def test_a_long_wrapper_is_read_to_the_end(box) -> None:
    """앞 8KB 만 읽던 때, 뒤쪽의 `unset CODEX_HOME` 이 잘려 나가 "넘긴다" 로 읽혔다."""
    _on_path(
        box,
        "#!/bin/bash\n"
        + HOME_LINE
        + "\ncodex-swap rotate || true\n# "
        + "x" * 9000
        + '\nunset CODEX_HOME\nexec real "$@"\n',
    )
    state = wiring.inspect("bash")
    assert state.kind == wiring.EXTERNAL
    assert not state.complete(isolate=True)


@pytest.mark.parametrize(
    "fallback",
    [
        '[[ -n "$rotate_cmd" ]] || rotate_cmd="$script_dir/codex-account"\n',
        'if [[ -z "$rotate_cmd" ]]; then\n  rotate_cmd="$script_dir/codex-account"\nfi\n',
    ],
    ids=["or-list", "if-block"],
)
def test_a_wrapper_that_falls_back_to_the_old_switcher_counts_as_ours(box, fallback: str) -> None:
    """codex-swap 이 있으면 그것을, 없을 때만 옛 전환기를 부른다 — dotfiles 의 한때 모양.

    조건부로 다시 담은 것을 "덮어썼다" 로 읽으면 codex-swap 쪽 갈래가 사라져 옛 전환기로 분류된다.
    """
    _on_path(
        box,
        '#!/bin/bash\nrotate_cmd="$(command -v codex-swap || true)"\n'
        + fallback
        + '"$rotate_cmd" rotate\nexec real "$@"\n',
    )
    assert wiring.inspect("bash").kind == wiring.EXTERNAL


def test_only_looking_up_the_old_switcher_is_not_calling_it(box) -> None:
    """`command -v` 는 찾기만 한다 — 부르는 것으로 치면 안내만 하는 wrapper 가 옛 배선이 된다."""
    _on_path(
        box,
        '#!/bin/bash\nif command -v codex-account >/dev/null; then echo "old switcher left"; fi\n'
        'exec real "$@"\n',
    )
    assert wiring.inspect("bash").kind == wiring.NONE


def test_a_profile_that_only_uses_codex_swap_is_not_wiring(box) -> None:
    """셸 프로필에서 codex-swap 을 부른다고 전환이 걸린 것은 아니다 — `rotate` 나 `exec` 여야 한다.

    배선이 없는데 있다고 읽으면 `init` 이 wrapper 를 놓지 않고 넘어간다.
    """
    (box.home / ".bashrc").write_text('alias cs="codex-swap"\ncodex-swap list >/dev/null || true\n')
    assert wiring.inspect("bash").kind == wiring.NONE


def test_a_profile_function_that_delegates_to_exec_is_complete(box) -> None:
    (box.home / ".bashrc").write_text('codex() { codex-swap exec "$@"; }\n')
    state = wiring.inspect("bash")
    assert state.kind == wiring.PROFILE
    assert state.complete(isolate=True)


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
    assert "master" in probed, "제 로그인을 가진 슬롯까지 검사를 멈췄다"
    assert found[0].state == doctor.SHARED


def test_sharing_the_apps_home_keeps_the_probe_off_every_copy_of_its_login(box) -> None:
    """**같은 홈이라고 슬롯 비교를 건너뛰었다.**

    그러면 활성 라벨 하나만 프로브에서 빠지고, 앱의 로그인을 그대로 쥔 다른 슬롯은 검사로
    넘어간다 — 같은 계정을 두 이름으로 등록해 두면 그렇다(활성은 이메일로 하나만 고른다).
    그 프로브가 토큰을 갱신하면 앱이 로그아웃된다.
    """
    box.app.mkdir(parents=True)
    box.mp.setenv("CODEX_ACCOUNT_DEFAULT_HOME", str(box.home / ".codex"))
    s = config.load()
    _auth(box.home / ".codex/auth.json", "master@example.com", refresh="rt-APP-LOGIN")
    _auth(s.accounts_dir / "master/auth.json", "master@example.com", refresh="rt-APP-LOGIN")
    _auth(s.accounts_dir / "master-2/auth.json", "master@example.com", refresh="rt-APP-LOGIN")
    _auth(s.accounts_dir / "work/auth.json", "work@example.com", refresh="rt-own-login")
    probed: list[str] = []

    def fake_check(_settings, label, _active):
        probed.append(label)
        return doctor.Finding(label, doctor.OK, "reachable")

    box.mp.setattr(doctor, "check", fake_check)
    box.mp.setattr(doctor, "drifted", lambda _s: None)
    found = doctor.run(s)
    assert probed == ["work"], probed
    assert "both using" in found[0].detail, found
    # 프로브를 막는 것만으로는 모자라다 — 어느 슬롯이 앱과 로그인을 나눠 쥐었는지 **처음부터**
    # 짚어야 사용자가 그 슬롯을 다시 로그인시킨다. 활성이라 검사에서 빠진 슬롯도 마찬가지다.
    assert {"master", "master-2"} <= {f.label for f in found if f.state == doctor.SHARED}, found
    # 같은 홈이면 활성 파일이 곧 앱의 파일이다. "다른 슬롯으로 바꿔라" 를 덧붙이면 틀린 안내다 —
    # 홈을 나누기 전에는 어느 슬롯으로 바꿔도 앱과 같은 파일을 쓴다.
    assert [f.label for f in found].count("active") == 1, found


def test_doctor_looks_again_right_before_each_probe(box) -> None:
    """**공유 검사와 프로브 사이에 슬롯 내용이 바뀔 수 있다** — `add`·`adopt` 가 동시에 돌면.

    첫 검사 결과만 믿으면 그사이 앱의 로그인을 받은 슬롯이 프로브로 넘어가 앱을 로그아웃시킨다.
    """
    s = _shared_setup(box)
    box.mp.setattr(doctor, "shared_with_app", lambda _settings, _labels=None: [])
    probed: list[str] = []

    def fake_check(_settings, label, _active):
        probed.append(label)
        return doctor.Finding(label, doctor.OK, "reachable")

    box.mp.setattr(doctor, "check", fake_check)
    box.mp.setattr(doctor, "drifted", lambda _s: None)
    found = doctor.run(s)
    assert probed == ["master"], probed
    assert [f.label for f in found if f.state == doctor.SHARED] == ["shared"], found


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


# ── 비켜 준 자리는 **실제로 있어야 한다** ──────────────────────────────────────


def test_stepping_aside_creates_the_home_we_hand_to_codex(box) -> None:
    """**앱이 깔린 기기에서 codex 가 아예 안 떴다.**

    `Error finding codex home: CODEX_HOME points to "…/.codex-cli", but that path does not
    exist` — codex 는 없는 `CODEX_HOME` 을 거부한다. 비어 있는 디렉토리는 괜찮다("Not logged
    in" 으로 끝난다). 자리를 비켜 주기로 한 순간 그 경로는 codex 가 한 번도 본 적 없는
    곳이 되므로, 만드는 쪽은 우리여야 한다.
    """
    box.app.mkdir(parents=True)
    s = config.load()
    isolated = box.home / ".codex-cli"
    assert isolated.is_dir(), s.default_home
    assert isolated.stat().st_mode & 0o777 == 0o700, oct(isolated.stat().st_mode)


def test_stepping_aside_does_not_conjure_the_apps_home(box) -> None:
    """**앱의 홈은 앱의 것이다.** 우리가 만들면 "앱을 쓰던 흔적" 을 보는 판정이 흔들린다."""
    box.app.mkdir(parents=True)
    config.load()
    assert not (box.home / ".codex").exists(), "비켜 준 쪽이 아니라 앱의 자리를 만들었다"


def test_the_home_we_print_to_a_wrapper_exists(box, capsys) -> None:
    """wrapper 는 이 한 줄을 그대로 `CODEX_HOME` 에 넣는다. 없는 경로를 주면 codex 가 죽는다."""
    box.app.mkdir(parents=True)
    assert cli.main(["home"]) == 0
    printed = Path(capsys.readouterr().out.strip())
    assert printed.is_dir(), printed


def test_switching_on_a_freshly_separated_machine(box, capsys) -> None:
    """**실기기 재현.** `Switch failed: [Errno 2] No such file or directory:
    '…/.codex-cli/auth.json.tmp.59753'`

    전환은 활성 자리 **옆에** temp 를 쓰고 rename 한다. 그 자리가 없으면 열기부터 실패한다.
    분리 직후에는 아직 아무것도 그 디렉토리를 만든 적이 없다 — 활성 `auth.json` 을 일부러
    두지 않는 것이 이 테스트의 전부다.
    """
    box.app.mkdir(parents=True)
    s = config.load()
    _auth(s.accounts_dir / "work/auth.json", "work@example.com", refresh="rt-work")
    _auth(s.accounts_dir / "personal/auth.json", "personal@example.com", refresh="rt-personal")
    code = cli.main(["use", "work"])
    captured = capsys.readouterr()
    assert code == 0, captured.out + captured.err
    assert (box.home / ".codex-cli/auth.json").is_file()


def test_the_plain_home_is_there_too_without_the_app(box) -> None:
    """앱이 없어도 같은 약속이다 — `~/.codex` 는 codex 가 만들어 주지만 기대지 않는다."""
    config.load()
    assert (box.home / ".codex").is_dir()


@pytest.mark.parametrize("told", ["~/unexpected", "relative-home"], ids=["tilde-kept", "relative"])
def test_a_relative_home_is_not_created_in_the_cwd(box, tmp_path, told: str) -> None:
    """**codex 를 칠 때마다 작업하던 폴더에 `~` 디렉토리를 흘릴 뻔했다.**

    따옴표 안에서 `~` 가 안 풀린 값이나 상대 경로는 현재 디렉토리 기준이다. 활성 홈을 만드는
    자리는 매 codex 호출의 전환이 지나가므로, 거기서 만들면 프로젝트마다 흔적이 남는다(교차 검토).
    """
    work = tmp_path / "some-project"
    work.mkdir()
    box.mp.chdir(work)
    box.mp.setenv("CODEX_ACCOUNT_DEFAULT_HOME", told)
    config.load()
    assert list(work.iterdir()) == [], list(work.iterdir())


# ── update: brew 로 깐 사람을 옛 판에 가두지 않는다 ────────────────────────────


def test_a_brew_head_install_is_told_about_fetch_head() -> None:
    install = selfupdate.Install(manager="brew", source="git+https://example/repo.git")
    with pytest.raises(selfupdate.UpdateError, match="--fetch-HEAD"):
        selfupdate.upgrade_command(install)
