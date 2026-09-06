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
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
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
        rel = "지남"
    elif secs < 3600:
        rel = f"{secs // 60}분 뒤"
    elif secs < 86400:
        rel = f"{secs // 3600}시간 뒤"
    else:
        rel = f"{secs // 86400}일 뒤"
    return f"{when:%m-%d %H:%M} ({rel})"


def _usage_line(u: Usage) -> str:
    return (
        f"사용량: {u.used_percent}% "
        f"(primary {_opt(u.primary_percent)}%, secondary {_opt(u.secondary_percent)}%) "
        f"· plan {_opt(u.plan_type)} · 리셋 {_reset_text(u.resets_at)}"
    )


def _usage_from_cache(d: dict[str, Any]) -> Usage:
    return Usage(
        used_percent=int(d["usedPercent"]),
        email=d.get("email"),
        plan_type=d.get("planType"),
        primary_percent=d.get("primaryPercent"),
        secondary_percent=d.get("secondaryPercent"),
        resets_at=d.get("resetsAt"),
        reached=bool(d.get("reached")),
    )


# ── 명령 ─────────────────────────────────────────────────────────────────────


def cmd_adopt(settings: config.Settings, label: str) -> int:
    if not store.label_syntax_ok(label):
        raise CliError(f"쓸 수 없는 라벨이다: {label}")
    live = store.active_auth(settings)
    if not live.is_file():
        raise CliError(f"로그인 상태가 아니다 ({live} 없음)")
    paths.ensure_root(settings)
    slot = store.slot_dir(settings, label)
    slot.mkdir(mode=0o700, parents=True, exist_ok=True)
    slot.chmod(0o700)
    dest = store.slot_auth(settings, label)
    shutil.copy2(live, dest)
    dest.chmod(0o600)
    print(f"등록: {label} ({identity.email_of(dest) or '이메일 불명'})")
    return 0


