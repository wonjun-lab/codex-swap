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

    credits: int | None = None
    """남은 사용량 리셋 쿠폰 수. 모르면 None.

    소진된 계정에 쿠폰이 남아 있으면 **전환하는 대신 그것을 쓰는** 선택지가 생긴다.
    지금까지 화면에 그 정보가 없어서 그 선택 자체가 보이지 않았다."""


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

    rung_provisional: bool = False
    """관문이 낡은 값에서 나온 추정인가. 참이면 `~` 를 붙여 표시한다."""

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
# 조작법은 **(키, 설명) 짝**으로 둔다. 문자열로 적어 두면 키가 어디부터 어디까지인지
# 다시 파싱해야 하는데, 그 파싱은 설명에 같은 글자가 들어가는 순간 틀린다 — 틀린 자리를
# 강조하는 화면은 강조가 없는 것보다 나쁘다. 폭에 맞춘 축약도 여기서 파생된다.
ACCOUNT_KEYS = (
    ("^v", "이동"),
    ("enter", "전환"),
    ("r", "사용량"),
    ("a", "등록"),
    ("p", "정책"),
    ("o", "자동전환"),
    ("q", "종료"),
)
AUTO_ON_LINES = ("  자동 전환: 켜짐   (o 로 끄기)", "  자동 전환: 켜짐", "  자동 ON", "  ON")
AUTO_OFF_LINES = ("  자동 전환: 꺼짐   (o 로 켜기)", "  자동 전환: 꺼짐", "  자동 OFF", "  OFF")
STALE_LEGENDS = (
    "  ~ 는 캐시가 낡았다는 표시다 (r 로 새로 읽는다)",
    "  ~ 는 낡은 값 (r 로 갱신)",
    "  ~ = 낡음",
)

POLICY_KEYS = (
    ("^v", "이동"),
    ("<>", "값 조정"),
    ("e", "직접 입력"),
    ("s", "저장"),
    ("esc", "취소"),
    ("q", "종료"),
)

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


def _help_line(*variants: str, width: int | None) -> str:
    """들어가는 것 중 가장 자세한 판을 고른다.

    긴 줄을 그냥 자르면 뒤쪽 키가 통째로 사라진다 — 40 칸에서 실제로 `r` 에서 잘렸다.
    자르는 대신 판을 바꾸면 **무엇이 빠졌는지가 보인다.** 마지막 판은 어떤 폭에서도
    쓰이므로 가장 짧아야 한다.
    """
    for variant in variants:
        if width is None or _width(variant) <= width:
            return variant
    return variants[-1]


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
        credits: int | None = None
        if entry is not None and "usedPercent" in entry:
            percent = entry["usedPercent"] if isinstance(entry["usedPercent"], int) else None
            raw_credits = entry.get("resetCredits")
            credits = raw_credits if isinstance(raw_credits, int) else None
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
            percent, credits = prev.percent, prev.credits
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
                credits=credits,
            )
        )

    rung, provisional = current_rung(settings, rows)

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
        current_rung=rung,
        rung_provisional=provisional,
        cooldown_left=cooldown_left(settings),
    )


def current_rung(settings: config.Settings, rows: Sequence[Row]) -> tuple[int | None, bool]:
    """지금 넘어야 하는 칸과 **그것이 잠정값인가**. 아는 값이 없으면 `(None, False)`.

    기준은 **가장 덜 쓴 계정**이다. 활성 기준으로 잡으면 앞선 쪽만 계속 올라가 둘이
    번갈아 밟기가 성립하지 않는다 (`policy.rung_for`). 화면이 정책과 다른 숫자를 말하면
    사용자는 화면 쪽을 믿으므로, 계산을 흉내내지 않고 그 함수를 그대로 부른다.

    **신선한 값이 하나라도 있으면 그것만 쓴다.** 정책이 보는 것은 TTL 안의 값 아니면
    방금 돌린 프로브뿐이라(`rotate._usage_of`), 섞으면 갈린다 — 활성 90% · 인증 실패
    후보의 낡은 10% · 정상 후보 80% 이면 섞은 쪽은 50%, 정책은 85% 를 관문으로 잡는다.

    하나도 신선하지 않으면(전환 직후가 그렇다) 낡은 값으로 **추정하고 잠정이라고 말한다.**
    아무것도 안 보여 주는 편이 정직해 보이지만, 그건 대부분의 시간에 화면에서 관문이
    사라진다는 뜻이다 — 틀릴 수 있다고 표시하며 보여 주는 편이 낫다. 표기는 사용량과
    같은 `~` 를 쓴다.
    """
    fresh = [r.percent for r in rows if r.percent is not None and not r.stale]
    if fresh:
        return policy.rung_for(settings.ladder, min(fresh)), False
    known = [r.percent for r in rows if r.percent is not None]
    if not known:
        return None, False
    return policy.rung_for(settings.ladder, min(known)), True


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
    # 시계가 뒤로 갔거나 스탬프가 미래면 `age` 가 음수가 되어 설정보다 긴 잔여가 나온다.
    # "쿨다운 3시간 남음" 인데 설정은 15 분인 화면은 사용자가 설정 쪽을 의심하게 만든다.
    left = min(settings.cooldown - age, settings.cooldown)
    return int(left) if left > 0 else None


