"""`exec`·`home` — 사용자가 codex 를 칠 때마다 지나는 자리.

공식 앱과 한 기기에서 같이 살면서 실제로 본 것들이 이 파일의 출발점이다.

- 앱의 codex 가 `CODEX_HOME=~/.codex` 로 떠서 `auth.json` 을 제자리에서 갱신하고, 자기 세션으로
  되돌려 놓았다. 원장에는 `A -> B` 만 쌓이고 `B -> A` 는 없었다.
- dotfiles wrapper 는 전환을 하긴 했지만 codex 를 앱의 홈으로 띄웠다.

여기서 재는 것은 그 각각이 **다시 조용히 일어나지 않는가**다. 실제 codex 도, 실제 토큰도 쓰지
않는다. 판정에 넣는 입력은 **제품 상수를 되받아 쓰지 않고 글자 그대로 적는다** — 상수를
입력으로 쓰면 그 상수가 틀려도 테스트가 따라 틀려서 아무것도 못 잡는다.

임시 HOME `box` 는 `_coexist.py` 에 있다.
"""

from __future__ import annotations

import os

import pytest

from _coexist import _script
from codex_swap import cli
from codex_swap.core import config, discovery, wiring

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
