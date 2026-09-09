"""진짜 터미널에 TUI 를 띄워 검증한다.

이 프로젝트에서 값비쌌던 결함은 전부 `render_screen` **바깥**에 있었다 — `_loop`·
`_paint`·`_prompt`. 넷을 세션 하나에서 밟았고, 그때 497 건이 전부 통과하고 있었다.
`pragma: no cover` 구간이라 단위 테스트가 닿지 않는다.

여기 있는 것은 그 넷에 대한 회귀 가드다. 각 테스트가 실제로 밟은 증상 하나를 지목한다.

하니스는 글자 격자와 **속성 격자**를 함께 복원한다. 색·굵기를 바이트 스트림에서 정규식으로
찾으면 curses 가 속성 구간을 어떻게 쪼개는지에 의존하게 되는데, 그 방식은 실제로 "행 전체
bold" 뮤테이션을 놓쳤다. 무엇이 어떤 속성으로 그려졌는지는 좌표로 물어야 한다.

복원이 맞다는 근거: 같은 상태에서 `render_screen` 이 만든 8 줄이 화면 격자와 **바이트
단위로 일치**한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from terminal import Session

pytestmark = pytest.mark.skipif(
    not hasattr(__import__("os"), "forkpty"), reason="pty 를 못 쓰는 플랫폼"
)


@pytest.fixture
def session(tmp_path: Path) -> Session:
    """계정 둘, 사용량은 캐시에 심어 둔 판. 프로브가 필요 없다."""
    s = Session(tmp_path / "home")
    s.slot("master", "a@example.com")
    s.slot("shared", "b@example.com")
    s.activate("a@example.com")
    s.cache("master", 58, credits=1)
    s.cache("shared", 70, credits=2)
    return s


def test_the_screen_is_up_and_q_quits(session: Session) -> None:
    screen = session.run([b"q"])
    assert screen.exit_code == 0
    assert "master" in screen.text and "shared" in screen.text
    assert "58%" in screen.row("master")


# ── 회귀 1: 자동 조회가 동기라 화면이 굳고 `q` 가 안 먹었다 ──────────────────


def test_q_works_while_a_probe_is_running(tmp_path: Path) -> None:
    """캐시가 비면 화면을 열자마자 조회가 돈다. 그 동안에도 나갈 수 있어야 한다.

    동기로 돌리던 때는 화면이 5.3 초 굳었고, 그 사이 누른 `q` 는 처리되지도 남지도
    않아 두 번 눌러야 나갈 수 있었다.
    """
    s = Session(tmp_path / "home", term="vt100")
    s.slot("master", "a@example.com")
    s.activate("a@example.com")
    s.slot("shared", "b@example.com")  # 캐시를 심지 않는다 → 자동 조회 대상

    screen = s.run([b"q"], settle=0.25, total=25.0)
    assert screen.exit_code == 0, "조회 중에 q 가 먹지 않았다"
    # 표가 먼저 떠 있어야 한다. 조회가 끝난 뒤에 그리면 그 동안 화면이 빈다.
    assert "LABEL" in screen.text


def test_the_probe_actually_runs_and_fills_the_screen(tmp_path: Path) -> None:
    """조회가 실제로 돌아 숫자가 채워지는지. 가짜 app-server 가 진짜 자식으로 뜬다."""
    s = Session(tmp_path / "home", term="vt100")
    s.slot("master", "a@example.com")
    s.activate("a@example.com")
    s.slot("shared", "b@example.com")

    screen = s.run([b"q"], settle=6.0, total=40.0)
    assert screen.exit_code == 0
    assert s.events.exists(), "프로브가 자식을 띄우지 않았다"
    assert "36%" in screen.text, screen.text  # ok.json 픽스처의 사용량


# ── 회귀 2: `_prompt` 가 반쪽만 읽어 `master` 가 `ster` 가 됐다 ──────────────


@pytest.mark.parametrize("term", ["xterm-256color", "vt100"])
def test_a_typed_name_arrives_whole_on_every_terminal(tmp_path: Path, term: str) -> None:
    """터미널마다 따로 확인한다 — 실패하는 조작이 터미널에 따라 다르기 때문이다.

    `_prompt` 는 커서 모양·에코·타임아웃을 차례로 건드린다. 이것들을 한 `suppress` 에
    몰아 넣으면 **앞의 실패가 뒤를 건너뛴다.** vt100 에는 `civis`(커서 숨김)가 없어
    `curs_set` 이 던지고, 그 바람에 다음 줄인 타임아웃 해제가 실행되지 않아 `getstr` 가
    논블로킹으로 남았다 — 한 글자도 못 받았다. xterm 에서는 성공해서 안 보였다.
    """
    s = Session(tmp_path / term, term=term)
    s.slot("master", "a@example.com")
    s.activate("a@example.com")
    s.cache("master", 58)
    screen = s.run([b"a", b".not-a-real-label\n", b"q"])
    assert screen.exit_code == 0
    assert ".not-a-real-label" in screen.text, screen.text


def test_a_typed_name_arrives_whole(session: Session) -> None:
    """루프의 논블로킹 타임아웃이 `getstr` 에 새면 앞 글자가 잘린다.

    실제로 `master` 를 넣었더니 `ster` 슬롯이 만들어졌다. 여기서는 규약상 거부되는
    이름을 넣어, **슬롯을 만들지 않고** 문자열이 온전히 도착했는지만 본다.
    """
    screen = session.run([b"a", b".not-a-real-label\n", b"q"])
    assert screen.exit_code == 0
    assert ".not-a-real-label" in screen.text, screen.text
    assert not (session.accounts / "ot-a-real-label").exists()


def test_a_typed_ladder_shows_up_on_the_policy_screen(session: Session) -> None:
    """`e` 직접 입력도 `_prompt` 를 쓴다 — `a` 와 같은 경로다.

    화면을 떠나지 않고 확인한다. `esc` 를 누르면 그 값은 화면에서 사라지므로, 마지막
    화면만 보면 "입력이 안 됐다" 와 "입력됐다가 취소됐다" 가 구별되지 않는다.
    """
    screen = session.run([b"p", b"e", b"33,66,88\n"])
    assert "codex-swap · policy" in screen.text
    assert "33,66,88" in screen.text, screen.text


def test_esc_leaves_the_policy_screen_without_saving(session: Session) -> None:
    screen = session.run([b"p", b"e", b"33,66,88\n", b"\x1b", b"q"])
    assert screen.exit_code == 0
    assert "Left without saving" in screen.text
    assert not (session.accounts / "config.json").exists(), "esc 인데 저장됐다"


def test_the_loop_still_breathes_after_a_prompt_closes(tmp_path: Path) -> None:
    """프롬프트를 닫은 뒤 **키를 더 누르지 않아도** 배경 조회 결과가 화면에 붙어야 한다.

    `_prompt` 는 읽는 동안 루프의 논블로킹 타임아웃을 끈다. 나올 때 되돌리지 못하면
    `getch` 가 무한정 막혀서, 조회가 끝나도 사용자가 아무 키나 누르기 전까지 화면이
    갱신되지 않는다 — 쿨다운 숫자도 같이 멈춘다. 들어가는 쪽 실패보다 조용하다.

    여기서는 프롬프트를 **취소**(빈 입력)하고, 그 뒤로 키를 하나도 보내지 않은 채
    결과 문구가 뜨는지만 본다.
    """
    s = Session(tmp_path / "home")
    s.slot("master", "a@example.com")
    s.activate("a@example.com")
    s.cache("master", 58)
    s.slot("shared", "b@example.com")  # 캐시 없음 → 조회 대상
    s.delay(2.0)

    screen = s.run([b"a", b"\n"], settle=0.8, total=40.0, wait_for="Usage refreshed")
    assert "Usage refreshed" in screen.text, (
        "프롬프트를 닫은 뒤 루프가 막혔다 — 조회 결과가 화면에 붙지 않는다"
    )


# ── 회귀 3: 조회 결과가 정책 화면을 덮어 편집이 날아갔다 ────────────────────


def test_a_finished_probe_does_not_eject_you_from_the_policy_screen(tmp_path: Path) -> None:
    """`build_view` 는 `mode` 를 기본값으로 되돌린다.

    정책 화면에서 값을 고치는 동안 배경 조회가 끝나면 화면이 통째로 튀어나가고 편집이
    사라졌다 — 그래서 `e` 로 넣은 사다리가 반영되지 않았다.
    """
    s = Session(tmp_path / "home")
    s.slot("master", "a@example.com")
    s.activate("a@example.com")
    s.cache("master", 58)  # 조회 대상을 하나로 줄여 대기 시간을 예측 가능하게
    s.slot("shared", "b@example.com")  # 캐시 없음 → 화면을 열면 조회가 돈다
    # 조회가 **편집 도중에** 끝나도록 늦춘다. 자연 속도에 맡기면 빠른 기기에서 조회가
    # 먼저 끝나 이 검사가 공허하게 통과한다.
    s.delay(2.0)

    # 조회가 **끝난 뒤**에 캡처해야 한다. 조용해졌다고 끊으면 아직 도는 중에 찍혀서
    # 검사가 아무것도 안 본 채 통과한다 — 실제로 그래서 뮤테이션을 놓쳤다.
    screen = s.run([b"p", b"e", b"33,66,88\n"], settle=1.0, total=60.0, wait_for="Usage refreshed")
    assert "Usage refreshed" in screen.text, "조회가 안 끝났다 — 검사가 성립하지 않는다"
    assert "codex-swap · policy" in screen.text, "정책 화면에서 튀어나갔다"
    assert "33,66,88" in screen.text, "미저장 편집이 날아갔다"


# ── 회귀 4: 행 전체 bold 가 같은 바를 활성 행에서만 길어 보이게 했다 ────────


def test_both_rows_draw_the_identical_bar(session: Session) -> None:
    """두 행의 바가 글자로도 **속성으로도** 같아야 한다.

    문자열은 원래도 같았다. 갈린 것은 활성 행에만 걸린 bold 였고, 이 터미널에서 bold
    글자가 더 넓게 그려져 "아래 바가 더 짧다" 로 읽혔다.
    """
    session.cache("master", 70, credits=1)  # 두 계정을 같은 사용량으로
    screen = session.run([b"q"])
    bars = [line[line.index("█") :].split("  ")[0] for line in screen.lines if "█" in line]
    assert len(bars) == 2, screen.text
    assert bars[0] == bars[1], bars


def test_the_bar_is_never_drawn_bold(session: Session) -> None:
    """강조는 라벨까지다. 바가 굵어지면 같은 문자열이 행마다 다른 길이로 보인다."""
    screen = session.run([b"q"])
    assert "1" not in screen.attrs_of("█"), "바를 bold 로 그렸다"
    assert "1" not in screen.attrs_of("▒")
    assert "1" in screen.attrs_of("master"), "활성 라벨 강조가 아예 없다"
    assert "1" not in screen.attrs_of("shared"), "비활성 라벨까지 강조했다"


def test_the_reset_time_is_not_cut_off(tmp_path: Path) -> None:
    """이 열을 보는 이유는 "언제 풀리나" 하나다. 그 답의 끝이 잘리면 자리만 차지한다.

    **`_RESET_COLS` 는 표시 언어를 따라간다.** 한국어이던 동안 상한은 23 이었는데
    (`09-07 21:00 (3시간 뒤)`) 상수가 20 이라, 하루 안쪽 리셋 — 가장 흔한 형태 — 이
    폭이 넉넉한 화면에서까지 **언제나** `(3시간 …` 으로 잘렸다. 영어로 옮기면서 상한이
    20 (`(in 23h)`)으로 내려가 상수도 다시 도출했다.

    그래서 이 테스트가 지키는 것은 특정 숫자가 아니라 **문구와 상수가 함께 움직인다**는
    것이다. 표시 문구를 고치면서 상수를 안 보면 같은 잘림이 되돌아온다.
    """
    s = Session(tmp_path / "home")
    s.slot("master", "a@example.com")
    s.activate("a@example.com")
    s.cache("master", 58, resets_in=3 * 3600 + 60)

    screen = s.run([b"q"], cols=120)
    row = screen.row("master")
    assert "(in 3h)" in row, row
    assert "…" not in row, f"리셋 시각이 잘렸다: {row}"


def test_the_cursor_glyph_is_coloured(session: Session) -> None:
    """enter 는 **커서 행**의 자격증명을 바꾼다. 그 표시가 안 보이면 확신할 수 없다."""
    screen = session.run([b"q"])
    assert "36" in screen.attrs_of(">"), "커서 표시에 색이 없다"


def test_auto_switching_being_off_is_not_whispered(tmp_path: Path) -> None:
    """꺼져 있는 것은 "왜 안 바뀌지" 의 첫 번째 원인이다. dim 으로 두면 안 읽힌다."""
    s = Session(tmp_path / "home")
    s.slot("master", "a@example.com")
    s.activate("a@example.com")
    s.cache("master", 58)
    (s.home / ".claude").mkdir(parents=True, exist_ok=True)
    (s.home / ".claude/.codex-rotate-off").touch()

    screen = s.run([b"q"])
    assert "Auto switch: off" in screen.text, screen.text
    assert "33" in screen.attrs_of("Auto switch: off"), "꺼짐이 켜짐과 같은 밝기다"


def test_only_the_key_glyphs_are_coloured(session: Session) -> None:
    """설명까지 강조하면 눈이 어디를 눌러야 하는지 못 찾고 줄 전체를 읽게 된다."""
    screen = session.run([b"q"])
    keys = screen.attrs_of("↑↓")
    assert "1" in keys and "36" in keys, keys
    assert "36" not in screen.attrs_of("move"), "설명까지 색을 입혔다"


# ── 색이 없는 터미널에서도 깨지지 않는다 ────────────────────────────────────


@pytest.mark.parametrize("term", ["vt100", "dumb"])
def test_a_colourless_terminal_still_works(tmp_path: Path, term: str) -> None:
    """색은 덧칠이지 유일한 신호가 아니다. 없으면 없는 대로 떠야 한다."""
    s = Session(tmp_path / term, term=term)
    s.slot("master", "a@example.com")
    s.activate("a@example.com")
    s.cache("master", 58)
    screen = s.run([b"q"])
    assert screen.exit_code == 0


# ── 회귀 5: 방향키로 들어가는 길 ────────────────────────────────────────────
#
# 키 배치를 바꿨다 — `enter` 는 "들어간다", 전환은 `s`. `_loop` 은 `pragma: no cover` 라
# 단위 테스트가 닿지 않으므로 여기서 실제 키를 눌러 본다.


def test_enter_no_longer_switches(session: Session) -> None:
    """가장 위험한 회귀다. `enter` 가 아직 전환하면 자격증명이 의도 없이 바뀐다."""
    screen = session.run([b"\n", b"q"])
    assert screen.exit_code == 0
    assert "Press s to switch" in screen.text, screen.text
    # 활성 계정이 그대로다 — `*` 가 master 줄에 있어야 한다.
    assert "*master" in screen.text.replace(" *master", "*master"), screen.row("master")


def test_s_switches(session: Session) -> None:
    screen = session.run([b"\x1bOB", b"s", b"q"])  # 아래로 한 칸 → shared → s
    assert screen.exit_code == 0
    assert "Switched to shared" in screen.text, screen.text


def test_arrowing_into_the_menu_and_entering_opens_the_policy_screen(session: Session) -> None:
    """계정을 지나 메뉴 첫 항목까지 내려가 `enter`."""
    down = [b"\x1bOB"] * 2  # 계정 2 개를 지나면 메뉴 첫 항목
    screen = session.run([*down, b"\n"])
    assert "codex-swap · policy" in screen.text, screen.text


def test_the_menu_quit_item_ends_the_session(session: Session) -> None:
    down = [b"\x1bOB"] * (2 + 4)  # 계정 2 + 메뉴 마지막(Quit) 까지
    screen = session.run([*down, b"\n"], total=25.0)
    assert screen.exit_code == 0


def test_s_on_the_menu_does_not_switch(session: Session) -> None:
    """커서가 메뉴에 있을 때 `s` 는 아무것도 바꾸지 않고 말해 준다."""
    screen = session.run([b"\x1bOB", b"\x1bOB", b"s", b"q"])
    assert screen.exit_code == 0
    assert "Move to an account first" in screen.text, screen.text
    assert "Switched to" not in screen.text, screen.text