# ── 그리기 ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Style:
    """한 줄에 입힐 표시 속성. curses 를 모른다.

    기본은 **줄 단위**다. 줄을 조각 리스트로 바꾸면 `render_lines` 가 문자열을 돌려주지
    못하게 되어 기존 화면 테스트가 전부 깨진다. 부분 강조가 필요한 곳은 `spans` 로
    덧칠한다 — 줄의 모양은 그대로 두고 구간만 얹으므로 그 대가를 치르지 않는다.
    """

    tone: str = "plain"
    """plain | dim | ok | warn | danger. 이름은 **의미**이지 색이 아니다 — 실제 색은
    `_paint` 한 곳에서 정하고, 색이 없는 터미널에서는 통째로 무시된다."""

    bold: bool = False

    spans: tuple[tuple[int, int, Style], ...] = ()
    """`(문자 시작, 문자 끝, 속성)`. 줄 위에 덧칠할 구간들.

    **칸이 아니라 문자 인덱스**다. 한글이 섞인 줄에서 둘은 다르고, 칸 계산은 실제로
    그리는 `_paint` 한 곳에만 두는 편이 안전하다 — 여기서 칸으로 적으면 폭 계산이
    순수 함수와 그리기 양쪽에 흩어진다.
    """


_PLAIN = Style()
_DIM = Style("dim")

_KEY_STYLE = Style("accent", bold=True)
"""단축키 글자에 입힐 속성. **설명이 아니라 키에만** 붙는다.

키와 설명이 같은 밝기면 눈이 어디를 눌러야 하는지 찾지 못하고 줄 전체를 읽게 된다.
반대로 줄 전체를 강조하면 그 줄이 화면에서 가장 시끄러운 것이 되는데, 조작법은 한 번
익히면 안 보는 정보다.
"""


def _assemble_keys(
    pairs: Sequence[tuple[str, str]], joiner: str, *, labels: bool
) -> tuple[str, tuple[tuple[int, int, Style], ...]]:
    """조작법 한 줄과 **키 글자의 구간**. 텍스트를 만들면서 구간을 함께 기록한다."""
    text = "  "
    spans = []
    for i, (key, label) in enumerate(pairs):
        if i:
            text += joiner
        start = len(text)
        text += key
        spans.append((start, len(text), _KEY_STYLE))
        if labels:
            text += f" {label}"
    return text, tuple(spans)


def keys_line(
    pairs: Sequence[tuple[str, str]], *, width: int | None
) -> tuple[str, tuple[tuple[int, int, Style], ...]]:
    """폭에 맞는 조작법 줄. 설명부터 버리고, 그다음 간격을 줄인다.

    키는 마지막까지 남는다 — 설명은 한 번 익히면 안 보지만 키는 계속 필요하다. 잘라내지
    않고 판을 바꾸는 이유는 잘리면 **뒤쪽 키가 통째로 사라지기** 때문이다(40 칸에서
    실제로 `r` 에서 잘렸다).
    """
    for joiner, labels in (("   ", True), ("  ", True), ("  ", False)):
        text, spans = _assemble_keys(pairs, joiner, labels=labels)
        if width is None or _width(text) <= width:
            return text, spans
    return _assemble_keys(pairs[-1:], "  ", labels=False)


BAR_FILL = "█"
BAR_EMPTY = "▒"
BAR_TICK = "┆"
BAR_TICK_PASSED = "╪"
AXIS_TICK = "┴"
AXIS_TICK_CURRENT = "┻"
"""바와 축의 글자. **East Asian Width 클래스가 전부 같아야 한다.**

원래 빈 칸이 `░`(Neutral)이고 채움이 `█`(Ambiguous)였다. Ambiguous 를 두 칸으로 그리는
터미널에서는 채움 비율이 곧 Ambiguous 글자 수라, **같은 24 글자 바가 행마다 다른 폭으로
그려졌다** — 58% 는 39 칸, 70% 는 42 칸. 사용자 눈에는 "아래 행의 바가 더 짧다" 로 보인다.

이 파일은 이미 같은 이유로 조작법을 ASCII 로 적어 두었는데(`↑↓` 는 Ambiguous), 바를
만들 때 그 교훈을 적용하지 않았다. 테스트가 이 불변식을 지킨다 — 눈으로는 안 보인다.
"""

