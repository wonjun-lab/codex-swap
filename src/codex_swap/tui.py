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
import unicodedata
from dataclasses import dataclass, replace

from codex_swap.core import cache, config, identity, probe, store
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


def _cell(text: str, cols: int) -> str:
    """잘라내고 채운다. 긴 라벨·이메일이 열을 밀어내지 못하게 한다."""
    return _pad(_clip(text, cols), cols)


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
) -> View:
    """디스크에서 현재 상태를 읽어 화면 상태를 만든다. 프로브는 돌리지 않는다.

    `select` 를 주면 커서를 그 라벨에 맞춘다. 커서가 위치 인덱스뿐이면 목록이 바뀔 때
    엉뚱한 계정을 가리키게 되고, 그 상태에서 enter 를 누르면 의도하지 않은 전환이 된다.
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
        cached = cache.read(settings, label)
        rows.append(
            Row(
                label=label,
                email=identity.email_of(store.slot_auth(settings, label)) or "?",
                used=f"{cached['usedPercent']}%" if cached and "usedPercent" in cached else "?",
                reset=_reset_text(cached.get("resetsAt")) if cached else "-",
                active=label == active,
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
    )


# ── 그리기 ───────────────────────────────────────────────────────────────────


def render_lines(view: View, *, height: int | None = None, width: int | None = None) -> list[str]:
    """화면 내용. 순수 함수라 터미널 없이 테스트한다."""
    if view.mode == "policy":
        return _render_policy(view, height=height, width=width)

    s = view.settings
    head = [f"codex-swap    사다리 {','.join(map(str, s.ladder))} · 마진 {s.margin}%p", ""]

    # 꼬리말은 **버릴 수 있는 순서**로 쌓는다. 화면이 짧으면 앞쪽부터 버리고, 메시지는
    # 마지막까지 남긴다 — 실패를 알리는 유일한 줄이라 그것을 잃으면 사용자는 아무것도
    # 안 일어난 줄 안다.
    droppable = [
        "  자동 전환: 꺼짐   (o 로 켜기)" if view.auto_off else "  자동 전환: 켜짐   (o 로 끄기)",
        _help_line(ACCOUNT_KEYS_FULL, ACCOUNT_KEYS_SHORT, width),
    ]
    # 활성 계정이 어느 슬롯과도 안 맞으면 전환이 지금 자격증명을 버린다. 조용히 두면
    # 사용자는 enter 한 번으로 그것을 잃는다.
    keep = []
    if view.rows and not view.active_registered:
        who = view.active_email or "알 수 없는 계정"
        keep.append(f"  주의: 활성({who})이 슬롯에 없다. 전환하면 이 자격증명은 보관되지 않는다")
    if view.message:
        keep.append(f"  {view.message}")

    if not view.rows:
        # 빈 화면에서도 메시지가 보여야 한다 — 등록 실패가 여기서 나온다. 다만 조작법은
        # 이 화면에 실제로 있는 키만 적는다.
        empty = ["  등록된 계정이 없다.", "", "  a  지금 로그인된 계정을 슬롯에 등록   q  종료"]
        if view.message:
            empty += ["", f"  {view.message}"]
        return head + empty

    header = [*head, f"   {_cell('LABEL', 14)} {_cell('EMAIL', 30)} {_cell('USED', 6)} RESET"]

    # ── 뷰포트 ──
    # 목록이 화면보다 길면 조작법이 먼저 밀려난다 — 계정 20 개 · 24 행에서 실제로 그랬다.
    # 머리말과 지켜야 할 꼬리말 자리를 먼저 떼고, 남는 만큼만 목록에 준다.
    rows = list(view.rows)
    start = 0
    tail_keep = ["", *keep] if keep else []

    if height is None:
        tail = ["", *droppable[::-1], *keep]
    else:
        # 최소 구성: 머리말 + 커서 행 1 + 지켜야 할 꼬리말. 남는 자리에 버릴 수 있는
        # 줄을 중요한 것(조작법)부터 채워 넣는다.
        room = height - len(header) - 1 - len(tail_keep)
        shown = []
        for line in reversed(droppable):  # 조작법 → 자동전환 순
            if len(shown) + 1 <= max(room - 1, 0):  # 빈 줄 하나 몫을 남긴다
                shown.append(line)
        tail = (["", *shown] if shown else []) + tail_keep

        budget = height - len(header) - len(tail)
        # 스크롤 표시가 붙을 수 있으므로 두 줄을 미리 뗀다.
        if len(rows) > budget:
            budget = max(budget - 2, 1)
            start = min(max(0, view.cursor - budget // 2), len(rows) - budget)
            rows = rows[start : start + budget]

    hidden_above = start
    hidden_below = len(view.rows) - (start + len(rows))

    body = []
    if hidden_above:
        body.append(f"   ^ {hidden_above}개 더")
    for i, row in enumerate(rows, start=start):
        cursor = ">" if i == view.cursor else " "
        mark = "*" if row.active else " "
        body.append(
            f" {cursor}{mark}{_cell(row.label, 14)} {_cell(row.email, 30)} "
            f"{_cell(row.used, 6)} {row.reset}"
        )
    if hidden_below:
        body.append(f"   v {hidden_below}개 더")

    # 최종 클램프. 아주 짧은 화면에서는 스크롤 표시까지 합한 바닥(머리말 3 + 표시 2 +
    # 행 1 + 메시지 2 = 8)이 화면보다 클 수 있다. 그때는 **본문**을 자른다 — 꼬리말을
    # 자르면 그리기 단계가 밑에서부터 잘라내므로 메시지가 먼저 사라진다.
    if height is not None:
        over = len(header) + len(body) + len(tail) - height
        if over > 0:
            keep_n = max(len(body) - over, 1)
            # 커서가 있는 줄을 남긴다. 표시줄이 먼저 밀려나는 것이 자연스럽다.
            cursor_at = next(
                (i for i, ln in enumerate(body) if ln.startswith(" >")), len(body) // 2
            )
            lo = min(max(0, cursor_at - keep_n // 2), max(0, len(body) - keep_n))
            body = body[lo : lo + keep_n]

        # 그래도 넘치면(머리말 3 + 행 1 + 메시지 2 = 6 이 바닥) 머리말을 앞에서 줄인다.
        # 제목과 빈 줄보다 "무엇이 잘못됐나" 가 먼저다.
        while len(header) + len(body) + len(tail) > height and len(header) > 1:
            header = header[1:]
        # 마지막 한 줄은 꼬리말의 빈 줄에서 뺀다.
        if len(header) + len(body) + len(tail) > height and tail and tail[0] == "":
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
        return build_view(
            view.settings, select=target.label, message=f"{target.label} 은 이미 활성이다"
        )
    try:
        with store.switch_lock(view.settings):
            store.switch(view.settings, target.label, "manual (tui)")
    except store.LockBusy:
        return build_view(
            view.settings,
            select=target.label,
            message="다른 전환이 진행 중이다. 잠시 뒤 다시 눌러라",
        )
    except (store.LockUnusable, store.StoreError) as exc:
        return build_view(view.settings, select=target.label, message=str(exc))
    except Exception as exc:
        return build_view(view.settings, select=target.label, message=f"전환 실패: {exc}")
    return build_view(view.settings, select=target.label, message=f"전환했다: {target.label}")


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
    return build_view(view.settings, select=label, message=f"등록했다: {label}")


def do_refresh(view: View) -> View:
    """모든 슬롯을 프로브해 캐시를 채운다. 네트워크를 타므로 명시적 키에만 건다."""
    s = view.settings
    try:
        codex_bin = str(resolve_codex_bin())
    except Exception as exc:
        return replace(view, message=f"codex 를 찾지 못했다: {exc}")
    try:
        active = store.active_label(s)
        labels = store.labels(s)
    except OSError as exc:
        return replace(view, message=f"슬롯을 읽지 못했다: {exc}")

    failed = []
    for label in labels:
        home = s.default_home if label == active else store.slot_dir(s, label)
        try:
            result = probe.probe(codex_bin, str(home))
        except Exception:
            failed.append(label)
            continue
        if result.outcome is ProbeOutcome.OK and result.usage is not None:
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
        else:
            failed.append(label)
    msg = "사용량을 새로 읽었다" if not failed else f"조회 실패: {', '.join(failed)}"
    select = view.rows[view.cursor].label if view.rows else None
    return build_view(s, select=select, message=msg)


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
    return build_view(view.settings, select=select, message=msg)


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


def _paint(stdscr, view: View) -> None:  # pragma: no cover - 터미널 필요
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    if height < 2 or width < 2:
        stdscr.refresh()
        return
    # 루프는 마지막 행 마지막 칸에 쓰면 curses 가 에러를 내므로 한 줄을 비워 둔다.
    for i, line in enumerate(render_lines(view, height=height - 1, width=width - 1)):
        if i >= height - 1:
            break
        with contextlib.suppress(curses.error):
            stdscr.addnstr(i, 0, _clip(line, width - 1), max(width - 1, 0))
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


def _loop(stdscr, settings: config.Settings) -> None:  # pragma: no cover - 터미널 필요
    # 커서 숨기기는 terminfo 에 `civis` 가 없는 터미널에서 실패한다. 화면을 못 여는
    # 이유로는 사소하므로 삼킨다.
    with contextlib.suppress(curses.error):
        curses.curs_set(0)
    # 기본 ESCDELAY 는 1 초라 esc 를 누르면 화면이 멈춘 것처럼 보인다.
    if hasattr(curses, "set_escdelay"):
        curses.set_escdelay(25)

    view = build_view(settings)
    errs = 0
    while True:
        _paint(stdscr, view)
        key = stdscr.getch()

        # tty 가 영구히 죽으면 getch 가 -1 을 즉시 반복 반환해 CPU 를 태운다.
        if key == -1:
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
                view = build_view(config.load(), cursor=view.cursor, message="저장하지 않고 나왔다")
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
            view = build_view(settings, cursor=max(0, view.cursor - 1))
        elif key == curses.KEY_DOWN:
            view = build_view(settings, cursor=min(max(len(view.rows) - 1, 0), view.cursor + 1))
        elif key in (curses.KEY_ENTER, 10, 13):
            view = do_switch(view)
            curses.flushinp()
        elif key in (ord("r"), ord("R")):
            # 프로브는 슬롯당 최대 20 초다. 먼저 그려 두지 않으면 화면이 굳은 채로
            # 아무 표시가 없고, 그동안 눌린 키는 끝난 뒤 한꺼번에 재생된다 — `o` 가
            # 섞여 있으면 자동 전환이 조용히 꺼진다.
            _paint(stdscr, replace(view, message="사용량 조회 중…"))
            view = do_refresh(view)
            curses.flushinp()
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
