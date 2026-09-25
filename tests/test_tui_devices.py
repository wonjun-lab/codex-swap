"""휴대폰·폴더블·태블릿 크기에서 화면이 **쓸 수 있는가**.

폭 몇 개만 재던 검사로는 모양이 전혀 다른 화면을 못 잡는다. 폴더블은 접으면 30 칸 남짓의
길쭉한 화면이고 펴면 거의 정사각형이다. 플립의 커버 화면은 한 줄이 30 칸도 안 되고 열 줄
남짓이다. 태블릿은 세로로 들면 휴대폰을 키운 모양, 가로로 들면 노트북에 가깝다.

크기는 SSH 앱(Termius·Blink·JuiceSSH)의 기본 글꼴에서 흔히 나오는 **글자 칸 수**다.
기기와 글꼴 크기에 따라 몇 칸씩 달라지므로 정확한 값이 아니라 **모양의 대표값**이다.

`_paint` 는 마지막 줄·마지막 칸을 비워 두고 그린다(curses 가 거기 쓰면 에러를 낸다).
그래서 여기서도 `폭 - 1`, `높이 - 1` 로 그린다 — 실제 화면과 같은 조건이어야 뜻이 있다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from codex_swap import tui
from codex_swap.core import config

sys.path.insert(0, str(Path(__file__).parent))

from terminal import Session

DEVICES: dict[str, tuple[int, int]] = {
    "phone-portrait": (40, 40),
    "phone-landscape": (88, 18),
    "fold-cover": (32, 44),
    "fold-cover-landscape": (92, 14),
    "fold-open": (84, 44),
    "fold-open-landscape": (108, 34),
    "flip-cover": (28, 12),
    "tablet-portrait": (96, 58),
    "tablet-landscape": (140, 40),
    "tablet-split-view": (60, 50),
}
"""`이름: (칸, 줄)`. 폴더블은 갤럭시 Z 폴드·플립, 태블릿은 11 인치 급이 기준이다."""

_IDS = list(DEVICES)


@pytest.fixture
def settings(_isolated_home: Path) -> config.Settings:
    return config.load()


def _rows(count: int) -> tuple[tui.Row, ...]:
    return tuple(
        tui.Row(
            f"acct{n:02d}",
            f"someone.{n}@example.com",
            f"{'~' if n % 3 == 1 else ''}{n * 7 % 100}%",
            "09-26 03:10 (in 5h)",
            n == 0,
            stale=n % 3 == 1,
            percent=n * 7 % 100,
            credits=n % 2,
        )
        for n in range(count)
    )


def _view(settings: config.Settings, count: int, **kw) -> tui.View:
    return tui.View(rows=_rows(count), cursor=0, settings=settings, current_rung=70, **kw)


def _draw(view: tui.View, device: str) -> list[tuple[str, tui.Style]]:
    cols, rows = DEVICES[device]
    return tui.render_screen(view, width=cols - 1, height=rows - 1)


def _fits(screen, device: str) -> None:
    cols, rows = DEVICES[device]
    assert len(screen) <= rows - 1, f"{device}: {len(screen)} 줄을 {rows - 1} 줄에 냈다"
    for text, _ in screen:
        assert tui._width(text) <= cols - 1, f"{device}: {text!r}"


def _has_cursor(screen) -> bool:
    return any(
        text.startswith(" >") or any(span.reverse for _, _, span in style.spans)
        for text, style in screen
    )


def _bold(screen) -> set[str]:
    return {
        text[a:b].lower()
        for text, style in screen
        for a, b, span in style.spans
        if span.bold and b - a == 1
    }


# ── 계정 화면 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("count", [2, 6, 15])
@pytest.mark.parametrize("device", _IDS)
def test_the_account_screen_is_usable_everywhere(settings, device: str, count: int) -> None:
    """어느 기기·계정 수에서든, 커서가 어디 있든: 넘치지 않고, 커서가 보이고, 메뉴의 모든
    단축키와 나가는 법이 보이고, 방금 일어난 일(메시지)이 보인다."""
    view = _view(settings, count, message="Switched to acct01")
    first_shape = None
    for cursor in range(tui.cursor_limit(view) + 1):
        screen = _draw(tui.replace(view, cursor=cursor), device)
        _fits(screen, device)
        texts = [text for text, _ in screen]
        assert _has_cursor(screen), f"{device} cursor={cursor}\n" + "\n".join(texts)
        missing = set(tui.MENU_KEYS.values()) - _bold(screen)
        assert not missing, f"{device} cursor={cursor}: {missing}\n" + "\n".join(texts)
        assert any("q quit" in t for t in texts), f"{device} cursor={cursor}\n" + "\n".join(texts)
        assert any(t.strip() == "Switched to acct01" for t in texts), device
        # 커서를 따라 화면 모양이 바뀌지 않는다 — 줄 수와 메뉴의 접힘.
        shape = (len(screen), any("Update codex-swap" in t for t in texts))
        first_shape = first_shape or shape
        assert shape == first_shape, (device, cursor, shape, first_shape)


@pytest.mark.parametrize("device", _IDS)
def test_the_key_hints_keep_their_words_where_there_is_room(settings, device: str) -> None:
    """설명이 빠진 `enter  n  d` 는 처음 보는 사람에게 글자 나열이다.

    높이가 넉넉한 기기에서는 조작법이 전부, 설명과 함께 보여야 한다. 아주 짧은 화면(플립
    커버·가로 폴드)은 계정과 메시지가 먼저라 조작법 줄이 빠질 수 있다 — 거기서도 `?` 로
    전부 볼 수 있다는 것은 아래 도움말 검사가 지킨다.
    """
    _, rows = DEVICES[device]
    texts = " ".join(text for text, _ in _draw(_view(settings, 3), device))
    if rows < 16:
        assert "q quit" in texts, texts
        return
    for key, label in tui.ACCOUNT_KEYS:
        assert f"{key} {label}" in texts, (device, key, texts)


# ── 도움말과 하위 화면 ────────────────────────────────────────────────────


@pytest.mark.parametrize("device", _IDS)
def test_help_fits_and_reaches_its_last_line(settings, device: str) -> None:
    cols, rows = DEVICES[device]
    view = tui.open_help(_view(settings, 3))
    for _ in range(200):
        _fits(_draw(view, device), device)
        nxt = tui.scroll_help(view, +1, height=rows - 1, width=cols - 1)
        if nxt.help_scroll == view.help_scroll:
            break
        view = nxt
    texts = [text for text, _ in _draw(view, device)]
    assert any(t.split()[:2] == ["q", "quit"] for t in texts), f"{device}\n" + "\n".join(texts)
    assert any("b back" in t for t in texts), device


def _sub_screens(settings: config.Settings) -> dict[str, tui.View]:
    base = _view(settings, 3)
    return {
        "policy": tui.replace(base, mode="policy", saved_settings=settings),
        "credits": tui.replace(base, mode="credits", credit_accounts=()),
        "doctor": tui.replace(base, mode="doctor", findings=()),
        "manage": tui.open_manage(base),
        "manage-pick": tui.replace(tui.open_manage(base), pick="rename"),
    }


@pytest.mark.parametrize("device", _IDS)
@pytest.mark.parametrize("name", ["policy", "credits", "doctor", "manage", "manage-pick"])
def test_every_sub_screen_fits_and_shows_the_way_back(settings, device: str, name: str) -> None:
    """들어갔는데 돌아오는 법이 안 보이는 화면은 갇힌 화면이다. 휴대폰에서는 `esc` 가 멀어
    `b` 가 보여야 한다."""
    screen = _draw(_sub_screens(settings)[name], device)
    _fits(screen, device)
    texts = " ".join(text for text, _ in screen)
    back = "b cancel" if name == "manage-pick" else "b back"
    assert back in texts, f"{device} {name}: {texts}"


# ── 실제 터미널 ───────────────────────────────────────────────────────────

_needs_pty = pytest.mark.skipif(
    not hasattr(__import__("os"), "forkpty"), reason="pty 를 못 쓰는 플랫폼"
)


@_needs_pty
@pytest.mark.parametrize("device", ["fold-cover", "fold-open", "flip-cover", "tablet-landscape"])
def test_a_real_terminal_of_that_shape_draws_the_same_screen(tmp_path: Path, device: str) -> None:
    """순수 함수가 옳아도 그리는 쪽이 어긋나면 소용없다. 모양이 가장 다른 넷을 실제 curses
    로 띄워, 화면에 적힌 글자가 `render_lines` 와 줄마다 같은지 본다."""
    cols, rows = DEVICES[device]
    s = Session(tmp_path / "home")
    s.slot("master", "a@example.com")
    s.slot("shared", "b@example.com")
    s.activate("b@example.com")
    s.cache("master", 47)
    s.cache("shared", 77)
    screen = s.run([b"q"], cols=cols, rows=rows, settle=0.8)
    assert screen.exit_code == 0, screen.text
    assert "Traceback" not in screen.text, screen.text

    import os

    saved = {k: os.environ.get(k) for k in s.env()}
    os.environ.update(s.env(cols, rows))
    try:
        expected = tui.render_lines(tui.build_view(config.load()), width=cols - 1, height=rows - 1)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    drawn = [line.rstrip() for line in screen.lines[: len(expected)]]
    assert drawn == [line.rstrip() for line in expected], (
        f"{device}\n--- 화면\n" + "\n".join(drawn) + "\n--- 기대\n" + "\n".join(expected)
    )
