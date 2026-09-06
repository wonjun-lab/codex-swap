"""터미널 UI.

`codex-swap` 을 인자 없이 부르면 여기로 온다. 계정 목록이 바로 보이고, 화살표로 고르고,
단축키로 전환·정책 변경을 한다.

`curses` 는 표준 라이브러리다. 원래 계획은 Textual 이었지만 이 화면에는 과하고, 이 패키지의
**런타임 의존 0** 이라는 제약을 지키는 편이 낫다.

화면 그리기와 상태 변경을 갈라 둔다. `render_lines` 는 순수 함수라 curses 없이 테스트할 수
있고, 실제로 화면 내용에 대한 테스트는 전부 그것을 부른다 — 터미널을 띄우는 테스트는
CI 에서 돌지 않는다.
"""

from __future__ import annotations

import curses
import unicodedata
from dataclasses import dataclass, replace

__all__ = ["Row", "View", "build_view", "render_lines", "replace"]

from codex_swap.core import cache, config, identity, probe, store
from codex_swap.core.discovery import resolve_codex_bin
from codex_swap.core.types import ProbeOutcome


@dataclass(frozen=True)
class Row:
    label: str
    email: str
    used: str
    reset: str
    active: bool


@dataclass(frozen=True)
class View:
    """화면에 필요한 것 전부. curses 를 모른다."""

    rows: tuple[Row, ...]
    cursor: int
    settings: config.Settings
    message: str = ""
    mode: str = "accounts"
    """accounts | policy"""

    policy_cursor: int = 0


POLICY_FIELDS = (
    ("ladder", "사다리", "전환 관문. 가장 덜 쓴 계정의 사용량 바로 위 칸이 현재 관문이다"),
    ("margin", "마진(%p)", "대상이 이만큼 낮아야 전환한다. 동률 근처의 무의미한 교체를 막는다"),
    ("cooldown", "쿨다운(초)", "자동 전환 사이 최소 간격"),
    ("cache_ttl", "캐시(초)", "사용량 캐시 수명"),
    ("check_interval", "스로틀(초)", "판단 자체를 이 간격으로 묶는다"),
)


def build_view(settings: config.Settings, *, cursor: int = 0, message: str = "") -> View:
    """디스크에서 현재 상태를 읽어 화면 상태를 만든다. 프로브는 돌리지 않는다."""
    active = store.active_label(settings)
    rows = []
    for label in store.labels(settings):
        cached = cache.read(settings, label)
        rows.append(
            Row(
                label=label,
                email=identity.email_of(store.slot_auth(settings, label)) or "?",
                used=f"{cached['usedPercent']}%" if cached and "usedPercent" in cached else "?",
                reset=_reset_of(cached),
                active=label == active,
            )
        )
    return View(
        rows=tuple(rows),
        cursor=min(cursor, max(len(rows) - 1, 0)),
        settings=settings,
        message=message,
    )


def _reset_of(cached: dict | None) -> str:
    from codex_swap.cli import _reset_text

    return _reset_text(cached.get("resetsAt")) if cached else "-"


def render_lines(view: View) -> list[str]:
    """화면 내용. 순수 함수라 터미널 없이 테스트한다."""
    if view.mode == "policy":
        return _render_policy(view)

    s = view.settings
    out = [f"codex-swap    사다리 {','.join(map(str, s.ladder))} · 마진 {s.margin}%p", ""]
    if not view.rows:
        out += ["  등록된 계정이 없다.", "", "  a  이 계정을 슬롯에 등록 (adopt)    q  종료"]
        return out

    out.append(f"   {'LABEL':<14} {'EMAIL':<30} {'USED':<6} RESET")
    for i, row in enumerate(view.rows):
        cursor = ">" if i == view.cursor else " "
        mark = "*" if row.active else " "
        out.append(f" {cursor}{mark}{row.label:<14} {row.email:<30} {row.used:<6} {row.reset}")
    out += [
        "",
        "  ↑↓ 이동   enter 이 계정으로 전환   r 사용량 조회   p 정책   q 종료",
    ]
    if s.off_switch.exists():
        out.append("  자동 전환: 꺼짐   (o 로 켜기)")
    else:
        out.append("  자동 전환: 켜짐   (o 로 끄기)")
    if view.message:
        out += ["", f"  {view.message}"]
    return out


