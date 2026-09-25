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


def test_keys_named_in_the_prose_are_keys_the_screen_has() -> None:
    """키를 두 번 옮기는 동안 본문에 옛 키가 남았다 — `o` 키, `p` 화면, `r` 로 새로고침.

    샘플·메뉴 목록만 재던 검사는 그 문장들을 못 봤다. 여기서는 본문이 키를 부르는 두 모양을
    다 잰다: "`x` key/screen" 은 실제로 있는 키여야 하고, "`메뉴 이름` … (`x`)" 는 그 항목의
    키여야 한다.
    """
    screen_keys = (
        set(tui.MENU_KEYS.values())
        | set(tui.MANAGE_KEYS.values())
        | {key for key, _ in (*tui.POLICY_KEYS, *tui.CREDIT_KEYS, *tui.DOCTOR_KEYS)}
        | {"b", "h", "?"}
    )
    for key in re.findall(r"`([a-z])` (?:key|screen)", README):
        assert key in screen_keys, f"README 가 없는 키를 부른다: `{key}`"

    titles = {
        **{title: tui.MENU_KEYS[action] for action, title in tui.MENU},
        **{title: tui.MANAGE_KEYS[action] for action, title in tui.MANAGE_ITEMS},
    }
    pairs = re.findall(r"`([A-Z][A-Za-z ]+)`[^`\n]{0,24}\(`([a-z])`\)", README)
    checked = [(name, key) for name, key in pairs if name in titles]
    assert checked, "검사할 짝을 하나도 못 찾았다 — 정규식이 README 와 어긋났다"
    for name, key in checked:
        assert key == titles[name], f"README: `{name}` 의 키를 `{key}` 로 적었다({titles[name]})"
