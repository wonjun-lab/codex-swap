"""CLI 와 TUI 가 같은 것을 지키는가.

이 프로젝트에서 반복해서 나온 결함이 하나 있다 — **한쪽 표면에만 가드가 있는 것.** 그때마다
약한 쪽이 이 도구의 실제 안전 수준이 됐고, 하필 기본 표면(인자 없이 `codex-swap`)이 TUI 라
그쪽이 약하면 대부분의 사용자가 약한 쪽을 쓴다.

지금까지 그렇게 갈렸던 것들: adopt 의 덮어쓰기 보호 · `use` 의 경고 · `CRED` 열 · 그리고
여기서 잡은 **전환 가드**.

여기서 재는 것은 "두 표면의 문구가 같은가" 가 아니다. 문구는 달라도 된다 — 화면과 파이프는
다른 매체다. 재는 것은 **같은 위험 앞에서 둘 다 멈추는가** 다.

의도적으로 한쪽에만 있는 것은 아래 `KNOWN_ASYMMETRY` 에 이유와 함께 적는다. 적히지 않은
비대칭이 생기면 그것은 결함이다.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from codex_swap import cli, tui
from codex_swap.core import config, store

KNOWN_ASYMMETRY = {
    "rotate": "훅이 부르는 명령이다. 사람이 화면에서 누를 일이 없다",
    "clean": "유지보수용. 잘못 눌러서 좋을 것이 없는 정리 작업이다",
    "--json": "기계용 출력. 화면에는 대응물이 없다",
    "add": "브라우저 로그인을 띄운다. curses 를 벗어났다 돌아오는 경로가 따로 필요하다",
    "rename": "아직 없다. TUI 에 넣을 값어치는 있지만 이번 범위가 아니다",
    "remove": "아직 없다. 위와 같다",
    "policy(cli)": "정책 편집이 TUI 에만 있다. CLI 사용자는 config.json 을 손으로 고쳐야 한다",
    "auto(cli)": "자동 전환 토글이 TUI 에만 있다. README 가 파일을 직접 만들라고 안내한다",
}
"""**적혀 있다고 괜찮다는 뜻은 아니다.** 아는 채로 두었다는 뜻이다."""


def _auth(path: Path, email: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    claims = base64.urlsafe_b64encode(json.dumps({"email": email}).encode())
    path.write_text(json.dumps({"tokens": {"id_token": f"h.{claims.decode().rstrip('=')}.s"}}))


@pytest.fixture
def orphan(_isolated_home: Path) -> config.Settings:
    """슬롯은 둘인데 **지금 로그인된 계정은 어느 슬롯도 아니다.**

    전환하면 그 자격증명이 사라진다 — 슬롯 사본이 없으므로 되돌릴 방법도 없다.
    """
    s = config.load()
    _auth(s.accounts_dir / "master/auth.json", "a@example.com")
    _auth(s.accounts_dir / "shared/auth.json", "b@example.com")
    _auth(s.default_home / "auth.json", "nobody@example.com")
    assert store.active_label(s) is None, "전제가 깨졌다 — 활성이 슬롯과 맞아 버렸다"
    return s


# ── 전환이 로그인된 계정을 버리려 할 때 ─────────────────────────────────────


def test_the_cli_refuses_and_names_the_way_out(orphan: config.Settings) -> None:
    with pytest.raises(cli.CliError) as exc:
        cli.cmd_use(orphan, "master")
    said = str(exc.value)
    assert "not in any slot" in said, said
    assert "adopt" in said, said
    assert "--force" in said, said


def test_the_tui_stops_too_instead_of_only_warning(orphan: config.Settings) -> None:
    """**여기가 갈려 있었다.** TUI 는 꼬리말에 경고 한 줄만 띄우고 그냥 전환했다.

    기본 표면이 자격증명을 더 쉽게 버리면, 경고가 있다는 사실은 위안이 되지 않는다.
    """
    view = tui.build_view(orphan)
    before = store.active_auth(orphan).read_text()

    after = tui.do_switch(view)
    assert after.switch_armed, "묻지도 않고 진행했다"
    assert "nobody@example.com" in after.message, after.message
    assert "adopt" in after.message, after.message
    assert store.active_auth(orphan).read_text() == before, "전환이 이미 일어났다"


def test_pressing_switch_again_goes_through(orphan: config.Settings) -> None:
    """완전히 막지는 않는다. 임시 로그인을 일부러 버리는 용법이 있다 — CLI 의 `--force`."""
    view = tui.do_switch(tui.build_view(orphan))
    after = tui.do_switch(view)
    assert not after.switch_armed
    assert "Switched to" in after.message, after.message


def test_moving_the_cursor_takes_the_arming_back(orphan: config.Settings) -> None:
    """물어본 것을 잊고 나중에 누른 `s` 가 곧바로 버리면 묻는 의미가 없다."""
    armed = tui.do_switch(tui.build_view(orphan))
    assert armed.switch_armed
    moved = tui.build_view(orphan, cursor=armed.cursor + 1)
    assert not moved.switch_armed


def test_a_registered_active_account_switches_without_a_second_press(
    _isolated_home: Path,
) -> None:
    """가드가 평상시를 방해하면 안 된다 — 매번 두 번 눌러야 하면 곧 반사적으로 넘긴다."""
    s = config.load()
    _auth(s.accounts_dir / "master/auth.json", "a@example.com")
    _auth(s.accounts_dir / "shared/auth.json", "b@example.com")
    _auth(s.default_home / "auth.json", "a@example.com")
    view = tui.build_view(s)
    at = next(i for i, row in enumerate(view.rows) if row.label == "shared")
    after = tui.do_switch(tui.replace(view, cursor=at))
    assert not after.switch_armed
    assert "Switched to shared" in after.message, after.message


def test_no_live_login_means_there_is_nothing_to_discard(_isolated_home: Path) -> None:
    """버릴 것이 없는데 묻는 것은 잡음이다."""
    s = config.load()
    _auth(s.accounts_dir / "master/auth.json", "a@example.com")
    _auth(s.accounts_dir / "shared/auth.json", "b@example.com")
    view = tui.build_view(s)
    assert view.active_email is None
    after = tui.do_switch(view)
    assert not after.switch_armed, after.message


# ── 비대칭 목록이 실제와 맞는가 ─────────────────────────────────────────────


def test_every_cli_command_is_either_on_screen_or_listed_as_known(
    _isolated_home: Path,
) -> None:
    """새 명령을 CLI 에만 붙이고 화면을 잊는 일이 반복됐다.

    여기서 막는 것은 "전부 화면에 넣어라" 가 아니다 — 넣지 않기로 했으면 **그 사실을 적어
    두라**는 것이다. 적히지 않은 비대칭은 결함이거나 잊은 것이다.
    """
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if getattr(a, "choices", None))

    # 별칭은 같은 파서를 가리킨다. 이름 단위로 보면 `rm` 이 `remove` 와 따로 걸려,
    # 목록에 둘을 다 적어야 하는 잡일이 생긴다.
    by_parser: dict[int, set[str]] = {}
    for name, sub_parser in sub.choices.items():
        by_parser.setdefault(id(sub_parser), set()).add(name)

    on_screen = {action for action, _ in tui.MENU} | {
        "use",
        "switch",
        "list",
        "ls",
        "status",
    }
    known = on_screen | set(KNOWN_ASYMMETRY)
    missing = {sorted(names)[0] for names in by_parser.values() if not (names & known)}
    assert not missing, (
        f"화면에 없고 목록에도 없는 명령: {sorted(missing)}\n"
        "TUI 에 넣거나, 안 넣는 이유를 KNOWN_ASYMMETRY 에 적어라"
    )