def _width(text: str) -> int:
    """터미널에서 차지하는 칸 수.

    한글은 두 칸을 먹는데 `str.__len__` 은 한 글자로 센다. 그래서 `f"{title:<12}"` 로
    맞추면 한글이 섞인 열이 어긋난다 — 실제로 정책 화면이 그렇게 깨져 있었다.
    """
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def _render_policy(view: View) -> list[str]:
    s = view.settings
    out = ["codex-swap · 정책", ""]
    for i, (key, title, why) in enumerate(POLICY_FIELDS):
        cursor = ">" if i == view.policy_cursor else " "
        value = ",".join(map(str, s.ladder)) if key == "ladder" else getattr(s, key)
        out.append(f" {cursor} {_pad(title, 14)} {value}")
        if i == view.policy_cursor:
            out.append(f"     {why}")
    out += [
        "",
        "  ↑↓ 이동   ←→ 값 조정   s 저장   esc 돌아가기",
        f"  저장 위치: {s.accounts_dir / config.CONFIG_NAME}",
    ]
    if view.message:
        out += ["", f"  {view.message}"]
    return out


# ── 동작 ─────────────────────────────────────────────────────────────────────


def do_switch(view: View) -> View:
    if not view.rows:
        return replace(view, message="등록된 계정이 없다")
    target = view.rows[view.cursor]
    if target.active:
        return replace(view, message=f"{target.label} 은 이미 활성이다")
    try:
        with store.switch_lock(view.settings):
            store.switch(view.settings, target.label, "manual (tui)")
    except Exception as exc:
        return replace(view, message=f"전환 실패: {exc}")
    return build_view(view.settings, cursor=view.cursor, message=f"전환했다: {target.label}")


def do_refresh(view: View) -> View:
    """모든 슬롯을 프로브해 캐시를 채운다. 네트워크를 타므로 명시적 키에만 건다."""
    s = view.settings
    try:
        codex_bin = str(resolve_codex_bin())
    except Exception as exc:
        return replace(view, message=f"codex 를 찾지 못했다: {exc}")
    active = store.active_label(s)
    failed = []
    for label in store.labels(s):
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
    return build_view(s, cursor=view.cursor, message=msg)


def do_toggle_auto(view: View) -> View:
    """자동 전환 on/off. off-switch 파일 하나가 그 스위치다 — bash 와 같은 파일이다."""
    sw = view.settings.off_switch
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
    return build_view(view.settings, cursor=view.cursor, message=msg)


LADDER_PRESETS = ((50, 70, 85, 95), (70,), (50, 75), (25, 50, 75, 90), (90,))


def adjust_policy(view: View, delta: int) -> View:
    """정책 값 하나를 올리거나 내린다.

    사다리는 자유 입력 대신 프리셋을 돈다. 화살표로 임의의 목록을 편집하게 만들면
    조작이 번거롭고, 사다리는 실제로 몇 가지 모양 중 하나를 고르는 값이다.
    """
    key = POLICY_FIELDS[view.policy_cursor][0]
    s = view.settings
    if key == "ladder":
        try:
            idx = LADDER_PRESETS.index(s.ladder)
        except ValueError:
            idx = 0
        return replace(
            view, settings=replace(s, ladder=LADDER_PRESETS[(idx + delta) % len(LADDER_PRESETS)])
        )

    step = {"margin": 1, "cooldown": 60, "cache_ttl": 60, "check_interval": 10}[key]
    value = max(0, getattr(s, key) + delta * step)
    return replace(view, settings=replace(s, **{key: value}))


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
    return replace(view, message=f"저장했다: {path}")


# ── curses 루프 ──────────────────────────────────────────────────────────────


def _loop(stdscr, settings: config.Settings) -> None:  # pragma: no cover - 터미널 필요
    curses.curs_set(0)
    view = build_view(settings)
    while True:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        for i, line in enumerate(render_lines(view)):
            if i >= height - 1:
                break
            stdscr.addnstr(i, 0, line, max(width - 1, 0))
        stdscr.refresh()

        key = stdscr.getch()
        if view.mode == "policy":
            if key in (27,):  # esc
                view = build_view(view.settings, cursor=view.cursor)
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

        if key in (ord("q"), ord("Q")):
            return
        if key == curses.KEY_UP:
            view = replace(view, cursor=max(0, view.cursor - 1), message="")
        elif key == curses.KEY_DOWN:
            view = replace(
                view, cursor=min(max(len(view.rows) - 1, 0), view.cursor + 1), message=""
            )
        elif key in (curses.KEY_ENTER, 10, 13):
            view = do_switch(view)
        elif key in (ord("r"), ord("R")):
            view = do_refresh(view)
        elif key in (ord("o"), ord("O")):
            view = do_toggle_auto(view)
        elif key in (ord("p"), ord("P")):
            view = replace(view, mode="policy", policy_cursor=0, message="")


def run(settings: config.Settings) -> int:  # pragma: no cover - 터미널 필요
    curses.wrapper(_loop, settings)
    return 0