def cmd_add(
    settings: config.Settings,
    label: str,
    *,
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
        raise CliError(f"쓸 수 없는 라벨이다: {label}")
    if store.slot_auth(settings, label).exists():
        raise CliError(f"이미 있는 라벨이다: {label} (지우려면 remove)")

    try:
        codex_bin = discovery.resolve_codex_bin()
    except Exception as exc:
        raise CliError(f"codex 바이너리를 찾지 못했다: {exc}") from exc

    paths.ensure_root(settings)
    slot = store.slot_dir(settings, label)
    slot.mkdir(mode=0o700, parents=True, exist_ok=True)
    slot.chmod(0o700)

    print(f"새 슬롯 '{label}' 에 로그인한다. 지금 활성 계정과 **다른** 계정으로 로그인하라.")

    # `CODEX_ROTATE_SKIP=1` 이 없으면 이 로그인이 띄우는 codex 가 wrapper 를 거쳐 다시
    # rotate 를 부르고, 그 rotate 가 지금 만들고 있는 슬롯을 후보로 본다. `CODEX_HOME` 이
    # 슬롯을 가리키므로 rotate 의 홈 가드에도 걸리지만, 두 겹으로 막는다.
    env = discovery.env_with_bin_dir(codex_bin)
    env["CODEX_ROTATE_SKIP"] = "1"
    env["CODEX_HOME"] = str(slot)

    run = runner or _run_login
    if run([str(codex_bin), "login"], env) != 0:
        raise CliError("로그인 실패")

    dest = store.slot_auth(settings, label)
    if not dest.is_file():
        raise CliError("로그인은 끝났는데 auth.json 이 생기지 않았다")
    dest.chmod(0o600)
    print(f"등록: {label} ({identity.email_of(dest) or '이메일 불명'})")
    return 0


def _run_login(argv: list[str], env: dict[str, str]) -> int:
    """브라우저 로그인은 대화형이라 stdio 를 그대로 물려준다."""
    return subprocess.call(argv, env=env)


def cmd_list(settings: config.Settings) -> int:
    labels = store.labels(settings)
    if not labels:
        print("등록된 계정이 없다. 먼저: codex-swap adopt <label>")
        return 0
    active = store.active_label(settings)
    print(f"{'':<3} {'LABEL':<14} {'EMAIL':<30} {'USED':<6} RESET")
    stale_seen: list[str] = []
    for label in labels:
        email = identity.email_of(store.slot_auth(settings, label)) or "?"
        # 프로브를 돌리지 않는 것은 의도다 — `list` 는 네트워크를 타지 않는 조회여야
        # 매 호출이 싸다. 신선한 값이 필요하면 `status --fresh`.
        #
        # 다만 TTL 이 지났다고 물음표를 찍지는 않는다. 그건 "읽지 못했다" 가 아니라
        # "5 분 지났다" 이고, 사람은 그 둘을 구별할 방법이 없어 토큰이 끊긴 줄 안다.
        # 낡은 값은 `~` 를 붙여 그대로 보여 준다 — 디스크만 읽으므로 비용은 그대로다.
        cached = cache.read(settings, label)
        stale = False
        if cached is None:
            aged = cache.read_stale(settings, label)
            if aged is not None:
                cached, stale = aged[0], True
        if cached is not None and "usedPercent" in cached:
            used = f"{'~' if stale else ''}{cached['usedPercent']}%"
            reset = _reset_text(cached.get("resetsAt"))
            if stale:
                stale_seen.append(label)
        else:
            used, reset = "?", "-"
        mark = "*" if label == active else " "
        print(f"{mark:<3} {label:<14} {email:<30} {used:<6} {reset}")
    print()
    # 낡은 행이 있을 때만 범례를 낸다. 늘 떠 있는 안내는 곧 안 읽힌다.
    if stale_seen:
        print("~ 는 캐시가 낡았다는 표시다. 새로 읽으려면: codex-swap status --fresh")
    ladder = ",".join(str(x) for x in settings.ladder)
    print(
        f"사다리 {ladder} · 마진 {settings.margin}%p · "
        f"캐시 {settings.cache_ttl}s · 쿨다운 {settings.cooldown}s"
    )
    if settings.off_switch.exists():
        print(f"자동 전환: 꺼짐 ({settings.off_switch})")
    return 0


def cmd_status(settings: config.Settings, *, fresh: bool) -> int:
    active = store.active_label(settings)
    if active is None:
        email = identity.email_of(store.active_auth(settings)) or "로그인 안 됨"
        print(f"활성 계정: {email} (슬롯 미등록)")
    else:
        print(f"활성 계정: {active}")

    if not fresh and active is not None:
        cached = cache.read(settings, active)
        if cached is not None and "usedPercent" in cached:
            print(_usage_line(_usage_from_cache(cached)))
            return 0

    # 활성이 어느 슬롯에도 없으면 홈을 직접 고른다. bash 는 이 경우 가짜 라벨
    # `__active__` 를 경로에 넣어 `CODEX_HOME=<root>/__active__` 로 프로브를 돌리는데,
    # 자격증명은 실제로 기본 홈에 있으므로 그 파생은 결함이다 (설계문 §7.5 D3).
    home = settings.default_home if active is None else store.slot_dir(settings, active)
    try:
        result = probe.probe(str(discovery.resolve_codex_bin()), str(home))
    except Exception:
        # 조회 실패는 한 줄로만 알린다. bash 도 프로브의 모든 비인증 실패를 이 한 줄로
        # 접는다 — 사용자에게 유용한 것은 "왜 실패했는가" 가 아니라 "지금 모른다" 다.
        print("사용량: 조회 실패")
        return 1
    if result.outcome is ProbeOutcome.OK and result.usage is not None:
        print(_usage_line(result.usage))
        return 0
    print("사용량: 조회 실패")
    return 1


def cmd_use(settings: config.Settings, label: str) -> int:
    if not store.label_syntax_ok(label):
        raise CliError(f"쓸 수 없는 라벨이다: {label}")
    with store.switch_lock(settings):
        store.switch(settings, label, "manual")
    print(
        f"전환했다: {label}. "
        "떠 있는 브로커는 옛 토큰을 들고 있으니 다음 프롬프트에서 자동 재시작된다."
    )
    return 0


def cmd_remove(settings: config.Settings, label: str) -> int:
    if not store.label_syntax_ok(label):
        raise CliError(f"쓸 수 없는 라벨이다: {label}")
    target = store.slot_dir(settings, label)
    if not target.is_dir() or target.is_symlink():
        raise CliError(f"없는 라벨이다: {label}")
    shutil.rmtree(target)
    print(f"삭제: {label}")
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
    print("슬롯의 프로브 부산물을 지웠다 (auth.json 은 보존).")
    return 0


def cmd_rotate(settings: config.Settings, *, dry_run: bool) -> int:
    """정책 실행. 평상시에는 **아무것도 출력하지 않는다.**"""
    decision = rotate.rotate(settings, dry_run=dry_run)
    origin = decision.from_label or "(로그아웃)" if isinstance(decision, Switched) else ""
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
        description="Codex 계정을 여러 개 보관하고 사용량에 따라 갈아끼운다.",
    )
    parser.add_argument("--version", action="version", version=f"codex-swap {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p = sub.add_parser("adopt", help="지금 로그인된 계정을 슬롯에 등록")
    p.add_argument("label")

    p = sub.add_parser("add", help="새 슬롯에 로그인")
    p.add_argument("label")

    sub.add_parser("list", aliases=["ls"], help="등록된 계정과 캐시된 사용량")

    p = sub.add_parser("status", help="활성 계정과 사용량")
    p.add_argument("--fresh", action="store_true", help="캐시를 무시하고 다시 조회")

    p = sub.add_parser("use", aliases=["switch"], help="수동 전환")
    p.add_argument("label")

    p = sub.add_parser("rotate", help="정책 실행")
    p.add_argument("--dry-run", action="store_true", help="판단만 하고 바꾸지 않는다")

    p = sub.add_parser("remove", aliases=["rm"], help="슬롯 삭제")
    p.add_argument("label")

    sub.add_parser("clean", help="슬롯의 프로브 부산물 정리")
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
        if args.command == "rotate":
            return 1
        print(f"codex-swap: {exc}", file=sys.stderr)
        return 1

    try:
        match args.command:
            case "adopt":
                return cmd_adopt(settings, args.label)
            case "add":
                return cmd_add(settings, args.label)
            case "list" | "ls":
                return cmd_list(settings)
            case "status":
                return cmd_status(settings, fresh=args.fresh)
            case "use" | "switch":
                return cmd_use(settings, args.label)
            case "rotate":
                return cmd_rotate(settings, dry_run=args.dry_run)
            case "remove" | "rm":
                return cmd_remove(settings, args.label)
            case "clean":
                return cmd_clean(settings)
            case _:
                raise CliError(f"모르는 명령: {args.command}")
    except CliError as exc:
        print(f"codex-swap: {exc}", file=sys.stderr)
        return 1
    except (store.StoreError, store.LockBusy, store.LockUnusable) as exc:
        print(f"codex-swap: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