_GUTTER = "  "
"""칼럼 사이 간격. 한 칸이면 `USED`·바·`RESET` 이 서로 붙어 읽힌다."""

BAR_COLS = 24
"""사용량 바의 칸 수. 0~100% 를 이만큼에 눌러 담는다.

24 는 4% 가 한 칸이라 사다리(50·70·85·95)가 서로 다른 칸에 떨어진다. 더 좁히면 85 와
95 가 같은 칸으로 뭉쳐 관문 표시가 뜻을 잃는다.
"""

_LABEL_COLS = 14
"""라벨 칸의 기본 폭."""

_LABEL_MIN = 6
"""라벨 칸의 하한. `master` · `shared` 정도가 온전히 들어간다."""

_EMAIL_MIN = 20
"""이메일 칸의 하한. 이보다 좁아지면 바를 포기한다.

`account.name@gmail.com` 류에서 20 칸이면 로컬 파트가 온전히 남아 계정이 구별된다.
더 줄이면 `account.…` 이 되어 확인하려던 것을 확인하지 못한다.
"""

# 바가 있는 줄의 고정 소비: 커서·활성 표시(3) + 라벨(14) + 공백 + 공백 + 사용량(6) +
# 공백 + 바 + 공백 + 리셋(20). 이메일은 남는 자리를 받는다.
_CREDIT_COLS = 4
"""리셋 쿠폰 열의 폭. `쿠폰` 머리말이 4 칸(한글 두 자)이고 값은 한 자리다."""

_RESET_COLS = 20


def _overhead(*, with_bar: bool, with_reset: bool) -> int:
    """라벨·이메일을 뺀 소비 폭.

    커서·활성 표시(3) + 사용량(6) + 켜져 있는 열들, 그리고 그 사이의 간격 전부.
    """
    g = len(_GUTTER)
    # 쿠폰은 리셋과 **같은 티어**다. 넷 다 부가 정보이고, 둘 중 하나만 남기면 그 경계에서
    # 화면이 어정쩡해진다 — 폭이 모자라면 두 열을 같이 접는다.
    tail = _CREDIT_COLS + g + _RESET_COLS + g if with_reset else 0
    return 3 + g + g + 6 + (BAR_COLS + g if with_bar else 0) + tail


_BAR_MIN_WIDTH = _overhead(with_bar=True, with_reset=True) + _LABEL_MIN + _EMAIL_MIN
"""바를 그리기 시작하는 폭. 손으로 고른 숫자가 아니라 다른 칸을 다 지키고 남는 자리에서 나온다."""

MIN_FIT_WIDTH = _overhead(with_bar=False, with_reset=False)
"""이 폭 이상이면 표와 크롬이 화면을 넘지 않는다.

그 아래는 열을 다 빼도 구분자와 사용량만으로 넘친다 — 터미널이 그 정도로 좁으면
`_paint` 의 클립에 맡긴다. 계약을 지킬 수 있는 범위를 **파생값으로** 적어 두는 편이,
숫자를 손으로 박아 두고 레이아웃이 바뀔 때마다 어긋나게 두는 것보다 낫다.
"""
"""바를 그리기 시작하는 터미널 폭. 이보다 좁으면 바와 축이 통째로 빠진다.

좁은 화면에서 바를 억지로 넣으면 이메일이나 리셋 시각이 잘려 나간다. 둘 다 바보다
먼저 필요한 정보다 — 어느 계정인지, 언제 풀리는지. 그래서 이 값은 손으로 고른 숫자가
아니라 **다른 칸을 다 지키고 남는 자리**에서 나온다.
"""


def _content_cols(values: Sequence[str], header: str, lo: int, hi: int) -> int:
    """내용에 맞춘 칸 폭. 머리말보다 좁아지지 않고, `hi` 를 넘지 않는다.

    고정폭으로 두면 짧은 값 뒤에 죽은 공백이 남는다 — 라벨 14 칸에 `master`(6) 를 넣으면
    여덟 칸이 그냥 비고, 그 빈 자리가 칼럼 사이 간격처럼 보여서 실제 간격(2 칸)과 뒤섞인다.
    """
    longest = max((_width(v) for v in values), default=0)
    return min(hi, max(lo, _width(header), longest))


