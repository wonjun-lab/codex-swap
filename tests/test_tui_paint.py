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

import curses as _curses
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


# ── Ambiguous 를 두 칸으로 그리는 터미널 ─────────────────────────────────────


class WideAmbiguousScreen:
    """CJK 터미널을 흉내내는 가짜 화면.

    pty 하네스로는 이 결함을 못 잡는다 — 그 렌더러도 Ambiguous 를 한 칸으로 세기 때문에
    `_paint` 의 계산과 늘 일치한다. glibc 의 `wcwidth` 도 로케일과 무관하게 1 을 주므로
    실제 ncurses 로도 재현되지 않는다. **틀리게 그리는 터미널이 있어야 차이가 보인다.**

    그래서 여기서는 `addstr` 이 커서를 두 칸씩 미는 화면을 만든다. `_paint` 가 칸을
    직접 계산하면 그 계산과 이 화면이 갈리고, ncurses 에게 맡기면(= 이 가짜 화면의
    커서를 따르면) 갈릴 것이 없다.
    """

    def __init__(self, rows: int, cols: int) -> None:
        self.rows, self.cols = rows, cols
        self.erase()

    @staticmethod
    def _cells(ch: str) -> int:
        return 2 if unicodedata.east_asian_width(ch) in "WFA" else 1

    def erase(self) -> None:
        self.text = [[" "] * self.cols for _ in range(self.rows)]
        self.attr = [[0] * self.cols for _ in range(self.rows)]
        self.y = self.x = 0

    def getmaxyx(self) -> tuple[int, int]:
        return self.rows, self.cols

    def move(self, y: int, x: int) -> None:
        self.y, self.x = y, x

    def refresh(self) -> None:
        pass

    def addstr(self, text: str, attr: int = 0) -> None:
        for ch in text:
            width = self._cells(ch)
            if self.x + width > self.cols:
                raise _curses.error("addstr: 화면 끝")
            self.text[self.y][self.x] = ch
            # 넓은 글자의 **뒤 칸**은 빈 문자열로 둔다. 공백으로 두면 줄을 되읽을 때
            # 글자 사이에 없던 칸이 끼어 화면과 다른 문자열이 나온다.
            for k in range(1, width):
                self.text[self.y][self.x + k] = ""
            for k in range(width):
                self.attr[self.y][self.x + k] = attr
            self.x += width

    def addnstr(self, y: int, x: int, text: str, n: int, attr: int = 0) -> None:
        self.move(y, x)
        self.addstr(text, attr)

    def row_text(self, y: int) -> str:
        return "".join(self.text[y]).rstrip()

    def row_with(self, needle: str) -> int:
        return next(y for y in range(self.rows) if needle in self.row_text(y))

    def marked(self, y: int, attr_bit: int) -> str:
        """그 행에서 속성이 걸린 글자를 **열 순서대로**.

        넓은 글자는 두 칸을 차지하지만 글자는 첫 칸에만 있으므로 한 번씩만 모인다.
        """
        return "".join(
            self.text[y][x]
            for x in range(self.cols)
            if self.attr[y][x] & attr_bit and self.text[y][x] not in ("", " ")
        )


@_needs_pty
def test_the_accent_lands_on_the_arrow_keys_even_when_ambiguous_is_two_cells(
    _isolated_home: Path,
) -> None:
    """진짜 회귀. 옛 덧칠 방식은 여기서 `←→` 대신 `move` 를 물들인다."""
    from codex_swap.core import config

    settings = config.load()
    view = tui.replace(tui.View(rows=(), cursor=0, settings=settings), mode="policy")
    screen = WideAmbiguousScreen(24, 120)
    tui._paint(screen, view)

    row = screen.row_with("adjust")

    # **글자가 먼저다.** 엉뚱한 칸에 덧칠하면 `←→` 자체는 굵게 남지만 그 자리에 있던
    # 글자를 덮어쓴다. 강조 위치만 보면 그 피해가 안 보인다 — 실제로 이 검사 없이는
    # 옛 방식으로 되돌리는 뮤테이션이 살아남았다.
    want = next(
        text for text, _ in tui.render_screen(view, height=23, width=119) if "adjust" in text
    )
    assert screen.row_text(row) == want.rstrip(), (
        f"그린 글자가 `render_screen` 과 다르다\n  그린 것: {screen.row_text(row)!r}\n"
        f"  기대:    {want.rstrip()!r}"
    )

    expected = "".join(key for key, _ in tui.POLICY_KEYS)
    assert screen.marked(row, _curses.A_BOLD) == expected, (
        f"강조가 키 글자에서 벗어났다\n  줄: {screen.row_text(row)!r}\n"
        f"  강조된 것: {screen.marked(row, _curses.A_BOLD)!r}\n  기대: {expected!r}"
    )


# ── 하네스 자신이 화면을 제대로 복원하는가 ─────────────────────────────────


def test_the_harness_handles_lines_being_pushed_around() -> None:
    """ncurses 는 내용의 줄 수가 바뀌면 전부 다시 그리지 않고 **줄을 민다.**

    `CSI L`(삽입)·`CSI M`(삭제)를 모르면 격자가 조용히 어긋난다. 실제로 계정 화면(줄이
    많다)에서 쿠폰 화면(적다)으로 넘어갈 때 조작법 줄이 격자에서 사라졌고, **자식은
    정상적으로 그리고 있었다** — 계측을 넣어서야 알았다.

    하네스가 줄을 잃으면 진짜 결함도 못 잡는다. 그래서 하네스 자신을 잰다.
    """
    from terminal import render

    dropped, _ = render("a\r\nb\r\nc\r\n\x1b[2;1H\x1b[M", 10, 4)
    assert [ln.rstrip() for ln in dropped] == ["a", "c", "", ""]

    pushed, _ = render("a\r\nb\r\nc\r\n\x1b[2;1H\x1b[L", 10, 4)
    assert [ln.rstrip() for ln in pushed] == ["a", "", "b", "c"]


def test_pushing_lines_carries_their_attributes() -> None:
    """글자만 옮기고 속성을 두고 오면 색이 엉뚱한 줄에 남는다."""
    from terminal import render

    lines, attrs = render("\x1b[1mbold\x1b[m\r\nplain\r\n\x1b[1;1H\x1b[L", 10, 3)
    assert [ln.rstrip() for ln in lines] == ["", "bold", "plain"]
    assert "1" in attrs[1][0], attrs[1][0]
    assert "1" not in attrs[0][0], attrs[0][0]


@_needs_pty
def test_the_credits_screen_keeps_its_keys_line_after_the_switch(session: Session) -> None:
    """화면을 바꾼 **뒤에도** 조작법이 보여야 한다.

    나가는 법이 안 보이는 화면은 갇힌 화면이다. 이 줄이 사라졌던 것은 하네스 탓이었지만,
    같은 증상을 내는 제품 결함도 있을 수 있으므로 여기서 잠근다.
    """
    down = [b"\x1bOB"] * (2 + next(i for i, (a, _) in enumerate(tui.MENU) if a == "credits"))
    screen = session.run([*down, b"\n", b"q"], settle=1.0, total=60.0)
    assert screen.exit_code == 0
    assert "codex-swap · credits" in screen.text, screen.text
    assert "esc back" in screen.text, screen.text
    assert "q quit" in screen.text, screen.text
