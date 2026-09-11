"""README 가 실제 코드와 어긋나지 않는지.

문서는 조용히 낡는다. 이번에 실제로 그랬다 — 키 배치를 바꾸고 나서도 README 의 화면
샘플과 설명은 옛 배치(`enter switch`)를 그대로 광고하고 있었고, 아무도 실패하지 않았다.
퍼블릭 저장소에서 그 줄은 **처음 오는 사람이 가장 먼저 읽는 것**이다.

여기서 고정하는 것은 문장이 아니라 **약속과 구현이 같은가**다. 문구를 다듬는 것은 자유고,
없는 명령을 광고하거나 옛 키를 가르치는 것만 막는다.
"""

from __future__ import annotations

import re
from pathlib import Path

from codex_swap import cli, tui

README = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")


def test_the_screenshot_shows_the_keys_that_exist() -> None:
    """샘플 안의 조작법 줄이 `keys_line` 이 실제로 만드는 것과 같아야 한다."""
    actual, _ = tui.keys_line(tui.ACCOUNT_KEYS, width=200)
    assert actual.strip() in README, (
        f"샘플의 조작법 줄이 낡았다. 지금 코드가 만드는 줄:\n  {actual.strip()}"
    )


def test_every_account_shortcut_is_documented() -> None:
    """행 전용 키도 메뉴에 없다는 이유로 README 에서 사라지면 안 된다."""
    for key, description in tui.ACCOUNT_KEYS:
        assert f"{key} {description}" in README


def test_the_screenshot_shows_the_menu() -> None:
    """메뉴는 이 화면의 절반이다. 샘플에 없으면 있는 줄도 모른다."""
    for _, title in tui.MENU:
        assert title in README, f"샘플에 메뉴 항목이 없다: {title}"


def test_every_documented_command_exists() -> None:
    """없는 명령을 광고하면 사용자는 자기 설치가 낡은 줄 안다."""
    parser = cli.build_parser()
    known = {a for act in parser._actions if getattr(act, "choices", None) for a in act.choices}
    for name in re.findall(r"`codex-swap ([a-z-]+)", README):
        assert name in known, f"README 에 없는 명령이 있다: {name}"


def test_every_documented_flag_exists() -> None:
    """`--fresh` 처럼 나중에 붙인 것이 문서에만 있거나 코드에만 있는 일을 막는다."""
    parser = cli.build_parser()
    subs = next(a for a in parser._actions if getattr(a, "choices", None))
    for cmd, flag in re.findall(r"`codex-swap ([a-z-]+)[^`]*?(--[a-z-]+)", README):
        if cmd not in subs.choices:
            continue
        opts = {o for act in subs.choices[cmd]._actions for o in act.option_strings}
        assert flag in opts, f"README 가 없는 옵션을 광고한다: codex-swap {cmd} {flag}"


def test_the_environment_table_matches_the_code() -> None:
    """환경변수는 이름이 틀리면 조용히 무시된다 — 사용자는 값이 안 먹는 이유를 모른다."""
    documented = set(re.findall(r"`(CODEX_[A-Z_]+)`", README))
    source = set()
    for path in (Path(__file__).resolve().parents[1] / "src").rglob("*.py"):
        source |= set(re.findall(r'"(CODEX_[A-Z_]+)"', path.read_text(encoding="utf-8")))
    missing = documented - source
    assert not missing, f"README 가 코드에 없는 환경변수를 적고 있다: {sorted(missing)}"
