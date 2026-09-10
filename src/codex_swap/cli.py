"""CLI 어댑터.

이 층은 서식과 exit code 만 다룬다. 판단은 전부 core 가 하고, 여기서는 그 결과를 사람이
읽을 문자열로 옮긴다. bash 에서 정책과 출력이 한 함수에 섞여 있던 것을 가르는 것이 이
이관의 목적 중 하나다.

`rotate` 만은 출력 규율이 특별하다 (계약 2 · 설계문 §5.1). wrapper 가 매 codex 호출마다
이 명령을 부르고 **stdout 만** 버리므로, stderr 로 나가는 것은 전부 사용자 화면에 실린다.
그래서 rotate 는 stdout 을 비우고 stderr 에는 전환이 실제로 일어났을 때만 한 줄 쓴다.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import unicodedata
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from codex_swap import __version__
from codex_swap.core import cache, config, discovery, identity, paths, probe, rotate, store
from codex_swap.core.types import ProbeOutcome, Switched, Usage, decision_exit_code


class CliError(Exception):
    """사용자에게 보여줄 실패. stderr 한 줄로 나가고 exit 1 이다."""


# ── 서식 ─────────────────────────────────────────────────────────────────────


def _opt(v: object) -> str:
    """bash 의 `// "-"` — 없는 값은 대시로 보인다."""
    return "-" if v is None else str(v)


def _reset_text(resets_at: object, *, now: float | None = None) -> str:
    """사용량이 되돌아오는 시각. epoch 을 사람이 읽을 형태로 바꾼다.

    raw epoch 을 그대로 보여주면 "언제 풀리나" 를 계산기 없이 알 수 없다. 남은 시간을
    함께 적는 이유는 그것이 실제로 알고 싶은 값이기 때문이다 — 오늘 안에 풀리는지,
    며칠 기다려야 하는지.

    쿠폰으로 사용량을 리셋하면 이 값이 앞으로 당겨진다. 그래서 이 표시가 곧 "쿠폰이
    먹었나" 를 확인하는 자리이기도 하다.
    """
    if isinstance(resets_at, bool) or not isinstance(resets_at, (int, float)):
        return "-"
    when = datetime.datetime.fromtimestamp(resets_at)
    current = datetime.datetime.now() if now is None else datetime.datetime.fromtimestamp(now)
    secs = int((when - current).total_seconds())
    if secs < 0:
        rel = "past"
    elif secs < 3600:
        rel = f"in {secs // 60}m"
    elif secs < 86400:
        rel = f"in {secs // 3600}h"
    else:
        rel = f"in {secs // 86400}d"
    return f"{when:%m-%d %H:%M} ({rel})"


def _usage_line(u: Usage) -> str:
    return (
        f"usage: {u.used_percent}% "
        f"(primary {_opt(u.primary_percent)}%, secondary {_opt(u.secondary_percent)}%) "
        f"· plan {_opt(u.plan_type)} · credits {_opt(u.reset_credits)} "
        f"· resets {_reset_text(u.resets_at)}"
    )


def _usage_from_cache(d: dict[str, Any]) -> Usage:
    """캐시 항목을 `Usage` 로 되돌린다.

    **`cache.write` 가 저장하는 키와 여기가 읽는 키가 같아야 한다.** 하나가 빠지면 그
    값은 캐시 히트일 때만 사라져서, 같은 계정이 경로에 따라 다른 말을 한다. 실제로
    `resetCredits` 가 그랬다 — `list` 는 2 라는데 `status` 는 `-` 였다.
    """
    return Usage(
        used_percent=int(d["usedPercent"]),
        email=d.get("email"),
        plan_type=d.get("planType"),
        primary_percent=d.get("primaryPercent"),
        secondary_percent=d.get("secondaryPercent"),
        resets_at=d.get("resetsAt"),
        reset_credits=d.get("resetCredits"),
        reached=bool(d.get("reached")),
    )


# ── 명령 ─────────────────────────────────────────────────────────────────────


def cmd_adopt(settings: config.Settings, label: str) -> int:
    if not store.label_syntax_ok(label):
        raise CliError(f"not a usable label: {label}")
    live = store.active_auth(settings)
    if not live.is_file():
        # `CODEX_HOME` 으로 홈을 옮긴 사용자는 실제로 **로그인돼 있는데** 여기서 "안 됐다"
        # 는 말을 듣는다. 이 도구가 그 변수를 따라가지 않는 것은 의도이므로(재귀 방어가
        # 죽는다 — `config.load` 참조), 대신 무엇을 하면 되는지 말해 준다. 그 안내 없이는
        # `codex login` 을 다시 돌리게 되고, 그건 같은 자리에 또 쓰여 영영 안 낫는다.
        moved = os.environ.get("CODEX_HOME")
        if moved and Path(moved).resolve() != settings.default_home.resolve():
            raise CliError(
                f"not logged in ({live} is missing), but CODEX_HOME points at {moved}. "
                f"Tell codex-swap too: CODEX_ACCOUNT_DEFAULT_HOME={moved}"
            )
        raise CliError(f"not logged in ({live} is missing)")

    # **다른 계정의 슬롯을 덮어쓰지 않는다.** `adopt` 는 활성 자격증명을 그 이름 위에
    # 그냥 복사하므로, 기존 이름을 입력하면 그 계정의 보관본이 사라지고 되돌릴 방법이
    # 없다. `tui.do_adopt` 는 이것을 막고 있었는데 CLI 에만 가드가 없었다 — 같은 위험에
    # 두 표면이 다르게 행동하면 약한 쪽이 곧 이 도구의 실제 안전 수준이다.
    #
    # 같은 계정이면 갱신이므로 허용한다. 썩은 사본을 새로 뜨는 정상 용법이다.
    existing = store.slot_auth(settings, label)
    if existing.exists():
        slot_email = identity.email_of(existing)
        if slot_email is not None and slot_email != identity.email_of(live):
            raise CliError(
                f"'{label}' already holds {slot_email}. Not overwriting "
                f"(to discard that account first: codex-swap remove {label})"
            )

    paths.ensure_root(settings)
    slot = store.slot_dir(settings, label)
    slot.mkdir(mode=0o700, parents=True, exist_ok=True)
    slot.chmod(0o700)
    dest = store.slot_auth(settings, label)
    shutil.copy2(live, dest)
    dest.chmod(0o600)
    print(f"adopted {label} ({identity.email_of(dest) or 'email unknown'})")
    return 0


def cmd_add(
    settings: config.Settings,
    label: str,
    *,
    force: bool = False,
    runner: Callable[[list[str], dict[str, str]], int] | None = None,
) -> int:
    """새 슬롯에 브라우저 로그인시킨다.

    `adopt` 와 달리 자격증명을 **복사하지 않고** codex 에게 슬롯 홈으로 로그인시킨다.
    그래서 이 명령만은 사용자 상호작용(브라우저)을 끼고 돈다.

    `runner` 는 테스트가 갈아끼우는 자리다. 실제 로그인은 사람 없이는 재현할 수 없지만,
    **무엇을 어떤 환경으로 부르는지**는 그것 없이도 고정할 수 있다 — 그 조립이 틀리면
    자격증명이 엉뚱한 홈에 떨어지거나 wrapper 재귀가 생긴다.
    """
    if not store.label_syntax_ok(label):
        raise CliError(f"not a usable label: {label}")
    existing = store.slot_auth(settings, label)
    if existing.exists() and not force:
        # 오래 안 쓴 슬롯은 토큰이 갱신 한계를 넘어 썩는다. 그것을 고치려면 `remove` 를
        # **먼저** 해야 했다 — 되돌릴 수 없는 삭제를 하고 나서, 실패할 수 있는 브라우저
        # 로그인을 시도하는 순서다. 로그인이 실패하면 아무것도 남지 않는다.
        raise CliError(
            f"label already exists: {label} "
            f"(--force logs in again and replaces it, or use remove to drop it)"
        )

    try:
        codex_bin = discovery.resolve_codex_bin()
    except discovery.UpstreamNotFound as exc:
        # `UpstreamNotFound` 의 메시지가 이미 "codex 를 못 찾았다" 이므로 그것을 다시
        # 감싸면 같은 말이 두 번 나온다 — 실제로
        # `could not find the codex binary: could not find the codex binary` 였다.
        raise CliError(f"{exc}. Set CODEX_ACCOUNT_BIN if it is installed elsewhere") from exc
    except Exception as exc:
        raise CliError(f"could not resolve the codex binary: {exc}") from exc

    paths.ensure_root(settings)
    slot = store.slot_dir(settings, label)
    slot.mkdir(mode=0o700, parents=True, exist_ok=True)
    slot.chmod(0o700)

    if existing.exists():
        # 무엇을 대신하는지 말하고 시작한다. 로그인 화면에서 계정을 고르는 것은 사용자라,
        # 여기서 이름을 보여 주지 않으면 엉뚱한 계정으로 덮고도 모른다.
        print(f"Replacing '{label}' ({identity.email_of(existing) or 'email unknown'}).")
    print(f"Logging in to slot '{label}'. Use an account **different** from the active one.")

    # `CODEX_ROTATE_SKIP=1` 이 없으면 이 로그인이 띄우는 codex 가 wrapper 를 거쳐 다시
    # rotate 를 부르고, 그 rotate 가 지금 만들고 있는 슬롯을 후보로 본다. `CODEX_HOME` 이
    # 슬롯을 가리키므로 rotate 의 홈 가드에도 걸리지만, 두 겹으로 막는다.
    env = discovery.env_with_bin_dir(codex_bin)
    env["CODEX_ROTATE_SKIP"] = "1"
    env["CODEX_HOME"] = str(slot)

    run = runner or _run_login
    if run([str(codex_bin), "login"], env) != 0:
        raise CliError("login failed")

    dest = store.slot_auth(settings, label)
    if not dest.is_file():
        raise CliError("login finished but no auth.json appeared")
    dest.chmod(0o600)
    print(f"adopted {label} ({identity.email_of(dest) or 'email unknown'})")
    return 0


def _run_login(argv: list[str], env: dict[str, str]) -> int:
    """브라우저 로그인은 대화형이라 stdio 를 그대로 물려준다.

    `discovery.resolve_codex_bin` 은 `CODEX_ACCOUNT_BIN` 을 **검증하지 않는다** — 핫패스에
    파일 읽기를 얹지 않으려는 의도된 선택이다(§8). 그 대가가 여기서 나온다: 값이 실행할
    수 없는 경로면 `subprocess` 가 `OSError` 를 던지고, `main` 은 `CliError` 계열만 잡으므로
    그 예외가 raw traceback 으로 사용자 화면까지 갔다. 환경 문제를 프로그램 결함처럼
    보이게 하는 출력이라 여기서 접는다.
    """
    try:
        return subprocess.call(argv, env=env)
    except OSError as exc:
        raise CliError(
            f"cannot run codex: {argv[0]} ({exc.strerror}). "
            "Check CODEX_ACCOUNT_BIN, or unset it and retry"
        ) from exc


def _policy_json(settings: config.Settings) -> dict[str, Any]:
    return {
        "ladder": list(settings.ladder),
        "margin": settings.margin,
        "cooldown": settings.cooldown,
        "cacheTtl": settings.cache_ttl,
        "checkInterval": settings.check_interval,
        "busyWindow": settings.busy_window,
    }


def cmd_list_json(settings: config.Settings) -> int:
    """`list` 의 기계용 판. **이 모양이 계약이다.**

    사람용 표는 폭·문구가 바뀌는 것이 정상이라 스크립트가 기댈 수 없다. 그런데 이 도구는
    래퍼·훅에서 불리는 것이 존재 이유라 기계가 읽을 자리가 필요하다. 여기서는 서식 대신
    **값**만 낸다 — `~` 접두어 대신 `stale` 불리언, `?` 대신 `null`.

    못 읽은 사용량을 `0` 으로 채우지 않는 것이 중요하다. 0 은 "가장 덜 쓴 계정" 으로
    읽혀 정반대의 판단을 부른다 (설계문 §6.4.1 의 3-상태와 같은 이유다).
    """
    active = store.active_label(settings)
    accounts = []
    for label in store.labels(settings):
        cached = cache.read(settings, label)
        stale = False
        if cached is None:
            aged = cache.read_stale(settings, label)
            if aged is not None:
                cached, stale = aged[0], True
        pct = cached.get("usedPercent") if isinstance(cached, dict) else None
        accounts.append(
            {
                "label": label,
                "email": identity.email_of(store.slot_auth(settings, label)),
                "active": label == active,
                "usedPercent": pct if isinstance(pct, int) else None,
                "stale": stale if pct is not None else False,
                "resetsAt": cached.get("resetsAt") if isinstance(cached, dict) else None,
                "resetCredits": cached.get("resetCredits") if isinstance(cached, dict) else None,
            }
        )
    print(
        json.dumps(
            {
                "active": active,
                "accounts": accounts,
                "policy": _policy_json(settings),
                "autoSwitch": not settings.off_switch.exists(),
            },
            indent=2,
        )
    )
    return 0


def _display_width(text: str) -> str | int:
    """터미널에서 차지하는 칸 수. 한글은 두 칸이다.

    `len()` 으로 재고 채우면 비-ASCII 이메일이 있는 행만 뒤 열이 밀린다. TUI 는 이미
    표시 폭으로 계산하는데(`tui._width`) `list` 는 안 하고 있었다. 그쪽을 그대로 쓰지
    않는 것은 `tui` 가 모듈 최상단에서 `curses` 를 들여오기 때문이다 — `cli` 는 그것
    없이도 돌아야 한다.
    """
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _pad_to(text: str, cols: int) -> str:
    """표시 폭 기준으로 채운다."""
    return text + " " * max(0, cols - _display_width(text))


def _age_text(seconds: int) -> str:
    """낡음의 크기. `~` 하나로는 5 분 전과 5 일 전이 구별되지 않는다.

    그 둘은 신뢰도가 전혀 다르다 — 5 분 전 값은 사실상 지금 값이고, 5 일 전 값은 그
    사이에 한도가 리셋됐을 수도 있는 값이다. 실제로 81%p 틀린 값을 `~` 하나로 보여줬다.
    """
    if seconds >= 86400:
        return f"{seconds // 86400}d"
    if seconds >= 3600:
        return f"{seconds // 3600}h"
    return f"{max(seconds // 60, 1)}m"


def refresh_all(settings: config.Settings) -> None:
    """모든 슬롯을 프로브해 캐시에 남긴다.

    이것이 없는 동안 **비활성 계정은 갱신될 경로가 없었다.** `rotate` 는 활성이 첫 관문
    아래면 지름길로 끝나 후보를 프로브하지 않고(주중 대부분의 호출이 여기다 — 의도된
    절제다), `status --fresh` 는 활성 계정만 읽으며, `list` 는 프로브를 아예 안 한다.
    그래서 비활성 슬롯의 숫자가 며칠이고 굳었다 — 실측으로 93% 라고 적힌 계정이 실제로는
    12% 였다.

    활성 계정은 슬롯 사본이 아니라 **기본 홈**으로 읽는다. 슬롯 사본은 토큰이 갱신되며
    뒤처지고 기본 홈이 언제나 사실이다 (`rotate._home_for` 와 같은 이유).

    한 슬롯이 실패해도 나머지는 계속한다 — 하나가 죽었다고 다른 계정의 숫자까지 잃을
    이유가 없다.
    """
    try:
        codex_bin = str(discovery.resolve_codex_bin())
    except Exception as exc:
        raise CliError(
            f"cannot run codex: {exc}. Check that `codex --version` works, "
            "or set CODEX_ACCOUNT_BIN to its path"
        ) from exc

    active = store.active_label(settings)
    for label in store.labels(settings):
        home = settings.default_home if label == active else store.slot_dir(settings, label)
        try:
            result = probe.probe(codex_bin, str(home))
        except Exception:
            continue
        if result.outcome is not ProbeOutcome.OK or result.usage is None:
            continue
        u = result.usage
        # **읽어 온 것이 정말 그 라벨의 계정인가.** 활성 라벨을 한 번 읽고 그 차례에
        # 기본 홈을 여는 사이, 다른 `rotate` 가 전환을 끝내면 그 홈에는 이미 다른 계정이
        # 들어 있다. 그대로 적으면 남의 사용량이 이 이름으로 캐시에 들어가고, `rotate` 는
        # 그 캐시를 정책 입력으로 읽으므로 후보 선택이 통째로 틀어진다.
        #
        # 이메일을 안 주는 응답도 있으므로, **알 때만** 견준다. 모르는 것을 막으면
        # 갱신이 통째로 멈춘다.
        want = identity.email_of(store.slot_auth(settings, label))
        if u.email is not None and want is not None and u.email != want:
            continue
        cache.write(
            settings,
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


def _expiry_text(expires_at: object, *, now: float | None = None) -> str:
    """쿠폰 만료 시각. `_reset_text` 와 같은 서식이되 **지난 것을 다르게 적는다.**

    사용량 리셋은 지나면 좋은 일(이미 풀렸다)이지만 쿠폰 만료는 지나면 **잃은 것**이다.
    같은 `past` 로 적으면 그 차이가 사라진다.
    """
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        return "-"
    when = datetime.datetime.fromtimestamp(expires_at)
    current = datetime.datetime.now() if now is None else datetime.datetime.fromtimestamp(now)
    secs = int((when - current).total_seconds())
    if secs < 0:
        return f"{when:%m-%d %H:%M} (expired)"
    if secs < 3600:
        rel = f"in {secs // 60}m"
    elif secs < 86400:
        rel = f"in {secs // 3600}h"
    else:
        rel = f"in {secs // 86400}d"
    return f"{when:%m-%d %H:%M} ({rel})"


def _probe_credits(settings: config.Settings) -> list[tuple[str, str, Usage | None]]:
    """슬롯마다 `(라벨, 이메일, 사용량)`. 못 읽은 슬롯은 사용량이 None.

    **캐시를 쓰지 않고 매번 프로브한다.** 쿠폰 상세는 캐시에 없다 — 넣으려면
    `resetCredits` 를 쓰는 여섯 자리에 키를 하나씩 더 얹어야 하고, 그중 한 곳이 빠져서
    캐시 히트일 때만 크레딧이 사라진 적이 이미 있다.

    비용은 사용자가 이 명령을 직접 쳤을 때만 든다. `list` 와 `rotate` 의 값싼 경로는
    그대로다.
    """
    try:
        codex_bin = str(discovery.resolve_codex_bin())
    except Exception as exc:
        raise CliError(
            f"cannot run codex: {exc}. Check that `codex --version` works, "
            "or set CODEX_ACCOUNT_BIN to its path"
        ) from exc

    active = store.active_label(settings)
    out: list[tuple[str, str, Usage | None]] = []
    for label in store.labels(settings):
        email = identity.email_of(store.slot_auth(settings, label)) or "?"
        home = settings.default_home if label == active else store.slot_dir(settings, label)
        usage: Usage | None = None
        try:
            result = probe.probe(codex_bin, str(home))
        except Exception:
            result = None
        if result is not None and result.outcome is ProbeOutcome.OK and result.usage is not None:
            u = result.usage
            # `refresh_all` 과 같은 신원 확인. 조회 도중 다른 rotate 가 전환을 끝내면
            # 기본 홈에 이미 다른 계정이 들어 있어, 남의 쿠폰을 이 이름으로 적게 된다.
            want = identity.email_of(store.slot_auth(settings, label))
            if u.email is None or want is None or u.email == want:
                usage = u
        out.append((label, email, usage))
    return out


def cmd_credits(settings: config.Settings) -> int:
    """계정별 리셋 쿠폰 — 개수·상태·만료.

    `list` 의 `CRED` 열은 개수만 말한다. 쿠폰은 **만료되는 자원**이라 그것만으로는 쓸지
    말지를 정할 수 없다 — 오늘 밤 사라질 쿠폰과 두 달 남은 쿠폰은 같은 `1` 로 보인다.
    """
    labels = store.labels(settings)
    if not labels:
        print("No accounts yet. Start with: codex-swap adopt <label>")
        return 0

    active = store.active_label(settings)
    rows: list[tuple[str, str, str, str, str]] = []
    unreadable: list[str] = []
    for label, email, usage in _probe_credits(settings):
        mark = "*" if label == active else " "
        if usage is None:
            unreadable.append(label)
            rows.append((mark, label, email, "?", "-"))
            continue
        if not usage.credits:
            # 개수는 아는데 목록이 비었을 수 있다 — 서버가 상세를 안 줄 때다. 0 으로
            # 단정하지 않고 아는 것만 적는다.
            count = "-" if usage.reset_credits is None else str(usage.reset_credits)
            rows.append((mark, label, email, count, "-"))
            continue
        for i, credit in enumerate(usage.credits):
            title = credit.title or credit.status or "credit"
            rows.append(
                (
                    mark if i == 0 else " ",
                    label if i == 0 else "",
                    email if i == 0 else "",
                    title if credit.status == "available" else f"{title} ({credit.status})",
                    _expiry_text(credit.expires_at),
                )
            )

    def _w(values: list[str], header: str, lo: int) -> int:
        widths = [_display_width(v) for v in values]
        return max(lo, _display_width(header), *widths) if widths else lo

    lw = _w([r[1] for r in rows], "LABEL", 6)
    ew = _w([r[2] for r in rows], "EMAIL", 12)
    cw = _w([r[3] for r in rows], "CREDIT", 6)

    def _row(mark: str, label: str, email: str, credit: str, expires: str) -> str:
        cells = [_pad_to(mark, 3), _pad_to(label, lw), _pad_to(email, ew), _pad_to(credit, cw)]
        return " ".join([*cells, expires]).rstrip()

    print(_row("", "LABEL", "EMAIL", "CREDIT", "EXPIRES"))
    for row in rows:
        print(_row(*row))
    print()
    if unreadable:
        print(f"could not read: {', '.join(unreadable)}. Try: codex-swap status --fresh")
    print("A credit resets that account's usage window. Spending one cannot be undone.")
    return 0


def cmd_credits_json(settings: config.Settings) -> int:
    """`credits` 의 기계용 판. **이 모양이 계약이다.**

    `id` 를 싣는 것은 나중에 쿠폰을 지목해 쓰기 위해서다. 다만 이 출력은 **그 순간의
    조회 결과**이지 저장된 값이 아니다 — 쿠폰을 쓸 때는 다시 조회해야 한다.
    """
    active = store.active_label(settings)
    accounts = []
    for label, email, usage in _probe_credits(settings):
        accounts.append(
            {
                "label": label,
                "email": email if email != "?" else None,
                "active": label == active,
                "availableCount": None if usage is None else usage.reset_credits,
                "readable": usage is not None,
                "credits": []
                if usage is None
                else [
                    {
                        "id": c.id,
                        "status": c.status,
                        "title": c.title,
                        "grantedAt": c.granted_at,
                        "expiresAt": c.expires_at,
                    }
                    for c in usage.credits
                ],
            }
        )
    print(json.dumps({"active": active, "accounts": accounts}, indent=2))
    return 0


def cmd_list(settings: config.Settings, *, fresh: bool = False) -> int:
    labels = store.labels(settings)
    if not labels:
        print("No accounts yet. Start with: codex-swap adopt <label>")
        return 0
    if fresh:
        refresh_all(settings)
    active = store.active_label(settings)
    # 행을 먼저 모으고 폭을 잰 뒤에 찍는다. 고정폭이면 `~93% 5h` 처럼 긴 값이 칸을
    # 넘쳐 뒤 열이 통째로 밀린다 — 낡음 나이를 붙이면서 실제로 그랬다.
    printed: list[tuple[str, str, str, str, str]] = []
    reset_of: dict[str, str] = {}
    stale_seen: list[str] = []
    seen_emails: dict[str, list[str]] = {}
    # 소진 판정에 쓸 재료. `list` 는 프로브를 돌리지 않으므로 **아는 것만으로** 판단한다.
    known_pct: list[int] = []
    resets: list[int] = []
    credits_total = 0
    for label in labels:
        email = identity.email_of(store.slot_auth(settings, label)) or "?"
        if email != "?":
            seen_emails.setdefault(email, []).append(label)
        # 프로브를 돌리지 않는 것은 의도다 — `list` 는 네트워크를 타지 않는 조회여야
        # 매 호출이 싸다. 신선한 값이 필요하면 `status --fresh`.
        #
        # 다만 TTL 이 지났다고 물음표를 찍지는 않는다. 그건 "읽지 못했다" 가 아니라
        # "5 분 지났다" 이고, 사람은 그 둘을 구별할 방법이 없어 토큰이 끊긴 줄 안다.
        # 낡은 값은 `~` 를 붙여 그대로 보여 준다 — 디스크만 읽으므로 비용은 그대로다.
        cached = cache.read(settings, label)
        stale = False
        age = 0
        if cached is None:
            aged = cache.read_stale(settings, label)
            if aged is not None:
                cached, age, stale = aged[0], aged[1], True
        cred = "-"
        if cached is not None and "usedPercent" in cached:
            used = f"{'~' if stale else ''}{cached['usedPercent']}%"
            if stale:
                used = f"{used} {_age_text(age)}"
            reset = _reset_text(cached.get("resetsAt"))
            if stale:
                stale_seen.append(label)
            pct = cached.get("usedPercent")
            if isinstance(pct, int):
                known_pct.append(pct)
            at = cached.get("resetsAt")
            if isinstance(at, (int, float)) and not isinstance(at, bool):
                resets.append(int(at))
            # 0 과 "모른다" 는 다르다. 0 으로 채우면 없는 크레딧을 없다고 **단정**한다.
            raw = cached.get("resetCredits")
            if isinstance(raw, int) and not isinstance(raw, bool):
                cred = str(raw)
                credits_total += raw
        else:
            used, reset = "?", "-"
        mark = "*" if label == active else " "
        printed.append((mark, label, email, used, cred))
        reset_of[label] = reset

    def _w(values: list[str], header: str, lo: int) -> int:
        widths = [_display_width(v) for v in values]
        return max(lo, _display_width(header), *widths) if widths else lo

    lw = _w([r[1] for r in printed], "LABEL", 6)
    ew = _w([r[2] for r in printed], "EMAIL", 12)
    uw = _w([r[3] for r in printed], "USED", 4)
    cw = _w([r[4] for r in printed], "CRED", 4)

    def _row(mark: str, label: str, email: str, used: str, cred: str, reset: str) -> str:
        cells = [
            _pad_to(mark, 3),
            _pad_to(label, lw),
            _pad_to(email, ew),
            _pad_to(used, uw),
            _pad_to(cred, cw),
            reset,
        ]
        return " ".join(cells).rstrip()

    print(_row("", "LABEL", "EMAIL", "USED", "CRED", "RESET"))
    for mark, label, email, used, cred in printed:
        print(_row(mark, label, email, used, cred, reset_of[label]))
    print()
    # 낡은 행이 있을 때만 범례를 낸다. 늘 떠 있는 안내는 곧 안 읽힌다.
    if stale_seen:
        print("~ marks a stale cached value, with its age. To refresh: codex-swap list --fresh")

    # 같은 이메일이 두 라벨에 있으면 **진 쪽 사본은 갱신을 못 받고 썩는다.** 활성 판정은
    # 정렬 첫 일치가 이기므로(`store.active_label`) 나머지는 sync-back 대상이 아니다.
    # 화면에 그 사실이 없으면 사용자는 두 줄이 그냥 둘인 줄 알고, 나중에 쓰려는 순간
    # 만료된 토큰을 만난다.
    for email, owners in sorted(seen_emails.items()):
        if len(owners) > 1:
            kept = active if active in owners else owners[0]
            rotting = [x for x in owners if x != kept]
            print(
                f"note: {', '.join(owners)} hold the same account ({email}). "
                f"Only '{kept}' is kept up to date; {', '.join(rotting)} will go stale"
            )

    ladder = ",".join(str(x) for x in settings.ladder)
    print(
        f"ladder {ladder} · margin {settings.margin}%p · "
        f"cache {settings.cache_ttl}s · cooldown {settings.cooldown}s"
    )
    # 전 계정이 사다리 끝을 넘었으면 자동 전환이 할 수 있는 일이 없다. 그 사실도, 언제
    # 풀리는지도, 크레딧이 남았다는 것도 화면 어디에도 없었다 — `rotate` 는 침묵하고
    # `--dry-run` 만 `ladder exhausted` 라고 답한다. 사용자는 99% 두 줄만 보고 무엇을
    # 기다려야 하는지 모른다.
    #
    # 아는 값만으로 판단한다. `list` 는 프로브를 돌리지 않으므로 모르는 슬롯이 있으면
    # 단정하지 않는다 — 그쪽이 멀쩡할 수 있다.
    top = settings.ladder[-1] if settings.ladder else None
    if (
        top is not None
        and known_pct
        and len(known_pct) == len(labels)
        and all(p >= top for p in known_pct)
    ):
        soonest = f" Earliest reset {_reset_text(min(resets))}." if resets else ""
        spend = (
            f" {credits_total} reset credit(s) left — spending one is the other way out."
            if credits_total > 0
            else ""
        )
        print(
            f"note: every account is past {top}%, so there is nothing to switch to.{soonest}{spend}"
        )

    # 위 줄이 방금 보여 준 값이 **저장한 값이 아닐 수 있다.** 그 사실을 바로 아래 붙인다.
    broken = config.file_config_error(settings.accounts_dir)
    if broken is not None:
        print(f"warning: the saved policy is being ignored — {broken}")
    if settings.off_switch.exists():
        print(f"automatic switching: off ({settings.off_switch})")
    return 0


def _status_json(active: str | None, email: str | None, usage: Usage | None) -> int:
    """실패도 **데이터로** 낸다. 스크립트가 stderr 문구를 읽게 두면 안 된다."""
    print(
        json.dumps(
            {
                "ok": usage is not None,
                "active": active,
                "email": email if usage is None else (usage.email or email),
                "usedPercent": None if usage is None else usage.used_percent,
                "planType": None if usage is None else usage.plan_type,
                "resetsAt": None if usage is None else usage.resets_at,
                "resetCredits": None if usage is None else usage.reset_credits,
                "reached": None if usage is None else usage.reached,
            },
            indent=2,
        )
    )
    return 0 if usage is not None else 1


def cmd_status(settings: config.Settings, *, fresh: bool, as_json: bool = False) -> int:
    active = store.active_label(settings)
    live_email = identity.email_of(store.active_auth(settings))

    # 자격증명이 아예 없으면 프로브는 실패할 수밖에 없다. 그 실패를 "조회 실패" 로
    # 보여 주면 방금 설치한 사용자는 도구가 깨진 줄 안다 — 실제로는 아직 아무것도 안 한
    # 상태다. 실패의 종류를 늘어놓지 않는다는 원칙(아래)과 다른 얘기다: 여기서는 애초에
    # 물어볼 것이 없다는 것을 알고 있으므로, 묻지 않고 다음 행동을 말해 준다.
    if not store.active_auth(settings).is_file():
        if as_json:
            return _status_json(active, None, None)
        print("active account: not logged in")
        print("Run codex login first, then codex-swap adopt <label> to keep it.")
        return 1

    if not as_json:
        if active is None:
            print(f"active account: {live_email or 'email unknown'} (not in any slot)")
        else:
            print(f"active account: {active}")

    if not fresh and active is not None:
        cached = cache.read(settings, active)
        if cached is not None and "usedPercent" in cached:
            if as_json:
                return _status_json(active, live_email, _usage_from_cache(cached))
            print(_usage_line(_usage_from_cache(cached)))
            return 0

    # 활성이 어느 슬롯에도 없으면 홈을 직접 고른다. bash 는 이 경우 가짜 라벨
    # `__active__` 를 경로에 넣어 `CODEX_HOME=<root>/__active__` 로 프로브를 돌리는데,
    # 자격증명은 실제로 기본 홈에 있으므로 그 파생은 결함이다 (설계문 §7.5 D3).
    home = settings.default_home if active is None else store.slot_dir(settings, active)

    # 바이너리 해석을 프로브 호출의 **인자 안**에 두면 그 실패가 아래 `except` 에 걸려
    # "사용량 조회 실패" 로 접힌다. 그러면 codex 가 아예 없는 기기에서도 화면은 계정을
    # 의심하게 만든다 — 고쳐야 할 것이 전혀 다른데 말이 같다. 설치 문제는 설치 문제로
    # 말해야 다음 행동이 정해진다.
    try:
        codex_bin = discovery.resolve_codex_bin()
    except Exception as exc:
        if as_json:
            return _status_json(active, live_email, None)
        print(f"cannot run codex: {exc}")
        print("Check that `codex --version` works, or set CODEX_ACCOUNT_BIN to its path.")
        return 1

    try:
        result = probe.probe(str(codex_bin), str(home))
    except Exception:
        # 여기서부터는 한 줄로만 알린다. bash 도 프로브의 모든 비인증 실패를 이 한 줄로
        # 접는다 — 사용자에게 유용한 것은 "왜 실패했는가" 가 아니라 "지금 모른다" 다.
        if as_json:
            return _status_json(active, live_email, None)
        print("usage: probe failed")
        return 1
    if result.outcome is ProbeOutcome.OK and result.usage is not None:
        # 읽었으면 남긴다. `list` 는 "새로 읽으려면 status --fresh" 라고 안내하는데, 그
        # 값을 버리면 안내를 따라도 표가 그대로 `?` 다 — 캐시에 쓰는 곳이 `rotate` 뿐이라
        # 실제로 그랬다. 라벨을 아는 경우에만 쓸 수 있다: 활성이 어느 슬롯과도 안 맞으면
        # 그 값을 **어느 라벨의 것으로도** 적을 수 없다.
        #
        # 실패는 조용하다 — 캐시를 못 쓴 대가는 다음 호출의 프로브 한 번이다.
        if active is not None:
            u = result.usage
            cache.write(
                settings,
                active,
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
        if as_json:
            return _status_json(active, live_email, result.usage)
        print(_usage_line(result.usage))
        return 0
    if as_json:
        return _status_json(active, live_email, None)
    print("usage: probe failed")
    return 1


def cmd_use(settings: config.Settings, label: str, *, force: bool = False) -> int:
    if not store.label_syntax_ok(label):
        raise CliError(f"not a usable label: {label}")

    # 전환은 활성 자격증명을 슬롯으로 되돌려 놓고(sync-back) 바꾼다. 그런데 활성이 어느
    # 슬롯과도 안 맞으면 되돌려 놓을 자리가 없어 **그냥 사라진다** (`store.switch` 가
    # `active_label is None` 이면 sync-back 을 건너뛴다). 사용자가 손으로 `codex login`
    # 한 계정이 그 경우이고, 잃으면 브라우저 재로그인 말고는 복구가 없다.
    #
    # TUI 는 이 상태에 전용 경고줄을 띄운다 — 대화형이라 사용자가 그것을 보고 enter 를
    # 누르면 동의한 것이다. CLI 는 볼 기회 없이 실행되므로 거부하는 편이 맞다. 버리는
    # 것이 뜻인 경우(임시 로그인)를 위해 `--force` 를 둔다.
    live = store.active_auth(settings)
    if not force and live.is_file() and store.active_label(settings) is None:
        who = identity.email_of(live) or "unknown account"
        raise CliError(
            f"the active account ({who}) is not in any slot; switching will not keep it. "
            "Adopt it first (codex-swap adopt <label>), or pass --force to discard it"
        )

    with store.switch_lock(settings):
        store.switch(settings, label, "manual")
    print(
        f"switched to {label}. "
        "A codex session that is already running keeps the old token until you restart it."
    )
    return 0


def cmd_rename(settings: config.Settings, old: str, new: str) -> int:
    """슬롯 이름을 바꾼다.

    지금까지 이름을 바꾸려면 `use` -> `adopt <새 이름>` -> `remove <옛 이름>` 을 거쳐야
    했고, 비활성 계정은 그 우회로도 안 돼 손으로 `mv` 하는 수밖에 없었다. **파괴적
    명령을 이름 바꾸기의 필수 단계로 두면 안 된다** — 중간에 멈추면 자격증명이 사라진다.

    락을 잡는 이유는 이 연산이 슬롯 디렉토리를 움직이기 때문이다. 전환이 같은 순간에
    그 디렉토리를 읽으면 반쯤 옮겨진 상태를 본다.
    """
    for name in (old, new):
        if not store.label_syntax_ok(name):
            raise CliError(f"not a usable label: {name}")
    src = store.slot_dir(settings, old)
    if not src.is_dir() or src.is_symlink():
        raise CliError(f"no such label: {old}")
    dst = store.slot_dir(settings, new)
    if dst.exists():
        # 덮어쓰면 그 계정의 보관본이 사라진다 — `adopt` 와 같은 종류의 손실이다.
        raise CliError(f"label already exists: {new} (use remove to drop it first)")

    with store.switch_lock(settings):
        os.rename(src, dst)
    # 캐시는 라벨로만 색인된다. 옛 이름의 숫자가 남으면 그 이름을 재사용할 때 `remove`
    # 에서와 같은 오염이 생긴다. 파일째 버리는 것도 그쪽과 같다.
    cache.clear(settings)
    print(f"renamed {old} -> {new}")
    return 0


def cmd_remove(settings: config.Settings, label: str, *, assume_yes: bool = False) -> int:
    if not store.label_syntax_ok(label):
        raise CliError(f"not a usable label: {label}")
    target = store.slot_dir(settings, label)
    if not target.is_dir() or target.is_symlink():
        raise CliError(f"no such label: {label}")
    was_active = store.active_label(settings) == label

    # 자격증명 삭제는 되돌릴 수 없다. 사람이 보고 있으면 한 번 묻는다 — 무엇을 지우는지
    # 이메일까지 보여 주고서다. 라벨만으로는 어느 계정인지 확신할 수 없다.
    #
    # tty 가 아니면 묻지 않는다. 파이프 뒤에서 물으면 영영 끝나지 않는다.
    if not assume_yes and sys.stdin.isatty():
        who = identity.email_of(store.slot_auth(settings, label)) or "email unknown"
        print(f"About to delete slot '{label}' ({who}). This cannot be undone.")
        try:
            answer = input("Type y to continue: ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in {"y", "yes"}:
            print("Left it alone.")
            return 1

    shutil.rmtree(target)
    # 캐시는 라벨로만 색인된다 — 어느 계정의 숫자인지는 적혀 있지 않다. 항목을 남기면
    # `adopt <같은 라벨>` 로 다른 계정을 그 이름에 넣었을 때 새 계정이 지운 계정의
    # 사용량을 최대 한 TTL 뒤집어쓴다. 표시만의 문제가 아니다: `rotate` 도 이 캐시를
    # 정책 입력으로 읽으므로(`rotate._usage_of`) 후보 선택이 통째로 틀어진다.
    #
    # 한 키만 빼지 않고 파일째 버리는 것은 `store.switch` 와 같다. 남는 항목도 어차피
    # TTL 안에서만 유효하고, 대가는 다음 rotate 의 프로브 몇 번뿐이다.
    cache.clear(settings)
    print(f"removed {label}")
    # 지운 것이 **활성 라벨**이면 자동 전환이 이 순간부터 영구 무동작이다 — 이후 rotate
    # 는 `active account is not a registered slot` 으로 끝나는데 그 사유는 `--dry-run`
    # 에서만 보인다. 여기서 말하지 않으면 사용자는 며칠 뒤에 "왜 안 바뀌지" 로 만난다.
    if was_active:
        print(
            "Note: that was the account you are using, so it is now in no slot. "
            "Automatic switching stops until you run: codex-swap adopt <label>"
        )
    return 0


def cmd_clean(settings: config.Settings) -> int:
    """슬롯의 프로브 부산물을 지운다. `auth.json` 은 보존한다."""
    for label in store.labels(settings):
        for entry in store.slot_dir(settings, label).iterdir():
            if entry.name == "auth.json":
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
    cache.clear(settings)
    print("Cleared probe leftovers from the slots (auth.json kept).")
    return 0


def cmd_rotate(settings: config.Settings, *, dry_run: bool) -> int:
    """정책 실행. 평상시에는 **아무것도 출력하지 않는다.**"""
    decision = rotate.rotate(settings, dry_run=dry_run)
    origin = decision.from_label or "(logged out)" if isinstance(decision, Switched) else ""
    if dry_run:
        if isinstance(decision, Switched):
            print(f"would switch: {origin} -> {decision.to_label} [{decision.reason}]")
        else:
            print(f"no switch: {decision.reason}")
    elif isinstance(decision, Switched):
        # 전환이 실제로 일어난 경우에만 한 줄. 이 줄은 사용자 터미널에 그대로 실린다.
        print(f"codex-swap: {origin} -> {decision.to_label}", file=sys.stderr)
    return decision_exit_code(decision)


# ── 진입점 ───────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-swap",
        description="Keep several Codex accounts and swap between them as usage climbs.",
        # 주 화면이 TUI 인데 도움말이 그것을 말하지 않으면, 인자 없이 실행해 볼 생각을
        # 하지 않은 사용자는 이 도구에 화면이 있다는 것을 모른 채로 쓴다.
        epilog=(
            "With no arguments this opens a TUI (list, usage bars, policy editor). "
            "Called from a pipe or a script it prints this help instead.\n"
            "To see why an automatic switch did not happen: codex-swap rotate --dry-run"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"codex-swap {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p = sub.add_parser("adopt", help="store the account you are logged in as")
    p.add_argument("label")

    p = sub.add_parser("add", help="log in to a new slot (opens a browser)")
    p.add_argument("label")
    p.add_argument(
        "--force", action="store_true", help="log in again even if the label already exists"
    )

    p = sub.add_parser("list", aliases=["ls"], help="stored accounts and cached usage")
    p.add_argument("--fresh", action="store_true", help="probe every slot before printing")
    p.add_argument("--json", action="store_true", help="machine-readable output")

    p = sub.add_parser("credits", help="usage-reset credits per account, with expiry")
    p.add_argument("--json", action="store_true", help="machine-readable output")

    p = sub.add_parser("status", help="active account and its usage")
    p.add_argument("--fresh", action="store_true", help="ignore the cache and probe now")
    p.add_argument("--json", action="store_true", help="machine-readable output")

    p = sub.add_parser("use", aliases=["switch"], help="switch by hand")
    p.add_argument("label")
    p.add_argument(
        "--force",
        action="store_true",
        help="switch even if the active account is in no slot (its credentials are lost)",
    )

    p = sub.add_parser("rotate", help="run the policy")
    p.add_argument("--dry-run", action="store_true", help="decide and report, change nothing")

    p = sub.add_parser("rename", help="give a slot a different name")
    p.add_argument("old")
    p.add_argument("new")

    p = sub.add_parser("remove", aliases=["rm"], help="delete a slot")
    p.add_argument("label")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")

    sub.add_parser("clean", help="clear probe leftovers from the slots")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        # 인자 없이 부르면 TUI 로 간다. 단, **TTY 일 때만** — 파이프나 스크립트에서
        # 부르면 대화형 화면이 걸려 영영 안 끝난다. 그 경우엔 지금까지처럼 도움말이다.
        if sys.stdin.isatty() and sys.stdout.isatty():
            from codex_swap import tui

            try:
                settings = config.load()
            except config.ConfigError as exc:
                print(f"codex-swap: {exc}", file=sys.stderr)
                return 1
            return tui.run(settings)
        parser.print_help()
        return 0

    try:
        settings = config.load()
    except config.ConfigError as exc:
        # rotate 는 fail-open 이다. 설정이 깨졌다고 여기서 시끄럽게 죽으면 그 출력이
        # 매 codex 호출에 실린다 — 조용히 무동작으로 끝낸다.
        #
        # **`--dry-run` 은 예외다.** 그 침묵은 wrapper 가 부르는 핫패스를 위한 것인데,
        # `--dry-run` 은 사람이 "왜 안 바뀌나" 를 물으려고 직접 친 명령이다. 원인이
        # 설정일 때만 그 유일한 진단 도구가 입을 닫으면, 가장 알고 싶은 순간에 아무
        # 답도 없다.
        if args.command == "rotate" and not getattr(args, "dry_run", False):
            return 1
        print(f"codex-swap: {exc}", file=sys.stderr)
        return 1

    try:
        match args.command:
            case "adopt":
                return cmd_adopt(settings, args.label)
            case "add":
                return cmd_add(settings, args.label, force=args.force)
            case "list" | "ls":
                if args.json:
                    if args.fresh:
                        refresh_all(settings)
                    return cmd_list_json(settings)
                return cmd_list(settings, fresh=args.fresh)
            case "credits":
                return cmd_credits_json(settings) if args.json else cmd_credits(settings)
            case "status":
                return cmd_status(settings, fresh=args.fresh, as_json=args.json)
            case "use" | "switch":
                return cmd_use(settings, args.label, force=args.force)
            case "rotate":
                return cmd_rotate(settings, dry_run=args.dry_run)
            case "rename":
                return cmd_rename(settings, args.old, args.new)
            case "remove" | "rm":
                return cmd_remove(settings, args.label, assume_yes=args.yes)
            case "clean":
                return cmd_clean(settings)
            case _:
                raise CliError(f"unknown command: {args.command}")
    except CliError as exc:
        print(f"codex-swap: {exc}", file=sys.stderr)
        return 1
    except store.LockBusy as exc:
        # `LockBusy` 는 경로만 들고 온다. 그대로 찍으면 화면에 파일 경로 한 줄이 남고,
        # 사용자는 그것이 오류인지 안내인지도 모른다. TUI 는 같은 상황에 "다른 전환이
        # 진행 중이다. 잠시 뒤 다시 눌러라" 라고 말하는데 그 문구가 CLI 로 오지 않았다.
        print(
            f"codex-swap: another switch is in progress ({exc}). Try again in a moment",
            file=sys.stderr,
        )
        return 1
    except (store.StoreError, store.LockUnusable) as exc:
        print(f"codex-swap: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        # 안전망. `OSError` 는 이 도구에서 거의 전부 **환경 문제**다 — 권한, 없는 경로,
        # 실행할 수 없는 바이너리, 꽉 찬 디스크. 그것을 raw traceback 으로 보여 주면
        # 사용자는 프로그램 결함으로 읽고 자기 환경을 보지 않는다. 넓은 `Exception` 을
        # 잡지 않는 이유는 그쪽은 실제로 우리 결함이고, 그때는 역추적이 필요하기 때문이다.
        print(f"codex-swap: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