def _layout(width: int | None, label_want: int, email_want: int) -> tuple[bool, bool, int, int]:
    """폭에 따라 무엇을 보여줄지 정한다. `(바, 리셋 시각, 라벨 칸, 이메일 칸)`.

    버리는 순서가 곧 우선순위다 — **바 → 리셋 시각 → 이메일 폭 → 라벨 폭**. 바는 있으면
    좋은 것이고, 리셋 시각은 이메일보다 나중에 필요하며, 어느 계정인지는 끝까지 남는다.

    넘치는 줄을 그리기 단계의 클립에 맡기지 않는다. 그러면 마지막 열이 반쯤 잘린 채 남아
    화면이 고장 난 것처럼 보인다 — 열을 통째로 빼는 편이 정직하다. 이 계약은
    `_MIN_FIT_WIDTH` 이상에서 성립한다.
    """
    if width is None:
        return True, True, label_want, email_want
    for with_bar, with_reset in ((True, True), (False, True), (False, False)):
        room = width - _overhead(with_bar=with_bar, with_reset=with_reset)
        if room >= label_want + email_want:
            return with_bar, with_reset, label_want, email_want
        # 자리가 모자라면 **이메일부터** 줄인다. 라벨은 어느 계정인지를 말하는 유일한
        # 열이고, 이메일은 그 확인일 뿐이다.
        if room >= label_want + _EMAIL_MIN:
            return with_bar, with_reset, label_want, room - label_want
    room = max(width - _overhead(with_bar=False, with_reset=False), 0)
    label = min(label_want, room)
    return False, False, label, max(0, room - label)


def _bar_cell(pct: float) -> int:
    """사용량이 몇 칸까지 채워지나. 0~BAR_COLS."""
    return min(max(round(pct * BAR_COLS / 100), 0), BAR_COLS)


def _tick_cell(pct: float) -> int:
    """눈금이 **몇 번째 칸**에 찍히나. 0~BAR_COLS-1.

    채움 수와 칸 번호를 같은 함수로 구하면 안 된다. 98% 이상은 채움이 24(= 전부)인데
    칸 번호로는 24 가 없어서(0~23) 눈금이 통째로 사라졌다. 축은 따로 clamp 하고 있어서
    축과 바가 서로 다른 자리를 가리켰다.
    """
    return min(_bar_cell(pct), BAR_COLS - 1)


def usage_bar(percent: int | None, rung: int | None) -> str:
    """사용량 바. 현재 관문 **하나만** 눈금으로 찍는다.

    사다리 네 칸을 전부 각 행에 찍으면 `░┆░░┃░░┆░░┆` 처럼 잡음이 된다. 행마다 다른 것은
    사용량뿐이고 관문은 모든 행에 같으므로, 전체 눈금은 아래 공용 축에 한 번만 그린다.
    여기 남기는 하나는 **지금 넘어야 하는 칸**이라 행마다 읽을 값이 있다.
    """
    if percent is None:
        return " " * BAR_COLS
    filled = _bar_cell(percent)
    tick = _tick_cell(rung) if rung is not None else None
    # 눈금이 넘어섰는지는 셀 인덱스가 아니라 **정책 술어**로 정한다. 정책은
    # `active_pct >= rung` 에서 전환하는데, 셀로 판정하면 정확히 관문 위(70% · 70)에서
    # `filled == tick` 이라 "아직 안 넘음" 으로 그려져 화면이 정책과 다른 말을 한다.
    crossed = rung is not None and percent >= rung
    out = []
    for i in range(BAR_COLS):
        if i == tick:
            out.append(BAR_TICK_PASSED if crossed else BAR_TICK)
        else:
            out.append(BAR_FILL if i < filled else BAR_EMPTY)
    return "".join(out)


