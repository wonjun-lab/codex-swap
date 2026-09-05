"""회전 조립 + fail-open 경계.

여기가 부수효과와 판단이 만나는 유일한 지점이다. 상태를 읽어 `Snapshot` 을 만들고,
`policy.decide` 에 넘기고, 돌아온 `Decision` 을 `store` 에 집행시킨다.

**어떤 예외도 이 모듈 밖으로 나가지 않는다.** rotate 는 codex wrapper 안에서 도는데,
wrapper 는 stdout 만 버리고 stderr 는 사용자 터미널로 그대로 흘린다. 자격증명을 다루는
코드가 여기에 트레이스백을 뱉으면 토큰이 화면에 실린다 (계약 2 · 설계문 §5.1).
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Callable
from pathlib import Path

from codex_swap.core import cache, discovery, paths, store
from codex_swap.core import probe as probe_mod
from codex_swap.core.config import ConfigError, Settings
from codex_swap.core.policy import Snapshot, decide
from codex_swap.core.types import (
    Decision,
    Failed,
    Indeterminate,
    NoOp,
    ProbeResult,
    Switched,
    Usage,
)

ProbeFn = Callable[[str, str | None], ProbeResult]
"""(codex_bin, home) -> ProbeResult. 테스트가 여기를 갈아끼운다."""


def _default_probe(codex_bin: str, home: str | None) -> ProbeResult:
    return probe_mod.probe(codex_bin, home)


# ── 상태 읽기 ────────────────────────────────────────────────────────────────


def _stamp_age(path: Path, now: float) -> float | None:
    try:
        return now - path.stat().st_mtime
    except OSError:
        return None


def throttled(settings: Settings, now: float) -> bool:
    """방금 판단했으면 다시 하지 않는다. stat 한 번이라 사실상 공짜다.

    캐시가 살아 있어도 라벨마다 auth.json 을 열어 email 을 뽑는 비용이 남아 매 codex
    호출에 실린다. 사용량은 며칠에 걸쳐 움직이므로 분 단위로 늦게 알아채도 잃는 게 없다.
    """
    if settings.check_interval <= 0:
        return False
    age = _stamp_age(paths.check_stamp_path(settings), now)
    return age is not None and age < settings.check_interval


def cooldown_active(settings: Settings, now: float) -> bool:
    age = _stamp_age(paths.rotate_stamp_path(settings), now)
    return age is not None and age < settings.cooldown


def busy(settings: Settings, now: float) -> bool:
    """최근 job 활동이 있으면 전환하지 않는다.

    브로커를 새 토큰으로 다시 띄우는 과정에서 진행 중인 턴이 401 로 끊기기 때문이다.

    창은 `effective_busy_window` 를 쓴다 — bash 의 `-mmin` 폴백이 BSD 전용이 아니라
    무조건 실행되므로 실효 창이 언제나 분 단위로 올림돼 있다 (설계문 §2.3.1).
    """
    root = settings.rotate_state_root
    window = settings.effective_busy_window
    if window <= 0 or not root.is_dir():
        return False
    try:
        for path in root.rglob("*.log"):
            with contextlib.suppress(OSError):
                if now - path.stat().st_mtime < window:
                    return True
    except OSError:
        return False
    return False


def _usage_of(
    settings: Settings, label: str, home: Path, codex_bin: str, probe_fn: ProbeFn, now: float
) -> ProbeResult:
    """캐시를 앞세운 프로브.

    캐시는 성공(`Ok`)만 담는다. 인증 실패는 담지 않으므로 히트가 죽은 토큰을 최대 한
    TTL 가린다 — 정해진 지연 예산이지 결함이 아니다 (설계문 §6.4.1).
    """
    cached = cache.read(settings, label, now=now)
    if cached is not None:
        with contextlib.suppress(Exception):
            return ProbeResult.of(
                Usage(
                    used_percent=int(cached["usedPercent"]),
                    email=cached.get("email"),
                    plan_type=cached.get("planType"),
                    primary_percent=cached.get("primaryPercent"),
                    secondary_percent=cached.get("secondaryPercent"),
                    resets_at=cached.get("resetsAt"),
                    reached=bool(cached.get("reached")),
                )
            )
    result = probe_fn(codex_bin, str(home))
    if result.ok and result.usage is not None:
        u = result.usage
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
                "reached": u.reached,
            },
            now=now,
        )
    return result


def _home_for(settings: Settings, label: str, active: str | None) -> Path:
    """라벨의 `CODEX_HOME`.

    활성 계정은 슬롯 사본이 아니라 실제로 쓰이는 기본 홈으로 재운다 — 슬롯 사본은 토큰이
    갱신되며 뒤처지고, 기본 홈이 언제나 사실이다. 슬롯 디렉토리를 프로브용 홈으로 쓰는
    것이 보관 토큰을 갱신해 주는 장치이기도 하다 (계약 4).
    """
    return settings.default_home if label == active else store.slot_dir(settings, label)


def _codex_home_mismatch(settings: Settings) -> bool:
    """호출자가 이미 홈을 지정했으면 그 홈으로 일하려는 것이다. 전역 파일을 건드리지 않는다.

    **물리 경로**로 견준다 (계약 13). 문자열 비교로 두면 `~/.codex/` 처럼 슬래시 하나만
    붙어도 가드가 뚫려, 슬롯 홈을 지정한 프로브가 전역 자격증명을 갈아끼운다.
    """
    raw = os.environ.get("CODEX_HOME")
    if not raw:
        return False

    def phys(p: str | Path) -> str:
        return os.path.realpath(p)

    return phys(raw) != phys(settings.default_home)


# ── 본체 ─────────────────────────────────────────────────────────────────────


def rotate(
    settings: Settings,
    *,
    dry_run: bool = False,
    probe_fn: ProbeFn | None = None,
    now: float | None = None,
) -> Decision:
    """정책을 실행한다. 예외를 올리지 않는다 — 전부 `Decision` 으로 접힌다."""
    try:
        return _rotate(settings, dry_run=dry_run, probe_fn=probe_fn or _default_probe, now=now)
    except ConfigError as exc:
        return Failed(f"config: {exc}")
    except store.LockBusy:
        return NoOp("another switch is in progress")
    except store.LockUnusable as exc:
        return Failed(str(exc))
    except Exception as exc:
        return Failed(f"{type(exc).__name__}: {exc}")


def _rotate(settings: Settings, *, dry_run: bool, probe_fn: ProbeFn, now: float | None) -> Decision:
    now = time.time() if now is None else now

    # ── 관문 사다리 ──
    # 순서가 계약이다. 넷 다 --dry-run 분기보다 **앞에** 있고, 전부 조용히 무동작으로
    # 끝난다(출력 없음). argparse 의 자연스러운 모양을 따라 dry_run 을 먼저 보면
    # 이 순서가 무너진다.
    if settings.skip:
        return NoOp("CODEX_ROTATE_SKIP")
    if settings.off_switch.exists():
        return NoOp(f"off switch ({settings.off_switch})")
    if _codex_home_mismatch(settings):
        return NoOp("CODEX_HOME points elsewhere")

    # 스탬프는 판단 **전에** 찍는다 (계약 11 · §5.2). 성공 경로로 옮기면 실패가 반복될 때
    # 스로틀이 걸리지 않아 매 codex 호출이 프로브를 돈다. --dry-run 은 읽기와 쓰기를
    # 둘 다 건너뛴다.
    if not dry_run:
        if throttled(settings, now):
            return NoOp("throttled")
        with contextlib.suppress(OSError):
            paths.ensure_root(settings)
            paths.check_stamp_path(settings).touch()

    labels = store.labels(settings)
    active_present = store.active_auth(settings).is_file()

    # 로그아웃 복구는 슬롯 하나로도 성립하므로 "둘 이상" 요건보다 앞이다. 여기서는
    # 활성이 없으니 모든 슬롯이 후보다.
    if not active_present:
        if not labels:
            return Indeterminate("logged out and no stored account")
        codex_bin = str(_resolve_bin())
        candidates = {
            label: _usage_of(
                settings, label, store.slot_dir(settings, label), codex_bin, probe_fn, now
            )
            for label in labels
        }
        return _execute(
            settings,
            decide(Snapshot(settings=settings, active_present=False, candidates=candidates)),
            dry_run=dry_run,
        )

    active = store.active_label(settings)
    if active is None:
        return NoOp("active account is not a registered slot")
    if len(labels) < 2:
        return NoOp("only one account registered")

    codex_bin = str(_resolve_bin())
    active_probe = _usage_of(settings, active, settings.default_home, codex_bin, probe_fn, now)

    # 싼 경로: 관문은 사다리의 첫 칸보다 낮아질 수 없다. 활성이 거기 못 미치면 다른
    # 계정을 프로브하지 않고 끝낸다 — 주중 대부분의 호출이 여기서 끝난다. 소진·인증
    # 실패는 그 절제를 건너뛰므로 이 지름길에서 제외한다.
    blocked = not active_probe.ok or (active_probe.usage is not None and active_probe.usage.reached)
    if (
        not blocked
        and active_probe.usage is not None
        and settings.ladder
        and active_probe.usage.used_percent < settings.ladder[0]
    ):
        return NoOp(f"active {active_probe.usage.used_percent}% below first rung")

    candidates = {
        label: _usage_of(settings, label, store.slot_dir(settings, label), codex_bin, probe_fn, now)
        for label in labels
        if label != active
    }

    snapshot = Snapshot(
        settings=settings,
        active_present=True,
        active_label=active,
        active_probe=active_probe,
        candidates=candidates,
        cooldown_active=cooldown_active(settings, now),
        busy=busy(settings, now),
    )
    return _execute(settings, decide(snapshot), dry_run=dry_run)


def _resolve_bin() -> Path:
    """wrapper 가 넘겨준 경로를 우선 쓴다.

    핫패스(wrapper)는 `CODEX_ACCOUNT_BIN` 으로 이미 해석된 경로를 넘기므로 탐색이 필요
    없다. 훅과 터미널 호출은 넘기지 않으므로 그때만 `discovery` 가 돈다 (설계문 §8).
    """
    return discovery.resolve_codex_bin()


def _execute(settings: Settings, decision: Decision, *, dry_run: bool) -> Decision:
    if not isinstance(decision, Switched) or dry_run:
        return decision
    with store.switch_lock(settings):
        store.switch(settings, decision.to_label, decision.reason)
    return decision
