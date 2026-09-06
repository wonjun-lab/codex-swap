"""터미널 UI.

`codex-swap` 을 인자 없이 부르면(TTY 일 때) 여기로 온다. 계정 목록이 바로 보이고,
화살표로 고르고, 단축키로 전환·등록·정책 변경을 한다.

`curses` 는 표준 라이브러리다. 설계문은 Textual 을 예정했지만 이 화면에는 과하고, 이
패키지의 **런타임 의존 0** 이라는 제약을 지키는 편이 낫다.

두 가지 규율이 이 파일의 구조를 정한다.

1. `render_lines` 는 **순수 함수**다. 파일도 터미널도 만지지 않는다. 화면 내용에 대한
   테스트가 전부 그것을 부르고, 터미널을 띄우는 테스트는 CI 에서 돌지 않는다.
   그래서 디스크를 읽는 일은 전부 `build_view` 가 미리 해 `View` 에 담는다.
2. 폭 계산은 **표시 칸** 기준이다. 한글은 터미널에서 두 칸을 먹는데 `len()` 은 한
   글자로 센다. 문자 수로 자르면 한글이 섞인 줄이 화면 밖으로 넘친다.
"""

from __future__ import annotations

import contextlib
import curses
import io
import queue
import threading
import time
import unicodedata
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace

from codex_swap.core import cache, config, identity, paths, policy, probe, store
from codex_swap.core.discovery import resolve_codex_bin
from codex_swap.core.types import ProbeOutcome

__all__ = ["Row", "View", "build_view", "render_lines", "replace"]


@dataclass(frozen=True)
class Row:
    label: str
    email: str
    used: str
    reset: str
    active: bool

    known: bool = True
    """사용량을 하나라도 알고 있나. False 면 `used` 가 `?` 이고 자동 조회 대상이다.

    `stale` 과 다르다 — 낡은 값은 **알고 있는** 값이다."""

    stale: bool = False
    """TTL 이 지난 값을 보여 주는 중인가. `used` 앞에 `~` 가 붙는다."""

    percent: int | None = None
    """사용량 원값. `used` 는 `~47%` 같은 **표시 문자열**이라 바를 그릴 수 없다.

    표시와 계산을 한 필드로 겸하면 서식이 바뀔 때마다 파싱이 따라 깨진다."""


@dataclass(frozen=True)
class View:
    """화면에 필요한 것 전부. curses 도 파일시스템도 모른다."""

    rows: tuple[Row, ...]
    cursor: int
    settings: config.Settings
    message: str = ""
    mode: str = "accounts"
    """accounts | policy"""

    policy_cursor: int = 0

    auto_off: bool = False
    """자동 전환이 꺼져 있나. `render_lines` 가 순수하려면 이 값을 미리 읽어 둬야 한다."""

    active_email: str | None = None
    active_registered: bool = True
    """활성 계정이 어느 슬롯과도 안 맞으면 False. 그 상태에서 전환하면 sync-back 이
    건너뛰어져 지금 자격증명이 사라진다 — 화면이 그것을 알려야 한다."""

    saved_settings: config.Settings | None = None
    """정책 화면에 들어올 때의 디스크 값. 무엇이 편집됐는지 가리는 기준이다."""

    current_rung: int | None = None
    """지금 넘어야 하는 사다리 칸. 사다리 전체(50,70,85,95)보다 이 하나가 행동을 정한다.

    정책은 **가장 덜 쓴 계정**의 사용량 바로 위 칸을 관문으로 잡는다(`policy.rung_for`).
    활성 기준으로 잡으면 앞선 쪽만 계속 올라가 번갈아 밟기가 성립하지 않는다."""

    cooldown_left: int | None = None
    """쿨다운이 걸려 있으면 남은 초. 화면이 "왜 안 바뀌는가" 에 답하는 자리다."""


POLICY_FIELDS = (
    ("ladder", "사다리", "전환 관문. 가장 덜 쓴 계정의 사용량 바로 위 칸이 현재 관문이다"),
    ("margin", "마진(%p)", "대상이 이만큼 낮아야 전환한다. 동률 근처의 무의미한 교체를 막는다"),
    ("cooldown", "쿨다운(초)", "자동 전환 사이 최소 간격"),
    ("cache_ttl", "캐시(초)", "사용량 캐시 수명"),
    ("check_interval", "스로틀(초)", "판단 자체를 이 간격으로 묶는다"),
)

LADDER_PRESETS = ((50, 70, 85, 95), (70,), (50, 75), (25, 50, 75, 90), (90,))

# 조작법은 ASCII 로 적는다. `↑↓` 는 East Asian Ambiguous 라 터미널마다 한 칸으로도 두
# 칸으로도 그려져, 폭 계산이 맞아도 실제 화면이 어긋난다.
ACCOUNT_KEYS_FULL = "  ^v 이동   enter 전환   r 사용량   a 등록   p 정책   o 자동전환   q 종료"
ACCOUNT_KEYS_SHORT = "  ^v  enter 전환  r  a  p  o  q"
POLICY_KEYS_FULL = "  ^v 이동   <> 값 조정   s 저장   esc 취소   q 종료"
POLICY_KEYS_SHORT = "  ^v  <>  s 저장  esc  q"

_TICK_MS = 120
"""`getch` 타임아웃(ms). 배경 조회 결과가 화면에 반영되는 지연이기도 하다.

짧게 잡을수록 반응이 좋아 보이지만 그만큼 자주 깨어나 다시 그린다. 120 ms 는 사람이
멈춤으로 느끼지 않으면서(대략 100 ms 이하가 즉각으로 읽힌다) 초당 8 회 정도만 도는 값이다.
"""


# ── 폭 ───────────────────────────────────────────────────────────────────────


def _width(text: str) -> int:
    """터미널에서 차지하는 칸 수. 한글은 두 칸이다."""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def _clip(text: str, cols: int) -> str:
    """표시 폭 기준으로 자른다. 문자 수로 자르면 한글 줄이 화면 밖으로 넘친다."""
    if cols <= 0:
        return ""
    out, used = [], 0
    for ch in text:
        w = _width(ch)
        if used + w > cols:
            break
        out.append(ch)
        used += w
    return "".join(out)


def _cell(text: str, cols: int, *, ellipsis: bool = False) -> str:
    """잘라내고 채운다. 긴 라벨·이메일이 열을 밀어내지 못하게 한다.

    `ellipsis` 는 잘렸다는 것을 보이게 한다. 표시가 없으면 `account.name@gmail.co` 가
    실제 주소인지 잘린 것인지 구별되지 않는다 — 계정을 확인하려고 보는 칸에서 그건
    쓸모가 없다.
    """
    if not ellipsis or _width(text) <= cols or cols < 2:
        return _pad(_clip(text, cols), cols)
    return _pad(_clip(text, cols - 1) + "…", cols)


