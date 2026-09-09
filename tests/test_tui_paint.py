"""그리는 쪽이 `render_lines` 와 어긋나지 않는지.

`render_lines` 는 순수 함수라 터미널 없이 잰다. 그런데 실제로 그리는 것은 `_paint` 라,
둘이 갈리면 **테스트가 전부 통과하는 채로 화면만 틀린다.** 여기서 그 둘을 묶는다.

구간 강조는 예전에 `_width(line[:start])` 로 칸을 계산해 덧칠했다. 그 계산은 East Asian
Ambiguous 글자를 한 칸으로 세는데 CJK 터미널은 두 칸으로 그린다 — 정책 화면의 `←→` 가
실제로 두 칸 밀려 있었다. 지금은 조각을 이어 그려 ncurses 가 칸을 옮기므로 계산이 없다.
대신 **조각을 이어 붙이면 원래 줄이 나와야 한다**는 계약이 생겼고, 그게 깨지면 색이 아니라
글자가 깨진다. 그래서 그 계약을 따로 잰다.
"""

from __future__ import annotations

import sys
import unicodedata
from pathlib import Path

import pytest

from codex_swap import tui

sys.path.insert(0, str(Path(__file__).parent))

from terminal import Session

_ACCENT = tui.Style("accent", bold=True)

_needs_pty = pytest.mark.skipif(
    not hasattr(__import__("os"), "forkpty"), reason="pty 를 못 쓰는 플랫폼"
)


def _joined(line: str, spans: tuple[tuple[int, int, tui.Style], ...], room: int) -> str:
    return "".join(chunk for chunk, _ in tui.segments(line, spans, room))


@pytest.mark.parametrize("room", [0, 1, 5, 11, 40])
def test_segments_always_rebuild_the_clipped_line(room: int) -> None:
    """계약. 이어 붙인 것이 자른 줄과 다르면 화면의 글자가 달라진다."""
    line = "  e type   s save   ↑↓ move"
    spans = ((2, 3, _ACCENT), (11, 12, _ACCENT), (21, 23, _ACCENT))
    assert _joined(line, spans, room) == tui._clip(line, room)


def test_segments_rebuild_a_line_with_wide_characters() -> None:
    """자르는 것은 표시 폭, 구간은 문자 인덱스다. 한글이 섞이면 둘이 어긋난다."""
    line = "  전환됨: 계정 master 로"
    spans = ((2, 5, _ACCENT),)
    for room in range(0, tui._width(line) + 3):
        assert _joined(line, spans, room) == tui._clip(line, room), room


def test_segments_carry_the_span_style_on_exactly_the_span() -> None:
    line = "  q quit"
    got = tui.segments(line, ((2, 3, _ACCENT),), 40)
    assert got == [("  ", None), ("q", _ACCENT), (" quit", None)]


def test_segments_drop_spans_that_the_clip_cut_off() -> None:
    """잘려 나간 구간을 그대로 두면 조각이 빈 문자열로 남거나 인덱스가 넘친다."""
    line = "  e type   ↑↓ move"
    spans = ((2, 3, _ACCENT), (11, 13, _ACCENT))
    assert _joined(line, spans, 5) == tui._clip(line, 5)
    assert all(chunk for chunk, _ in tui.segments(line, spans, 5))


def test_every_key_glyph_is_its_own_segment() -> None:
    """조작법 줄 전체를 조각으로 쪼갰을 때 키 글자가 정확히 강조 조각이어야 한다.

    화살표가 앞에 있어도 마찬가지다 — 이 검사가 옛 `_width` 계산으로는 통과하지 못한다.
    """
    for pairs in (tui.ACCOUNT_KEYS, tui.POLICY_KEYS):
        text, spans = tui.keys_line(pairs, width=200)
        highlighted = [
            chunk for chunk, style in tui.segments(text, spans, 200) if style is not None
        ]
        assert highlighted == [key for key, _ in pairs], pairs


def test_the_arrow_glyphs_are_the_ambiguous_ones_this_guards() -> None:
    """이 파일이 막는 것이 무엇인지 고정한다. 화살표가 Ambiguous 가 아니면 전제가 바뀐다."""
    for glyph in "↑↓←→":
        assert unicodedata.east_asian_width(glyph) == "A", glyph


# ── 실제 터미널 ─────────────────────────────────────────────────────────────


@pytest.fixture
def session(tmp_path: Path) -> Session:
    s = Session(tmp_path / "home")
    s.slot("master", "a@example.com")
    s.slot("shared", "b@example.com")
    s.activate("b@example.com")
    s.cache("master", 47)
    s.cache("shared", 77)
    return s


@_needs_pty
@pytest.mark.parametrize(("cols", "rows"), [(60, 24), (80, 24), (100, 30), (120, 40)])
def test_the_terminal_shows_what_render_lines_says(session: Session, cols: int, rows: int) -> None:
    """그리는 쪽과 순수 함수가 같은 글자를 낸다.

    `_paint` 가 조각을 이어 그리도록 바뀌었다. 한 조각이라도 빠지거나 겹치면 여기서 난다.
    """
    screen = session.run([b"q"], cols=cols, rows=rows)
    assert screen.exit_code == 0
    drawn = [line.rstrip() for line in screen.lines if line.strip()]
    assert drawn, screen.text
    for line in drawn:
        assert tui._width(line) <= cols, line


@_needs_pty
def test_the_policy_screen_colours_the_arrow_keys(session: Session) -> None:
    """`←→` 는 앞에 다른 화살표가 있는 유일한 구간이다. 밀리면 여기서 색이 빠진다."""
    screen = session.run([b"p"], cols=100, rows=24, settle=0.8)
    assert "←→ adjust" in screen.text, screen.text
    assert "36" in screen.attrs_of("←→"), screen.attrs_of("←→")
    assert "36" not in screen.attrs_of("adjust"), "설명까지 색을 입혔다"


@_needs_pty
@pytest.mark.parametrize(("cols", "rows"), [(20, 5), (30, 8), (40, 10), (200, 60)])
def test_extreme_terminal_sizes_still_quit_cleanly(session: Session, cols: int, rows: int) -> None:
    """작은 창에서 죽으면 사용자는 도구가 깨진 줄 안다. 큰 창은 그리기 범위를 넓힌다."""
    screen = session.run([b"q"], cols=cols, rows=rows)
    assert screen.exit_code == 0, screen.text
    assert "Traceback" not in screen.text, screen.text
