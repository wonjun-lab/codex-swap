"""비켜 준 자리는 **실제로 있어야 한다.**

공식 앱과 한 기기에서 같이 살면서 실제로 본 것이 출발점이다.

- 앱의 codex 가 `CODEX_HOME=~/.codex` 로 떠서 `auth.json` 을 제자리에서 갱신하고, 자기 세션으로
  되돌려 놓았다. 원장에는 `A -> B` 만 쌓이고 `B -> A` 는 없었다.

그래서 앱이 깔린 기기에서는 활성 홈을 `~/.codex-cli` 로 비켜 준다. 그런데 그 자리를 만드는
코드가 없어, 앱이 깔린 기기에 새로 깔자 codex 가 아예 안 떴고 첫 전환도 실패했다. 여기서 재는
것은 그 자리가 **처음부터 있는가**, 그리고 만들면 안 되는 자리는 만들지 않는가다. 실제 codex
도, 실제 토큰도 쓰지 않는다. 판정에 넣는 입력은 **제품 상수를 되받아 쓰지 않고 글자 그대로
적는다** — 상수를 입력으로 쓰면 그 상수가 틀려도 테스트가 따라 틀려서 아무것도 못 잡는다.

임시 HOME `box` 는 `_coexist.py` 에 있다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from _coexist import _auth
from codex_swap import cli
from codex_swap.core import config

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