def _help_line(full: str, short: str, width: int | None) -> str:
    """좁은 터미널에서는 짧은 판으로 바꾼다.

    긴 줄을 그냥 자르면 뒤쪽 키가 통째로 사라진다 — 40 칸에서 실제로 `r` 에서 잘렸다.
    """
    return full if width is None or _width(full) <= width else short


# ── 상태 읽기 ────────────────────────────────────────────────────────────────


def build_view(
    settings: config.Settings,
    *,
    cursor: int = 0,
    select: str | None = None,
    message: str = "",
    saved_settings: config.Settings | None = None,
    carry: Mapping[str, Row] | None = None,
) -> View:
    """디스크에서 현재 상태를 읽어 화면 상태를 만든다. 프로브는 돌리지 않는다.

    `select` 를 주면 커서를 그 라벨에 맞춘다. 커서가 위치 인덱스뿐이면 목록이 바뀔 때
    엉뚱한 계정을 가리키게 되고, 그 상태에서 enter 를 누르면 의도하지 않은 전환이 된다.

    `carry` 는 **직전 화면의 행**이다. 디스크에서 값을 못 얻었을 때 마지막으로 알던
    숫자를 이어받는다. 이것이 필요한 이유는 `store.switch` 가 전환 직후 캐시를 파일째
    지우기 때문이다 — 그건 정책 쪽의 옳은 동작이지만, 화면까지 같이 비면 방금 전환한
    사용자가 두 계정 모두 `?` 인 표를 보게 된다. 사용량은 전환한다고 달라지지 않으므로
    직전 값을 그대로 보여 주는 것이 맞다.
    """
    from codex_swap.cli import _reset_text

    try:
        active = store.active_label(settings)
        labels = store.labels(settings)
        active_email = identity.email_of(store.active_auth(settings))
    except OSError as exc:
        return View(
            rows=(),
            cursor=0,
            settings=settings,
            message=f"슬롯을 읽지 못했다: {exc}",
            auto_off=False,
        )

    rows = []
    for label in labels:
        email = identity.email_of(store.slot_auth(settings, label)) or "?"
        # 셋을 순서대로 시도한다. 신선한 캐시 → 낡은 캐시(낡았다고 표시) → 직전 화면.
        # 물음표는 **정말로 한 번도 값을 얻지 못한 슬롯**에만 남고, 그건 `_loop` 이
        # 자동 조회로 채운다.
        entry = cache.read(settings, label)
        stale = False
        if entry is None:
            aged = cache.read_stale(settings, label)
            if aged is not None:
                entry, stale = aged[0], True
        percent: int | None = None
        if entry is not None and "usedPercent" in entry:
            percent = entry["usedPercent"] if isinstance(entry["usedPercent"], int) else None
            used = f"{'~' if stale else ''}{entry['usedPercent']}%"
            reset = _reset_text(entry.get("resetsAt"))
            known = True
        # 이어받기는 **같은 계정일 때만**이다. 라벨은 슬롯 이름일 뿐이라 지웠다가 다른
        # 계정으로 다시 등록할 수 있고, 그러면 새 이메일 옆에 옛 계정의 사용량이 붙는다.
        # `~` 는 값이 낡았다는 뜻이지 **다른 사람 것**이라는 뜻이 아니라, 그 화면은
        # 낡은 것보다 나쁘다 — 틀린 것을 맞다고 말한다.
        elif (
            carry is not None
            and (prev := carry.get(label)) is not None
            and prev.known
            and prev.email == email
        ):
            # 캐시가 사라진 자리. 전환 직후가 이 경로다.
            used, reset, known, stale = prev.used, prev.reset, True, True
            percent = prev.percent
            if not used.startswith("~"):
                used = f"~{used}"
        else:
            used, reset, known = "?", "-", False
        rows.append(
            Row(
                label=label,
                email=email,
                used=used,
                reset=reset,
                active=label == active,
                known=known,
                stale=stale,
                percent=percent,
            )
        )

    if select is not None:
        with contextlib.suppress(ValueError):
            cursor = [r.label for r in rows].index(select)

    return View(
        rows=tuple(rows),
        cursor=min(max(cursor, 0), max(len(rows) - 1, 0)),
        settings=settings,
        message=message,
        auto_off=settings.off_switch.exists(),
        active_email=active_email,
        active_registered=active is not None,
        saved_settings=saved_settings,
        current_rung=current_rung(settings, rows),
        cooldown_left=cooldown_left(settings),
    )


def current_rung(settings: config.Settings, rows: Sequence[Row]) -> int | None:
    """지금 넘어야 하는 칸. 아는 값이 하나도 없으면 None.

    기준은 **가장 덜 쓴 계정**이다. 활성 기준으로 잡으면 앞선 쪽만 계속 올라가 둘이
    번갈아 밟기가 성립하지 않는다 (`policy.rung_for`). 화면이 정책과 다른 숫자를 말하면
    사용자는 화면 쪽을 믿으므로, 계산을 흉내내지 않고 그 함수를 그대로 부른다.
    """
    known = [r.percent for r in rows if r.percent is not None]
    if not known:
        return None
    return policy.rung_for(settings.ladder, min(known))


def cooldown_left(settings: config.Settings) -> int | None:
    """쿨다운 잔여 초. 안 걸려 있으면 None.

    "왜 안 바뀌는가" 는 이 화면에서 가장 자주 나오는 질문인데, 지금까지 답할 자리가
    없었다. 쿨다운은 그 답 중 유일하게 **시간이 지나면 저절로 풀리는** 것이라, 남은
    시간을 보여 주는 것만으로 기다릴지 손으로 옮길지가 갈린다.
    """
    try:
        age = time.time() - paths.rotate_stamp_path(settings).stat().st_mtime
    except OSError:
        return None
    left = settings.cooldown - age
    return int(left) if left > 0 else None


# ── 그리기 ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Style:
    """한 줄에 입힐 표시 속성. curses 를 모른다.

    조각 단위가 아니라 **줄 단위**인 것이 의도다. 조각으로 가면 `render_lines` 가
    문자열을 돌려주지 못하게 되어 기존 화면 테스트가 전부 깨진다. 줄 단위로도 이 화면이
    말해야 하는 것(어느 계정이 관문을 넘었나, 어느 값이 낡았나)은 다 표현된다.
    """

    tone: str = "plain"
    """plain | dim | ok | warn | danger. 이름은 **의미**이지 색이 아니다 — 실제 색은
    `_paint` 한 곳에서 정하고, 색이 없는 터미널에서는 통째로 무시된다."""

    bold: bool = False


_PLAIN = Style()
_DIM = Style("dim")

BAR_COLS = 24
"""사용량 바의 칸 수. 0~100% 를 이만큼에 눌러 담는다.

24 는 4% 가 한 칸이라 사다리(50·70·85·95)가 서로 다른 칸에 떨어진다. 더 좁히면 85 와
95 가 같은 칸으로 뭉쳐 관문 표시가 뜻을 잃는다.
"""