def ladder_axis(ladder: Sequence[int], rung: int | None) -> tuple[str, str]:
    """바 아래에 한 번만 그리는 공용 눈금과 숫자. `(축, 숫자줄)`.

    현재 관문은 `┻` 로 갈라 둔다. 네 칸이 다 같아 보이면 "지금 어디를 넘어야 하나" 가
    다시 안 보인다 — 그게 이 축을 그리는 이유의 절반이다.
    """
    axis = [" "] * BAR_COLS
    # 숫자 줄만 **한 칸 넓다.** 마지막 칸(95·100)의 눈금이 바 끝에 있어서, 두 글자를
    # 눈금 오른쪽으로 밀 자리가 없으면 앞 숫자와 붙어 `8595` 가 된다.
    labels = [" "] * (BAR_COLS + 1)
    for step in ladder:
        i = _tick_cell(step)
        # 두 칸이 같은 자리에 떨어지면(사다리가 촘촘할 때) **현재 관문을 지킨다.**
        # 그냥 덮어쓰면 하필 지금 필요한 표시가 사라진다 — `[1,2,3,4]` 에서 실제로 그랬다.
        if axis[i] != AXIS_TICK_CURRENT:
            axis[i] = AXIS_TICK_CURRENT if step == rung else AXIS_TICK
        text = str(step)
        # 눈금이 첫 글자 위에 오게 놓는다. `i - len//2` 로 두면 두 글자 숫자가 눈금보다
        # 한 칸 왼쪽으로 치우쳐 보인다 — 눈으로 바로 어긋나 보이는 자리다.
        start = min(max(i - (len(text) - 1) // 2, 0), BAR_COLS + 1 - len(text))
        if start < 0:
            continue
        # 양옆 한 칸까지 비어 있어야 놓는다. 딱 붙으면 `8595` 처럼 한 덩어리로 읽힌다.
        lo, hi = max(start - 1, 0), min(start + len(text) + 1, BAR_COLS + 1)
        if any(labels[k] != " " for k in range(lo, hi)):
            continue
        for k, ch in enumerate(text):
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


def _headline(view: View, *, show_ladder: bool, width: int | None) -> str:
    """머리말. 사다리 전체보다 **지금 넘어야 하는 칸**이 행동을 정한다.

    `show_ladder` 는 바가 빠졌을 때다. 그때는 축도 없으므로 사다리 전체를 여기 적지
    않으면 화면 어디에도 남지 않는다.

    좁으면 **뒤에서부터 버린다.** 우선순위는 관문 → 쿨다운 → 마진이다 — 관문은 무엇을
    넘어야 하는지, 쿨다운은 언제 풀리는지이고, 마진은 그 둘을 안 뒤에나 필요하다.
    자르지 않고 버리는 것은 `마진 5%` 처럼 반쯤 남은 값이 틀린 정보이기 때문이다.
    """
    s = view.settings
    ladder = f"사다리 {','.join(map(str, s.ladder))}"
    if show_ladder or view.current_rung is None:
        gate = ladder
    else:
        gate = f"현재 관문 {'~' if view.rung_provisional else ''}{view.current_rung}%"
    parts = [gate]
    if view.cooldown_left is not None:
        parts.append(f"쿨다운 {_duration(view.cooldown_left)} 남음")
    parts.append(f"마진 {s.margin}%p")

    title = "codex-swap"
    while parts:
        line = f"{title}    " + " · ".join(parts)
        if width is None or _width(line) <= width:
            return line
        parts.pop()
    return title


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
        return _render_policy(view, height=height, width=width)

    s = view.settings
    # 바는 자리가 남을 때만 그린다. 억지로 넣으면 이메일·리셋 시각이 잘리는데, 둘 다
    # 바보다 먼저 필요한 정보다.
    with_bar, with_reset, label_cols, email_cols = _layout(
        width,
        _content_cols([r.label for r in view.rows], "LABEL", _LABEL_MIN, _LABEL_COLS),
        _content_cols([r.email for r in view.rows], "EMAIL", _EMAIL_MIN, 30),
    )
    head = [(_headline(view, show_ladder=not with_bar, width=width), _PLAIN), ("", _PLAIN)]

    # 꼬리말은 **버릴 수 있는 순서**로 쌓는다. 화면이 짧으면 앞쪽부터 버리고, 메시지는
    # 마지막까지 남긴다 — 실패를 알리는 유일한 줄이라 그것을 잃으면 사용자는 아무것도
    # 안 일어난 줄 안다.
    keys_text, keys_spans = keys_line(ACCOUNT_KEYS, width=width)
    droppable: list[tuple[str, Style]] = [
        (_help_line(*(AUTO_OFF_LINES if view.auto_off else AUTO_ON_LINES), width=width), _DIM),
        (keys_text, Style("dim", spans=keys_spans)),
    ]
    # `~` 는 낡은 값이라는 표시다. 범례가 없으면 사용자는 그 기호를 오류로 읽는다.
    # 낡은 행이 하나도 없으면 넣지 않는다 — 늘 떠 있는 안내는 곧 안 읽힌다.
    if any(r.stale for r in view.rows):
        droppable.insert(0, (_help_line(*STALE_LEGENDS, width=width), _DIM))
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

    columns = (
        f"   {_cell('LABEL', label_cols)}{_GUTTER}"
        f"{_cell('EMAIL', email_cols)}{_GUTTER}{_cell('USED', 6)}"
    )
    if with_bar:
        columns += f"{_GUTTER}{_cell('', BAR_COLS)}"
    if with_reset:
        columns += f"{_GUTTER}{_cell('쿠폰', _CREDIT_COLS)}{_GUTTER}RESET"
    header = [*head, (columns.rstrip() if not with_reset else columns, _DIM)]

    # ── 뷰포트 ──
    # 목록이 화면보다 길면 조작법이 먼저 밀려난다 — 계정 20 개 · 24 행에서 실제로 그랬다.
    # 머리말과 지켜야 할 꼬리말 자리를 먼저 떼고, 남는 만큼만 목록에 준다.
    rows = list(view.rows)
    start = 0
    axis_lines: list[tuple[str, Style]] = []
    if with_bar and any(r.percent is not None for r in view.rows):
        axis, labels = ladder_axis(s.ladder, view.current_rung)
        pad = f"   {' ' * label_cols}{_GUTTER}{' ' * email_cols}{_GUTTER}{' ' * 6}{_GUTTER}"
        axis_lines = [
            (f"{pad}{axis}", _DIM),
            (f"{pad}{labels}{_GUTTER}{AXIS_TICK_CURRENT} = 현재 관문", _DIM),
        ]

    tail_keep = [("", _PLAIN), *keep] if keep else []

    if height is None:
        tail = [("", _PLAIN), *droppable[::-1], *keep]
    else:
        # 최소 구성: 머리말 + 커서 행 1 + 지켜야 할 꼬리말. 남는 자리에 버릴 수 있는
        # 줄을 중요한 것(조작법)부터 채워 넣는다.
        room = height - len(header) - 1 - len(tail_keep)
        shown = []
        for entry in reversed(droppable):  # 조작법 → 자동전환 순
            if len(shown) + 1 <= max(room - 1, 0):  # 빈 줄 하나 몫을 남긴다
                shown.append(entry)
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
            f" {cursor}{mark}{_cell(row.label, label_cols, ellipsis=True)}{_GUTTER}"
            f"{_cell(row.email, email_cols, ellipsis=True)}{_GUTTER}{_cell(row.used, 6)}"
        )
        if with_bar:
            line += f"{_GUTTER}{usage_bar(row.percent, view.current_rung)}"
        if with_reset:
            credits = "-" if row.credits is None else str(row.credits)
            line += (
                f"{_GUTTER}{_cell(credits, _CREDIT_COLS)}"
                f"{_GUTTER}{_cell(row.reset, _RESET_COLS, ellipsis=True)}"
            ).rstrip()
        # **행 전체에 bold 를 걸지 않는다.** 이 터미널에서 bold 글자는 더 굵고 넓게
        # 그려져서, 같은 문자열인 바가 활성 행에서만 길어 보인다 — 실제로 두 행의
        # 문자열·폭·열 위치가 전부 같은데도 "아래 바가 더 짧다" 로 읽혔다.
        # 강조는 라벨 구간에만 얹는다. 거기는 글자라 굵어져도 뜻이 왜곡되지 않는다.
        spans = ((1, 3 + _width(row.label), Style(bold=True)),) if row.active else ()
        body.append((line, Style(_row_tone(row, view), spans=spans)))
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


def _render_policy(
    view: View, *, height: int | None = None, width: int | None = None
) -> list[tuple[str, Style]]:
    s = view.settings
    saved = view.saved_settings
    out: list[tuple[str, Style]] = [("codex-swap · 정책", _PLAIN), ("", _PLAIN)]
    for i, (key, title, why) in enumerate(POLICY_FIELDS):
        cursor = ">" if i == view.policy_cursor else " "
        value = ",".join(map(str, s.ladder)) if key == "ladder" else getattr(s, key)
        # 저장 안 된 편집을 표시한다. 아니면 화면의 숫자가 이미 반영된 것처럼 보인다.
        edited = " *" if saved is not None and getattr(s, key) != getattr(saved, key) else ""
        selected = i == view.policy_cursor
        out.append((f" {cursor} {_pad(title, 14)} {value}{edited}", Style(bold=selected)))
        if selected:
            out.append((f"     {why}", _DIM))
    keys_text, keys_spans = keys_line(POLICY_KEYS, width=width)
    out += [
        ("", _PLAIN),
        (keys_text, Style("dim", spans=keys_spans)),
        ("  s 를 누르면 저장되고, 이후 자동 전환이 이 값을 따른다", _DIM),
        (f"  저장 위치: {s.accounts_dir / config.CONFIG_NAME}", _DIM),
    ]
    if view.message:
        out += [("", _PLAIN), (f"  {view.message}", _PLAIN)]
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
            "resetCredits": u.reset_credits,
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


def edit_policy(view: View, raw: str | None) -> View:
    """커서가 있는 항목에 값을 **직접** 넣는다. 빈 입력은 취소다.

    파싱은 `config` 의 파서를 그대로 쓴다. 화면이 자기만의 규칙을 만들면 환경변수·설정
    파일과 받아들이는 값이 갈려서, 터미널에서는 되는데 화면에서는 거부되는(또는 그 반대)
    상황이 생긴다 — 그때 사용자는 어느 쪽이 진짜인지 알 수 없다.

    화살표 순환을 대신하는 것이 아니라 **더한다.** 흔한 값은 순환이 빠르고, 프리셋에
    없는 값은 이것뿐이다.
    """
    if not raw or not raw.strip():
        return replace(view, message="")
    key = POLICY_FIELDS[view.policy_cursor][0]
    title = POLICY_FIELDS[view.policy_cursor][1]
    if key == "ladder":
        rungs = config.parse_ladder(raw)
        if not rungs:
            return replace(view, message=f"{title}: 숫자를 읽지 못했다 (보기: 50,70,85,95)")
        # 정렬해 둔다. 정책은 앞에서부터 훑어 첫 상회 칸을 관문으로 잡으므로(`rung_for`),
        # 순서가 뒤엉킨 사다리는 조용히 엉뚱한 칸을 고른다.
        return replace(
            view, settings=replace(view.settings, ladder=tuple(sorted(set(rungs)))), message=""
        )
    try:
        value = config.parse_int(raw)
    except config.ConfigError:
        return replace(view, message=f"{title}: 정수가 아니다 ({raw.strip()!r})")
    if value < 0:
        # 음수는 `config` 가 받지만(bash 패리티) 이 값들에 뜻이 없다. 저장하면 쿨다운이
        # 영원히 안 걸리는 식으로 조용히 이상해진다.
        return replace(view, message=f"{title}: 0 보다 작을 수 없다")
    return replace(view, settings=replace(view.settings, **{key: value}), message="")


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


_TONE_COLORS = {"ok": 1, "warn": 2, "danger": 3, "accent": 4}
"""tone → color pair 번호. 0 은 curses 가 예약한 기본 쌍이라 1 부터 쓴다."""


def _tone_fg() -> dict[str, int]:  # pragma: no cover - curses 상수
    return {
        "ok": curses.COLOR_GREEN,
        "warn": curses.COLOR_YELLOW,
        "danger": curses.COLOR_RED,
        # 상태가 아니라 **조작**이라 상태 삼색과 겹치지 않는 색을 쓴다. 초록·노랑·빨강
        # 중 하나를 쓰면 단축키가 계정 상태를 말하는 것처럼 읽힌다.
        "accent": curses.COLOR_CYAN,
    }


def _init_colors() -> bool:  # pragma: no cover - 터미널 필요
    """색을 쓸 수 있으면 쌍을 등록하고 True.

    `use_default_colors` 로 배경을 -1 로 둔다. 검정으로 칠하면 밝은 테마 터미널에서
    글자만 남기고 배경이 뒤집혀 읽기 어려워진다.
    """
    if not curses.has_colors():
        return False
    with contextlib.suppress(curses.error):
        curses.start_color()
        # 함수 자체가 없는 빌드가 있다. 그건 `curses.error` 가 아니라 `AttributeError` 라
        # 안쪽 suppress 를 지나쳐 TUI 가 통째로 못 뜬다.
        background = -1
        try:
            curses.use_default_colors()
        except (curses.error, AttributeError):
            # 배경 -1 은 `use_default_colors` 가 성립해야 유효하다. 실패했으면 검정으로
            # 내린다 — 색을 통째로 포기하는 것보다 낫다.
            background = curses.COLOR_BLACK
        for tone, pair in _TONE_COLORS.items():
            curses.init_pair(pair, _tone_fg()[tone], background)
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
    room = max(width - 1, 0)
    for i, (line, style) in enumerate(render_screen(view, height=height - 1, width=room)):
        if i >= height - 1:
            break
        with contextlib.suppress(curses.error):
            stdscr.addnstr(i, 0, _clip(line, room), room, _attr_of(style, colored))
        # 구간을 **덧칠**한다. 줄을 조각으로 쪼개 이어 붙이지 않는 이유는, 그러면 폭
        # 계산이 조각마다 필요해지고 한 조각이 틀리면 뒤가 전부 밀리기 때문이다.
        for start, end, span_style in style.spans:
            # `spans` 는 문자 인덱스다. 칸으로 옮기는 계산은 여기 한 곳에만 둔다.
            col = _width(line[:start])
            if col >= room:
                break
            with contextlib.suppress(curses.error):
                stdscr.addnstr(
                    i,
                    col,
                    _clip(line[start:end], room - col),
                    room - col,
                    _attr_of(span_style, colored),
                )
    stdscr.refresh()


def _try(fn, *args: object) -> None:  # pragma: no cover - 터미널 필요
    """터미널 조작 하나를 시도하고 실패는 삼킨다.

    화면을 못 여는 이유로는 사소한 것들(`civis` 가 없다 …)이라 삼키는 것이 맞다. 다만
    **한 번에 하나씩** 삼켜야 한다 — 여럿을 한 블록에 두면 하나의 실패가 나머지를
    조용히 건너뛴다.
    """
    with contextlib.suppress(curses.error):
        fn(*args)


def _prompt(stdscr, label: str) -> str | None:  # pragma: no cover - 터미널 필요
    """한 줄 입력. 취소하거나 비면 None.

    **루프가 걸어 둔 논블로킹 타임아웃을 끄고 읽는다.** 켜진 채로 `getstr` 를 부르면
    `wgetch` 가 타임아웃마다 ERR 을 돌려줘서 사용자가 다 치기 전에 반쪽만 읽고 끝난다 —
    `master` 를 넣었는데 `ster` 슬롯이 만들어지는 것을 실제로 재현했다. 배경 조회를
    넣으면서 `stdscr.timeout()` 이 생겼고, 그때 이 함수를 같이 보지 않았다.
    """
    height, width = stdscr.getmaxyx()
    # **한 문장씩 따로 감싼다.** 한 `suppress` 에 몰아 넣으면 앞 문장이 던지는 순간
    # 뒤가 통째로 건너뛰어진다. 실제로 `curs_set(1)` 이 없는 터미널(vt100)에서 바로
    # 다음 줄인 타임아웃 해제가 실행되지 않아, `getstr` 가 논블로킹으로 남아 입력을
    # 한 글자도 못 받았다 — pty 테스트가 이것을 잡았다.
    _try(curses.echo)
    _try(curses.curs_set, 1)
    _try(stdscr.timeout, -1)
    try:
        stdscr.addnstr(height - 1, 0, _clip(label, width - 1), max(width - 1, 0))
        stdscr.clrtoeol()
        raw = stdscr.getstr(height - 1, min(_width(label), max(width - 1, 0)), 64)
    except (curses.error, KeyboardInterrupt):
        return None
    finally:
        # 나가는 쪽이 더 중요하다. 타임아웃을 복원하지 못하면 루프가 통째로 블로킹이
        # 되어, 배경 조회 결과가 영영 화면에 붙지 않고 쿨다운도 멈춘다.
        _try(curses.noecho)
        _try(curses.curs_set, 0)
        _try(stdscr.timeout, _TICK_MS)
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


def apply_probe_result(view: View, message: str) -> View:
    """배경 조회가 끝났다. 화면을 갱신한다 — **다만 계정 화면일 때만 다시 만든다.**

    `build_view` 는 디스크에서 새로 읽으므로 `mode` 도 기본값(계정)으로 돌아가고 아직
    저장하지 않은 정책 편집도 사라진다. 정책 화면에서 값을 고치는 동안 조회가 끝나면
    화면이 통째로 튀어나가고 편집이 날아갔다 — 실제로 그래서 `e` 로 넣은 사다리가
    반영되지 않았다.

    다른 화면에서는 결과 문구만 얹는다. 계정 화면으로 돌아오는 경로(esc)가 어차피
    디스크에서 다시 읽으므로 숫자는 그때 최신이 된다.
    """
    if view.mode != "accounts":
        return replace(view, message=message)
    here = view.rows[view.cursor].label if view.rows else None
    return build_view(view.settings, select=here, message=message, carry=_carry(view))


def refresh_clock(view: View) -> View:
    """시간이 지나 달라지는 값만 다시 읽는다.

    쿨다운 잔여는 `build_view` 에서 한 번 계산되고 그 뒤로 얼지 않아야 한다 — 입력 없이
    기다리는 동안 루프는 같은 `View` 를 계속 그리므로, 그냥 두면 화면이 "쿨다운 15분
    남음" 을 영원히 붙들고 있는다. 실제로 시계를 2000 초 넘겨도 그대로였다.

    디스크 전체를 다시 읽지 않는 이유는 이 함수가 매 틱(120 ms) 도는 자리이기 때문이다.
    스탬프 `stat` 한 번이면 되고, 나머지는 키 입력이 있을 때 `build_view` 가 갱신한다.
    """
    left = cooldown_left(view.settings)
    return view if left == view.cooldown_left else replace(view, cooldown_left=left)


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
            view = apply_probe_result(view, done)
            # 조회 중에 전환·등록이 있었으면 새 슬롯이 비어 있을 수 있다.
            kick(auto_probe_targets(view, attempted))
        _paint(stdscr, probing_note(refresh_clock(view), prober.labels), colored)
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
            elif key in (ord("e"), ord("E")):
                title = POLICY_FIELDS[view.policy_cursor][1]
                view = edit_policy(view, _prompt(stdscr, f"{title} = "))
                curses.flushinp()
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