_EMAIL_MIN = 20
"""이메일 칸의 하한. 이보다 좁아지면 바를 포기한다.

`account.name@gmail.com` 류에서 20 칸이면 로컬 파트가 온전히 남아 계정이 구별된다.
더 줄이면 `account.…` 이 되어 확인하려던 것을 확인하지 못한다.
"""

# 바가 있는 줄의 고정 소비: 커서·활성 표시(3) + 라벨(14) + 공백 + 공백 + 사용량(6) +
# 공백 + 바 + 공백 + 리셋(20). 이메일은 남는 자리를 받는다.
_RESET_COLS = 20


def _fixed_cols(*, with_bar: bool, with_reset: bool) -> int:
    """이메일을 뺀 고정 소비 폭. 커서·활성 표시(3) + 라벨(14) + 공백들 + 사용량(6)."""
    return (
        3
        + 14
        + 1
        + 1
        + 6
        + (1 + _RESET_COLS if with_reset else 0)
        + (BAR_COLS + 1 if with_bar else 0)
    )


_BAR_MIN_WIDTH = _fixed_cols(with_bar=True, with_reset=True) + _EMAIL_MIN
"""바를 그리기 시작하는 터미널 폭. 이보다 좁으면 바와 축이 통째로 빠진다.

좁은 화면에서 바를 억지로 넣으면 이메일이나 리셋 시각이 잘려 나간다. 둘 다 바보다
먼저 필요한 정보다 — 어느 계정인지, 언제 풀리는지. 그래서 이 값은 손으로 고른 숫자가
아니라 **다른 칸을 다 지키고 남는 자리**에서 나온다.
"""


def _layout(width: int | None) -> tuple[bool, bool, int]:
    """폭에 따라 무엇을 보여줄지 정한다. `(바, 리셋 시각, 이메일 칸)`.

    버리는 순서가 곧 우선순위다 — **바 → 리셋 시각 → 이메일 폭**. 바는 있으면 좋은
    것이고, 리셋 시각은 이메일보다 나중에 필요하며, 어느 계정인지는 끝까지 남아야 한다.

    넘치는 줄을 그리기 단계의 클립에 맡기면 안 된다. 그러면 마지막 열이 반쯤 잘린 채
    남아 화면이 고장 난 것처럼 보인다 — 열을 통째로 빼는 편이 정직하다.
    """
    if width is None:
        return True, True, 30
    for with_bar, with_reset in ((True, True), (False, True), (False, False)):
        room = width - _fixed_cols(with_bar=with_bar, with_reset=with_reset)
        if room >= _EMAIL_MIN:
            return with_bar, with_reset, min(30, room)
    # 여기까지 오면 이메일도 온전히 못 담는다. 남는 자리를 그대로 준다(0 이면 열이 사라진다).
    room = width - _fixed_cols(with_bar=False, with_reset=False)
    return False, False, max(0, room)


def _bar_cell(pct: float) -> int:
    """사용량이 몇 번째 칸에 떨어지나. 축과 바가 **같은 함수**를 써야 눈금이 어긋나지 않는다."""
    return min(max(round(pct * BAR_COLS / 100), 0), BAR_COLS)


def usage_bar(percent: int | None, rung: int | None) -> str:
    """사용량 바. 현재 관문 **하나만** 눈금으로 찍는다.

    사다리 네 칸을 전부 각 행에 찍으면 `░┆░░┃░░┆░░┆` 처럼 잡음이 된다. 행마다 다른 것은
    사용량뿐이고 관문은 모든 행에 같으므로, 전체 눈금은 아래 공용 축에 한 번만 그린다.
    여기 남기는 하나는 **지금 넘어야 하는 칸**이라 행마다 읽을 값이 있다.
    """
    if percent is None:
        return " " * BAR_COLS
    filled = _bar_cell(percent)
    tick = _bar_cell(rung) if rung is not None else None
    # 눈금이 넘어섰는지는 셀 인덱스가 아니라 **정책 술어**로 정한다. 정책은
    # `active_pct >= rung` 에서 전환하는데, 셀로 판정하면 정확히 관문 위(70% · 70)에서
    # `filled == tick` 이라 "아직 안 넘음" 으로 그려져 화면이 정책과 다른 말을 한다.
    crossed = rung is not None and percent >= rung
    out = []
    for i in range(BAR_COLS):
        if i == tick:
            out.append("╪" if crossed else "┆")
        else:
            out.append("█" if i < filled else "░")
    return "".join(out)


def ladder_axis(ladder: Sequence[int], rung: int | None) -> tuple[str, str]:
    """바 아래에 한 번만 그리는 공용 눈금과 숫자. `(축, 숫자줄)`.

    현재 관문은 `┻` 로 갈라 둔다. 네 칸이 다 같아 보이면 "지금 어디를 넘어야 하나" 가
    다시 안 보인다 — 그게 이 축을 그리는 이유의 절반이다.
    """
    axis = [" "] * BAR_COLS
    labels = [" "] * BAR_COLS
    for step in ladder:
        i = min(_bar_cell(step), BAR_COLS - 1)
        axis[i] = "┻" if step == rung else "┴"
        text = str(step)
        start = max(i - 1, 0)
        for k, ch in enumerate(text):
            if start + k < BAR_COLS:
                labels[start + k] = ch
    return "".join(axis), "".join(labels)


def _row_tone(row: Row, view: View) -> str:
    """행의 색. 임의 구간이 아니라 **사다리**에 묶는다.

    50/80/90 같은 관습적 구간을 쓰면 색이 이 도구의 판단과 무관한 말을 하게 된다. 여기서
    색이 뜻해야 하는 것은 하나다 — 이 계정이 지금 전환 대상인가.
    """
    if row.percent is None:
        return "dim"
    ladder = view.settings.ladder
    if ladder and row.percent >= ladder[-1]:
        # 사다리 끝. 더 올라갈 칸이 없다는 뜻이라 다른 계정도 대개 같은 처지다.
        return "danger"
    gate = view.current_rung if view.current_rung is not None else (ladder[0] if ladder else None)
    if gate is not None and row.percent >= gate:
        return "warn"
    return "ok"


def _headline(view: View, show_ladder: bool) -> str:
    """머리말. 사다리 전체보다 **지금 넘어야 하는 칸**이 행동을 정한다.

    `show_ladder` 는 바가 빠졌을 때다. 그때는 축도 없으므로 사다리 전체를 여기 적지
    않으면 화면 어디에도 남지 않는다.
    """
    s = view.settings
    parts = []
    if show_ladder:
        parts.append(f"사다리 {','.join(map(str, s.ladder))}")
    elif view.current_rung is not None:
        parts.append(f"현재 관문 {view.current_rung}%")
    else:
        parts.append(f"사다리 {','.join(map(str, s.ladder))}")
    parts.append(f"마진 {s.margin}%p")
    if view.cooldown_left is not None:
        parts.append(f"쿨다운 {_duration(view.cooldown_left)} 남음")
    return "codex-swap    " + " · ".join(parts)


def _duration(seconds: int) -> str:
    """사람이 읽는 길이. 초 단위로 흐르는 숫자는 화면에서 잡음이다."""
    if seconds >= 3600:
        return f"{seconds // 3600}시간 {seconds % 3600 // 60}분"
    if seconds >= 60:
        return f"{seconds // 60}분"
    return f"{seconds}초"


def render_lines(view: View, *, height: int | None = None, width: int | None = None) -> list[str]:
    """화면 내용. 순수 함수라 터미널 없이 테스트한다.

    `render_screen` 의 얇은 껍질이다 — 두 함수를 따로 만들면 줄 수가 어긋날 수 있다.
    """
    return [text for text, _ in render_screen(view, height=height, width=width)]


def render_screen(
    view: View, *, height: int | None = None, width: int | None = None
) -> list[tuple[str, Style]]:
    """화면 내용과 줄별 속성. 순수 함수다 — 파일도 터미널도 만지지 않는다."""
    if view.mode == "policy":
        return [(line, _PLAIN) for line in _render_policy(view, height=height, width=width)]

    s = view.settings
    # 바는 자리가 남을 때만 그린다. 억지로 넣으면 이메일·리셋 시각이 잘리는데, 둘 다
    # 바보다 먼저 필요한 정보다.
    with_bar, with_reset, email_cols = _layout(width)
    head = [(_headline(view, show_ladder=not with_bar), _PLAIN), ("", _PLAIN)]

    # 꼬리말은 **버릴 수 있는 순서**로 쌓는다. 화면이 짧으면 앞쪽부터 버리고, 메시지는
    # 마지막까지 남긴다 — 실패를 알리는 유일한 줄이라 그것을 잃으면 사용자는 아무것도
    # 안 일어난 줄 안다.
    droppable = [
        "  자동 전환: 꺼짐   (o 로 켜기)" if view.auto_off else "  자동 전환: 켜짐   (o 로 끄기)",
        _help_line(ACCOUNT_KEYS_FULL, ACCOUNT_KEYS_SHORT, width),
    ]
    # `~` 는 낡은 값이라는 표시다. 범례가 없으면 사용자는 그 기호를 오류로 읽는다.
    # 낡은 행이 하나도 없으면 넣지 않는다 — 늘 떠 있는 안내는 곧 안 읽힌다.
    if any(r.stale for r in view.rows):
        droppable.insert(0, "  ~ 는 캐시가 낡았다는 표시다 (r 로 새로 읽는다)")
    # 활성 계정이 어느 슬롯과도 안 맞으면 전환이 지금 자격증명을 버린다. 조용히 두면
    # 사용자는 enter 한 번으로 그것을 잃는다.
    keep: list[tuple[str, Style]] = []
    if view.rows and not view.active_registered:
        who = view.active_email or "알 수 없는 계정"
        keep.append(
            (
                f"  주의: 활성({who})이 슬롯에 없다. 전환하면 이 자격증명은 보관되지 않는다",
                Style("warn"),
            )
        )
    if view.message:
        keep.append((f"  {view.message}", _PLAIN))

    if not view.rows:
        # 빈 화면에서도 메시지가 보여야 한다 — 등록 실패가 여기서 나온다. 다만 조작법은
        # 이 화면에 실제로 있는 키만 적는다.
        empty = [
            ("  등록된 계정이 없다.", _PLAIN),
            ("", _PLAIN),
            ("  a  지금 로그인된 계정을 슬롯에 등록   q  종료", _DIM),
        ]
        if view.message:
            empty += [("", _PLAIN), (f"  {view.message}", _PLAIN)]
        return head + empty

    columns = f"   {_cell('LABEL', 14)} {_cell('EMAIL', email_cols)} {_cell('USED', 6)}"
    if with_bar:
        columns += f" {_cell('', BAR_COLS)}"
    if with_reset:
        columns += " RESET"
    header = [*head, (columns.rstrip() if not with_reset else columns, _DIM)]

    # ── 뷰포트 ──
    # 목록이 화면보다 길면 조작법이 먼저 밀려난다 — 계정 20 개 · 24 행에서 실제로 그랬다.
    # 머리말과 지켜야 할 꼬리말 자리를 먼저 떼고, 남는 만큼만 목록에 준다.
    rows = list(view.rows)
    start = 0
    axis_lines: list[tuple[str, Style]] = []
    if with_bar and any(r.percent is not None for r in view.rows):
        axis, labels = ladder_axis(s.ladder, view.current_rung)
        pad = f"   {' ' * 14} {' ' * email_cols} {' ' * 6} "
        axis_lines = [(f"{pad}{axis}", _DIM), (f"{pad}{labels}   ┻ = 현재 관문", _DIM)]

    tail_keep = [("", _PLAIN), *keep] if keep else []

    if height is None:
        tail = [("", _PLAIN), *[(x, _DIM) for x in droppable[::-1]], *keep]
    else:
        # 최소 구성: 머리말 + 커서 행 1 + 지켜야 할 꼬리말. 남는 자리에 버릴 수 있는
        # 줄을 중요한 것(조작법)부터 채워 넣는다.
        room = height - len(header) - 1 - len(tail_keep)
        shown = []
        for line in reversed(droppable):  # 조작법 → 자동전환 순
            if len(shown) + 1 <= max(room - 1, 0):  # 빈 줄 하나 몫을 남긴다
                shown.append((line, _DIM))
        tail = ([("", _PLAIN), *shown] if shown else []) + tail_keep

        budget = height - len(header) - len(tail) - len(axis_lines)
        # 스크롤 표시가 붙을 수 있으므로 두 줄을 미리 뗀다.
        if len(rows) > budget:
            budget = max(budget - 2, 1)
            start = min(max(0, view.cursor - budget // 2), len(rows) - budget)
            rows = rows[start : start + budget]

    hidden_above = start
    hidden_below = len(view.rows) - (start + len(rows))

    body: list[tuple[str, Style]] = []
    if hidden_above:
        body.append((f"   ^ {hidden_above}개 더", _DIM))
    for i, row in enumerate(rows, start=start):
        cursor = ">" if i == view.cursor else " "
        mark = "*" if row.active else " "
        line = (
            f" {cursor}{mark}{_cell(row.label, 14, ellipsis=True)} "
            f"{_cell(row.email, email_cols, ellipsis=True)} {_cell(row.used, 6)}"
        )
        if with_bar:
            line += f" {usage_bar(row.percent, view.current_rung)}"
        if with_reset:
            line += f" {_cell(row.reset, _RESET_COLS, ellipsis=True)}".rstrip()
        body.append((line, Style(_row_tone(row, view), bold=row.active)))
    if hidden_below:
        body.append((f"   v {hidden_below}개 더", _DIM))
    body += axis_lines

    # 최종 클램프. 아주 짧은 화면에서는 스크롤 표시까지 합한 바닥(머리말 3 + 표시 2 +
    # 행 1 + 메시지 2 = 8)이 화면보다 클 수 있다. 그때는 **본문**을 자른다 — 꼬리말을
    # 자르면 그리기 단계가 밑에서부터 잘라내므로 메시지가 먼저 사라진다.
    if height is not None:
        over = len(header) + len(body) + len(tail) - height
        if over > 0:
            keep_n = max(len(body) - over, 1)
            # 커서가 있는 줄을 남긴다. 표시줄이 먼저 밀려나는 것이 자연스럽다.
            cursor_at = next(
                (i for i, (ln, _) in enumerate(body) if ln.startswith(" >")), len(body) // 2
            )
            lo = min(max(0, cursor_at - keep_n // 2), max(0, len(body) - keep_n))
            body = body[lo : lo + keep_n]

        # 그래도 넘치면(머리말 3 + 행 1 + 메시지 2 = 6 이 바닥) 머리말을 앞에서 줄인다.
        # 제목과 빈 줄보다 "무엇이 잘못됐나" 가 먼저다.
        while len(header) + len(body) + len(tail) > height and len(header) > 1:
            header = header[1:]
        # 마지막 한 줄은 꼬리말의 빈 줄에서 뺀다.
        if len(header) + len(body) + len(tail) > height and tail and tail[0][0] == "":
            tail = tail[1:]

    return header + body + tail


def _render_policy(view: View, *, height: int | None = None, width: int | None = None) -> list[str]:
    s = view.settings
    saved = view.saved_settings
    out = ["codex-swap · 정책", ""]
    for i, (key, title, why) in enumerate(POLICY_FIELDS):
        cursor = ">" if i == view.policy_cursor else " "
        value = ",".join(map(str, s.ladder)) if key == "ladder" else getattr(s, key)
        # 저장 안 된 편집을 표시한다. 아니면 화면의 숫자가 이미 반영된 것처럼 보인다.
        edited = " *" if saved is not None and getattr(s, key) != getattr(saved, key) else ""
        out.append(f" {cursor} {_pad(title, 14)} {value}{edited}")
        if i == view.policy_cursor:
            out.append(f"     {why}")
    out += [
        "",
        _help_line(POLICY_KEYS_FULL, POLICY_KEYS_SHORT, width),
        "  s 를 누르면 저장되고, 이후 자동 전환이 이 값을 따른다",
        f"  저장 위치: {s.accounts_dir / config.CONFIG_NAME}",
    ]
    if view.message:
        out += ["", f"  {view.message}"]
    if height is not None and len(out) > height:
        # 메시지가 있으면 그것부터 지킨다.
        keep = out[-2:] if view.message else []
        out = out[: max(height - len(keep), 1)] + keep
    return out


# ── 동작 ─────────────────────────────────────────────────────────────────────


def _carry(view: View) -> dict[str, Row]:
    """직전 화면의 행. 디스크에서 값을 못 얻은 자리를 이것으로 메운다.

    상태를 바꾼 뒤 화면을 다시 만드는 모든 경로가 이걸 넘겨야 한다. 한 곳이라도 빠지면
    그 동작만 표를 물음표로 되돌려, 사용자에게는 특정 키를 누르면 사용량이 사라지는
    것으로 보인다.
    """
    return {r.label: r for r in view.rows}


def do_switch(view: View) -> View:
    """선택한 계정으로 전환한다.

    캐시된 행이 아니라 **디스크**에서 활성을 다시 읽는다. 배경에서 `rotate` 가 돌면
    화면의 `*` 가 낡아, 그것을 믿으면 멀쩡한 전환을 "이미 활성" 이라며 거부한다.
    """
    if not view.rows:
        return replace(view, message="등록된 계정이 없다")
    target = view.rows[view.cursor]
    try:
        current = store.active_label(view.settings)
    except OSError as exc:
        return replace(view, message=f"상태를 읽지 못했다: {exc}")
    if target.label == current:
        # 아무것도 하지 않은 분기인데도 `carry` 가 필요하다. 전환이 캐시를 비운 직후
        # 같은 행에서 enter 를 한 번 더 누르는 것이 흔한 조작인데, 여기서 이어받지
        # 않으면 화면이 방금 지켜 낸 숫자를 도로 물음표로 되돌린다.
        return build_view(
            view.settings,
            select=target.label,
            message=f"{target.label} 은 이미 활성이다",
            carry=_carry(view),
        )
    try:
        with store.switch_lock(view.settings):
            store.switch(view.settings, target.label, "manual (tui)")
    except store.LockBusy:
        return build_view(
            view.settings,
            select=target.label,
            message="다른 전환이 진행 중이다. 잠시 뒤 다시 눌러라",
            carry=_carry(view),
        )
    except (store.LockUnusable, store.StoreError) as exc:
        return build_view(view.settings, select=target.label, message=str(exc), carry=_carry(view))
    except Exception as exc:
        return build_view(
            view.settings, select=target.label, message=f"전환 실패: {exc}", carry=_carry(view)
        )
    return build_view(
        view.settings, select=target.label, message=f"전환했다: {target.label}", carry=_carry(view)
    )


def do_adopt(view: View, label: str | None) -> View:
    """지금 로그인된 계정을 슬롯에 등록한다.

    **다른 계정의 슬롯을 덮어쓰지 않는다.** `adopt` 는 활성 자격증명을 그 이름 위에 그냥
    복사하므로, 기존 이름을 입력하면 그 계정의 보관본이 사라진다 — 되돌릴 방법이 없다.
    같은 계정이면 갱신이므로 허용한다(썩은 사본을 새로 뜨는 정상 용법이다).
    """
    if not label:
        return replace(view, message="")
    if not store.label_syntax_ok(label):
        return replace(view, message=f"쓸 수 없는 라벨이다: {label}")

    existing = store.slot_auth(view.settings, label)
    if existing.exists():
        live_email = identity.email_of(store.active_auth(view.settings))
        slot_email = identity.email_of(existing)
        if slot_email is not None and slot_email != live_email:
            return replace(
                view,
                message=f"'{label}' 에는 이미 {slot_email} 이 있다. 덮어쓰지 않는다",
            )

    from codex_swap import cli

    try:
        # cmd_adopt 는 stdout 에 찍는다. curses 가 화면을 쥔 상태에서 그 쓰기가 나가면
        # ncurses 의 화면 모델이 어긋나 이후 세션 내내 화면이 깨진다 — 삼켜 버린다.
        with contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_adopt(view.settings, label)
    except Exception as exc:
        return replace(view, message=f"등록 실패: {exc}")
    return build_view(view.settings, select=label, message=f"등록했다: {label}", carry=_carry(view))


def _probe_into_cache(s: config.Settings, label: str, active: str | None, codex_bin: str) -> bool:
    """한 슬롯을 프로브해 캐시에 얹는다. 성공이면 True.

    활성 라벨만 홈이 다르다 — 자격증명이 실제로 `~/.codex` 에 있으므로 슬롯 경로로
    프로브하면 보관본(대개 더 낡은 토큰)을 읽는다.
    """
    home = s.default_home if label == active else store.slot_dir(s, label)
    try:
        result = probe.probe(codex_bin, str(home))
    except Exception:
        return False
    if result.outcome is not ProbeOutcome.OK or result.usage is None:
        return False
    u = result.usage
    cache.write(
        s,
        label,
        {
            "email": u.email,
            "planType": u.plan_type,
            "usedPercent": u.used_percent,
            "primaryPercent": u.primary_percent,
            "secondaryPercent": u.secondary_percent,
            "resetsAt": u.resets_at,
            "reached": u.reached,
        },
    )
    return True


def do_refresh(view: View, labels: tuple[str, ...] | None = None) -> View:
    """슬롯을 프로브해 캐시를 채운다. 네트워크를 타므로 아무 데서나 부르지 않는다.

    `labels` 를 주면 그것만 조회한다. 화면을 열 때 **값을 하나도 모르는 슬롯만** 채우는
    자동 조회가 이 경로를 쓴다 — 전체 조회는 슬롯당 최대 20 초라 열 때마다 물릴 비용이
    아니지만, 물음표만 채우는 것은 대개 한 번뿐이고 그마저 없으면 화면이 빈다.
    """
    s = view.settings
    try:
        codex_bin = str(resolve_codex_bin())
    except Exception as exc:
        return replace(view, message=f"codex 를 찾지 못했다: {exc}")
    try:
        active = store.active_label(s)
        targets = labels if labels is not None else tuple(store.labels(s))
    except OSError as exc:
        return replace(view, message=f"슬롯을 읽지 못했다: {exc}")

    failed = [lb for lb in targets if not _probe_into_cache(s, lb, active, codex_bin)]
    if not failed:
        msg = "사용량을 새로 읽었다"
    elif labels is not None:
        # 자동 조회의 실패는 사용자가 시킨 일이 아니다. 무엇이 비어 있는지만 알린다.
        msg = f"사용량을 읽지 못했다: {', '.join(failed)} (r 로 다시 시도)"
    else:
        msg = f"조회 실패: {', '.join(failed)}"
    select = view.rows[view.cursor].label if view.rows else None
    return build_view(s, select=select, message=msg, carry=_carry(view))


def auto_probe_targets(view: View, attempted: Collection[str] = ()) -> tuple[str, ...]:
    """자동으로 조회할 슬롯. 값을 하나도 모르고, **이번 세션에서 아직 안 시도한** 것.

    `attempted` 가 없으면 프로브가 계속 실패하는 슬롯 하나가 화면을 인질로 잡는다.
    실패해도 `known` 은 여전히 False 라, 화면을 열 때마다·enter 를 누를 때마다 그 슬롯을
    다시 조회한다. 그 동안 키 입력은 처리되지 않으므로 `q` 로 나가지도 못한다.

    한 번 시도했으면 그것으로 족하다. 다시 읽는 것은 `r` 이 있고, 그건 사용자가 시킨
    일이라 기다림도 그의 선택이다.
    """
    seen = set(attempted)
    return tuple(r.label for r in view.rows if not r.known and r.label not in seen)


def do_toggle_auto(view: View) -> View:
    """자동 전환 on/off. off-switch 파일 하나가 그 스위치다 — bash 와 같은 파일이다."""
    sw = view.settings.off_switch
    select = view.rows[view.cursor].label if view.rows else None
    try:
        if sw.exists():
            sw.unlink()
            msg = "자동 전환을 켰다"
        else:
            sw.parent.mkdir(parents=True, exist_ok=True)
            sw.touch()
            msg = "자동 전환을 껐다"
    except OSError as exc:
        return replace(view, message=f"스위치를 바꾸지 못했다: {exc}")
    return build_view(view.settings, select=select, message=msg, carry=_carry(view))


def adjust_policy(view: View, delta: int) -> View:
    """정책 값 하나를 올리거나 내린다.

    사다리는 프리셋을 돈다. 화살표로 임의의 목록을 편집하게 만들면 조작이 번거롭고,
    사다리는 실제로 몇 가지 모양 중 하나를 고르는 값이다. 다만 **현재 값이 프리셋에
    없으면 그것을 고리에 넣는다** — 안 그러면 한 번 움직이는 순간 돌아올 수 없다.
    """
    key = POLICY_FIELDS[view.policy_cursor][0]
    s = view.settings
    if key == "ladder":
        # 고리의 앵커는 **디스크 값**이다. 현재 값에서 매번 고리를 다시 만들면, 프리셋에
        # 없는 사다리를 쓰던 사람이 한 번 움직이는 순간 그 값이 고리에서 빠져 영영 돌아올
        # 수 없다. 저장된 값을 항상 고리에 넣어 되돌아올 자리를 남긴다.
        anchor = (view.saved_settings or s).ladder
        ring = LADDER_PRESETS if anchor in LADDER_PRESETS else (anchor, *LADDER_PRESETS)
        idx = ring.index(s.ladder) if s.ladder in ring else 0
        return replace(
            view, settings=replace(s, ladder=ring[(idx + delta) % len(ring)]), message=""
        )

    step = {"margin": 1, "cooldown": 60, "cache_ttl": 60, "check_interval": 10}[key]
    value = max(0, getattr(s, key) + delta * step)
    return replace(view, settings=replace(s, **{key: value}), message="")


def save_policy(view: View) -> View:
    s = view.settings
    try:
        path = config.save_policy(
            s.accounts_dir,
            ladder=list(s.ladder),
            margin=s.margin,
            cooldown=s.cooldown,
            cache_ttl=s.cache_ttl,
            check_interval=s.check_interval,
        )
    except OSError as exc:
        return replace(view, message=f"저장 실패: {exc}")

    # 환경변수가 파일을 이긴다(설계상). 저장했는데 안 먹는 값이 있으면 말해 준다 —
    # 아무 말 없이 "저장했다" 만 띄우면 사용자는 반영된 줄 안다.
    fresh = config.load()
    shadowed = [title for key, title, _ in POLICY_FIELDS if getattr(fresh, key) != getattr(s, key)]
    msg = f"저장했다: {path}"
    if shadowed:
        msg += f" (환경변수가 이깁니다: {', '.join(shadowed)})"
    return replace(view, saved_settings=s, message=msg)


# ── curses 루프 ──────────────────────────────────────────────────────────────


_TONE_COLORS = {"ok": 1, "warn": 2, "danger": 3}
"""tone → color pair 번호. 0 은 curses 가 예약한 기본 쌍이라 1 부터 쓴다."""


def _init_colors() -> bool:  # pragma: no cover - 터미널 필요
    """색을 쓸 수 있으면 쌍을 등록하고 True.

    `use_default_colors` 로 배경을 -1 로 둔다. 검정으로 칠하면 밝은 테마 터미널에서
    글자만 남기고 배경이 뒤집혀 읽기 어려워진다.
    """
    if not curses.has_colors():
        return False
    with contextlib.suppress(curses.error):
        curses.start_color()
        with contextlib.suppress(curses.error):
            curses.use_default_colors()
        curses.init_pair(_TONE_COLORS["ok"], curses.COLOR_GREEN, -1)
        curses.init_pair(_TONE_COLORS["warn"], curses.COLOR_YELLOW, -1)
        curses.init_pair(_TONE_COLORS["danger"], curses.COLOR_RED, -1)
        return True
    return False


def _attr_of(style: Style, colored: bool) -> int:  # pragma: no cover - curses 상수
    """`Style` → curses 속성. 색이 없으면 굵기·흐림만 남는다.

    색을 못 쓰는 터미널에서도 화면이 **똑같이 읽혀야** 한다. 그래서 색은 덧칠이지
    유일한 신호가 아니다 — 활성은 `*`, 낡은 값은 `~`, 관문은 `┻` 로 이미 구별된다.
    """
    attr = curses.A_BOLD if style.bold else 0
    if style.tone == "dim":
        return attr | curses.A_DIM
    if colored and style.tone in _TONE_COLORS:
        return attr | curses.color_pair(_TONE_COLORS[style.tone])
    return attr


def _paint(stdscr, view: View, colored: bool = False) -> None:  # pragma: no cover - 터미널 필요
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    if height < 2 or width < 2:
        stdscr.refresh()
        return
    # 루프는 마지막 행 마지막 칸에 쓰면 curses 가 에러를 내므로 한 줄을 비워 둔다.
    for i, (line, style) in enumerate(render_screen(view, height=height - 1, width=width - 1)):
        if i >= height - 1:
            break
        with contextlib.suppress(curses.error):
            stdscr.addnstr(
                i, 0, _clip(line, width - 1), max(width - 1, 0), _attr_of(style, colored)
            )
    stdscr.refresh()


def _prompt(stdscr, label: str) -> str | None:  # pragma: no cover - 터미널 필요
    """한 줄 입력. 취소하거나 비면 None."""
    height, width = stdscr.getmaxyx()
    with contextlib.suppress(curses.error):
        curses.echo()
        curses.curs_set(1)
    try:
        stdscr.addnstr(height - 1, 0, _clip(label, width - 1), max(width - 1, 0))
        stdscr.clrtoeol()
        raw = stdscr.getstr(height - 1, min(_width(label), max(width - 1, 0)), 64)
    except (curses.error, KeyboardInterrupt):
        return None
    finally:
        with contextlib.suppress(curses.error):
            curses.noecho()
            curses.curs_set(0)
        stdscr.clearok(True)
    return raw.decode("utf-8", "replace").strip() or None


class _Prober:
    """사용량 조회를 별도 스레드에서 돌린다.

    이 클래스가 존재하는 이유는 순전히 **입력 응답성** 때문이다. 동기로 돌렸더니 화면을
    여는 데 5.3 초가 걸렸고(실측, 슬롯 2 개), 그 동안 curses 가 키를 읽지 않아 `q` 조차
    먹지 않았다. 게다가 끝난 뒤 `flushinp()` 가 그 사이 눌린 키를 버려서, 사용자는 두 번
    눌러야 나갈 수 있었다. 프로브 타임아웃이 **요청당** 20 초라 최악은 슬롯당 1 분에
    가까우므로 그냥 두면 안 되는 종류의 멈춤이다.

    **curses 를 만지지 않는다.** 스레드는 디스크와 네트워크만 다루고(프로브 → 캐시 쓰기)
    결과는 문자열 하나로 큐에 넘긴다. 화면을 다시 만드는 것은 주 스레드가 디스크에서
    한다 — 그래야 조회 중에 일어난 전환·등록이 결과에 덮이지 않는다.
    """

    def __init__(self) -> None:
        self._queue: queue.Queue[str] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._labels: tuple[str, ...] = ()

    @property
    def labels(self) -> tuple[str, ...]:
        """지금 조회 중인 라벨. 비어 있으면 노는 중이다."""
        return self._labels

    def start(self, settings: config.Settings, labels: Sequence[str]) -> bool:
        """조회를 시작한다. 이미 돌고 있거나 대상이 없으면 False.

        겹쳐 띄우지 않는 것은 같은 슬롯을 두 번 읽지 않기 위해서다. 놓친 대상은 다음
        틱에 `auto_probe_targets` 가 다시 집어 준다.
        """
        if self._thread is not None or not labels:
            return False
        self._labels = tuple(labels)
        self._thread = threading.Thread(
            target=self._run, args=(settings, self._labels), daemon=True
        )
        self._thread.start()
        return True

    def take(self) -> str | None:
        """끝났으면 결과 메시지를, 아직이면 None.

        큐를 먼저 보고 스레드를 정리한다. 반대로 하면 스레드가 막 끝났는데 결과를 한 틱
        늦게 집는다.
        """
        try:
            message = self._queue.get_nowait()
        except queue.Empty:
            return None
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._thread = None
        self._labels = ()
        return message

    def _run(self, settings: config.Settings, labels: tuple[str, ...]) -> None:
        # 이 스레드에서 나가는 예외는 아무도 못 본다. 무엇이 됐든 메시지 하나는 반드시
        # 큐에 넣어야 `take` 가 영영 None 을 돌려주고 화면이 "조회 중" 에 굳는 일이 없다.
        try:
            self._queue.put(_refresh_message(settings, labels))
        # 넓게 잡는다. 이 스레드에서 나가는 예외는 아무도 못 보고, 큐가 비면 `take` 가
        # 영영 None 을 돌려줘 화면이 "조회 중" 에 굳는다.
        except BaseException as exc:
            self._queue.put(f"사용량 조회가 실패했다: {exc}")


def _refresh_message(settings: config.Settings, labels: tuple[str, ...]) -> str:
    """`labels` 를 프로브해 캐시에 얹고, 화면에 띄울 한 줄을 만든다. curses 를 모른다."""
    try:
        codex_bin = str(resolve_codex_bin())
    except Exception as exc:
        return f"codex 를 찾지 못했다: {exc}"
    try:
        active = store.active_label(settings)
    except OSError as exc:
        return f"슬롯을 읽지 못했다: {exc}"
    failed = [lb for lb in labels if not _probe_into_cache(settings, lb, active, codex_bin)]
    if not failed:
        return "사용량을 새로 읽었다"
    return f"사용량을 읽지 못했다: {', '.join(failed)} (r 로 다시 시도)"


def probing_note(view: View, labels: Sequence[str]) -> View:
    """조회 중이라는 것을 메시지 줄에 얹는다. 화면이 멈춘 것처럼 보이지 않게 한다.

    기존 메시지를 지우지 않는다 — 실패 통지 위에 덮으면 사용자가 그것을 못 본다.
    """
    if not labels:
        return view
    note = f"사용량 조회 중… ({', '.join(labels)})"
    return replace(view, message=f"{view.message}   {note}" if view.message else note)


def _loop(stdscr, settings: config.Settings) -> None:  # pragma: no cover - 터미널 필요
    # 커서 숨기기는 terminfo 에 `civis` 가 없는 터미널에서 실패한다. 화면을 못 여는
    # 이유로는 사소하므로 삼킨다.
    with contextlib.suppress(curses.error):
        curses.curs_set(0)
    # 기본 ESCDELAY 는 1 초라 esc 를 누르면 화면이 멈춘 것처럼 보인다.
    if hasattr(curses, "set_escdelay"):
        curses.set_escdelay(25)
    colored = _init_colors()

    # 조회가 도는 동안에도 키를 읽어야 하므로 getch 를 논블로킹으로 만든다. 이 값이
    # 곧 조회 결과가 화면에 반영되는 지연이고, 사람이 못 느끼는 범위에서 가장 크게 잡는다.
    stdscr.timeout(_TICK_MS)

    # 자동 조회를 이미 시도한 라벨. 실패한 슬롯이 매 조작마다 화면을 붙잡지 않도록
    # 세션 동안 유지한다. `r` 은 이것과 무관하게 전부 다시 읽는다.
    attempted: set[str] = set()
    prober = _Prober()
    view = build_view(settings)

    def kick(labels: Sequence[str]) -> None:
        """조회를 띄운다. 실패해도 화면은 그대로 돌아간다."""
        if prober.start(settings, labels):
            attempted.update(labels)

    kick(auto_probe_targets(view, attempted))
    errs = 0
    while True:
        done = prober.take()
        if done is not None:
            here = view.rows[view.cursor].label if view.rows else None
            view = build_view(settings, select=here, message=done, carry=_carry(view))
            # 조회 중에 전환·등록이 있었으면 새 슬롯이 비어 있을 수 있다.
            kick(auto_probe_targets(view, attempted))
        _paint(stdscr, probing_note(view, prober.labels), colored)
        started = time.monotonic()
        key = stdscr.getch()

        if key == -1:
            # 타임아웃이 걸려 있으므로 -1 은 평상시의 "그 동안 입력이 없었다" 다. 죽은
            # tty 는 기다리지 않고 **즉시** -1 을 돌려주는 것으로 갈린다 — 그것만 센다.
            # 시간을 안 보고 세면 가만히 있는 사용자가 쫓겨난다.
            if time.monotonic() - started < _TICK_MS / 2000:
                errs += 1
                if errs > 50:
                    return
            continue
        errs = 0

        # q 는 어느 화면에서든 종료다. 정책 화면에서만 안 먹으면, 계정 화면이 "q 종료"
        # 라고 광고해 놓고 한 단계 들어가면 배신하는 셈이 된다.
        if key in (ord("q"), ord("Q")):
            return
        if key == curses.KEY_RESIZE:
            continue

        if view.mode == "policy":
            if key == 27:  # esc — 편집을 버린다
                view = build_view(
                    config.load(),
                    cursor=view.cursor,
                    message="저장하지 않고 나왔다",
                    carry=_carry(view),
                )
            elif key == curses.KEY_UP:
                view = replace(view, policy_cursor=max(0, view.policy_cursor - 1), message="")
            elif key == curses.KEY_DOWN:
                view = replace(
                    view,
                    policy_cursor=min(len(POLICY_FIELDS) - 1, view.policy_cursor + 1),
                    message="",
                )
            elif key == curses.KEY_LEFT:
                view = adjust_policy(view, -1)
            elif key == curses.KEY_RIGHT:
                view = adjust_policy(view, +1)
            elif key in (ord("s"), ord("S")):
                view = save_policy(view)
            continue

        if key == curses.KEY_UP:
            # 커서를 움직일 때마다 디스크를 다시 읽는다. 배경에서 rotate 가 돌면 `*` 가
            # 낡는데, 이 화면의 존재 이유가 바로 그 회전이다.
            view = build_view(settings, cursor=max(0, view.cursor - 1), carry=_carry(view))
        elif key == curses.KEY_DOWN:
            view = build_view(
                settings,
                cursor=min(max(len(view.rows) - 1, 0), view.cursor + 1),
                carry=_carry(view),
            )
        elif key in (curses.KEY_ENTER, 10, 13):
            # 전환은 캐시를 파일째 비운다. `carry` 가 직전 숫자를 이어받지만 그것도
            # 없는 슬롯(이 화면에서 아직 한 번도 못 읽은 것)은 배경에서 채운다.
            view = do_switch(view)
            kick(auto_probe_targets(view, attempted))
            curses.flushinp()
        elif key in (ord("r"), ord("R")):
            # 사용자가 명시적으로 시켰으므로 자동 조회의 억제를 푼다 — 일시적인
            # 네트워크 장애로 억제된 슬롯이 영영 물음표로 남으면 안 된다.
            attempted.clear()
            if not prober.start(settings, [r.label for r in view.rows]):
                view = replace(view, message="이미 조회 중이다")
        elif key in (ord("o"), ord("O")):
            view = do_toggle_auto(view)
            curses.flushinp()
        elif key in (ord("a"), ord("A")):
            view = do_adopt(view, _prompt(stdscr, "슬롯 이름: "))
            curses.flushinp()
        elif key in (ord("p"), ord("P")):
            view = replace(
                view, mode="policy", policy_cursor=0, message="", saved_settings=view.settings
            )


def run(settings: config.Settings) -> int:  # pragma: no cover - 터미널 필요
    try:
        curses.wrapper(_loop, settings)
    except KeyboardInterrupt:
        return 130
    except curses.error as exc:
        # TERM 이 비었거나 알 수 없으면 `initscr` 안에서 터진다. 트레이스백 대신 한 줄로
        # 알리고, 같은 일을 하는 서브커맨드가 있다는 것을 말해 준다.
        import sys

        print(
            f"codex-swap: 이 터미널에서는 화면을 열 수 없다 ({exc}). `codex-swap list` 를 쓰라.",
            file=sys.stderr,
        )
        return 1
    return 0
