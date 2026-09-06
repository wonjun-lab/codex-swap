"""회전 정책 — 순수 함수.

이 모듈은 파일도 네트워크도 시계도 만지지 않는다. 판단에 필요한 것은 전부 `Snapshot`
으로 들어오고, 나가는 것은 `Decision` 하나다. bash 에서 사다리 판정이 프로브·파일 I/O 와
한 함수에 엉켜 있어 격리 테스트가 불가능했던 것이 이 이관의 첫 번째 이유다.

`rotate.py` 가 상태를 모아 여기에 넘기고, 돌아온 `Decision` 을 `store` 에 집행시킨다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from codex_swap.core.config import Settings
from codex_swap.core.types import (
    Decision,
    Indeterminate,
    NoOp,
    ProbeOutcome,
    ProbeResult,
    Switched,
)


@dataclass(frozen=True)
class Snapshot:
    """판단 시점의 세계 전체.

    `decide` 가 파일도 시계도 만지지 않을 수 있는 이유가 이 타입이다 — 판단에 필요한
    것이 전부 값으로 들어오므로, 사다리·마진·쿨다운·busy 의 전 조합을 부수 효과 없이
    돌릴 수 있다. 폐기된 bash 차등 하니스가 두 정책 엔진에 먹이던 것도 이 값이었다.
    """

    settings: Settings

    active_present: bool
    """`~/.codex/auth.json` 이 있는가. 없으면 로그아웃 복구 경로다."""

    active_label: str | None = None
    """활성 email 과 일치하는 슬롯. 슬롯에 없는 계정으로 직접 로그인했으면 None."""

    active_probe: ProbeResult | None = None
    candidates: dict[str, ProbeResult] = field(default_factory=dict)
    """활성이 아닌 슬롯들. 로그아웃 복구 경로에서는 **모든** 슬롯이 여기 온다."""

    cooldown_active: bool = False
    busy: bool = False


def first_rung(ladder: tuple[int, ...]) -> int | None:
    """사다리의 첫 칸.

    활성 사용량이 이보다 낮으면 어떤 조합에서도 전환이 성립하지 않으므로, 다른 계정을
    프로브하지 않고 바로 끝낼 수 있다. 주중 대부분의 호출이 이 경로다.
    """
    return ladder[0] if ladder else None


def rung_for(ladder: tuple[int, ...], min_pct: int) -> int | None:
    """현재 관문 = **가장 덜 쓴 계정**의 사용량 바로 위 칸.

    활성을 기준으로 잡으면 앞선 쪽만 계속 올라가 번갈아 밟기가 성립하지 않는다.
    남은 칸이 없으면 None — 사다리가 끝났고 더 얻을 게 없다.
    """
    for step in ladder:
        if min_pct < step:
            return step
    return None


def _best(candidates: dict[str, ProbeResult]) -> tuple[str | None, int]:
    """가장 덜 쓴 후보. 동률이면 **최초 열거 라벨**이 이긴다 (bash 의 strict `<`).

    그래서 호출자가 넘기는 dict 의 삽입 순서가 결과를 바꾼다. `store` 가 정렬된 순서로
    채워 넣어 구현 간 순서 의존을 없앤다 (설계문 §6.5).
    """
    best_label, best_pct = None, 101
    for label, probe in candidates.items():
        if not probe.ok or probe.usage is None:
            continue
        pct = probe.usage.used_percent
        if pct < best_pct:
            best_label, best_pct = label, pct
    return best_label, best_pct


def decide(snap: Snapshot) -> Decision:
    """정책 본체. bash `codex_account_rotate` 의 판단 부분과 동치여야 한다."""
    s = snap.settings

    # ── 완전 로그아웃 복구 ──
    # auth.json 자체가 없으면 "활성 계정" 이라는 개념이 성립하지 않는다. 견줄 대상이
    # 없으므로 사다리 바깥의 별도 경로로 다룬다. 슬롯이 하나뿐이어도 복구한다 —
    # 전환은 둘이 있어야 의미가 있지만 복구는 하나로도 성립하므로, 이 검사가 아래의
    # "둘 이상" 요건보다 **앞에** 있어야 한다.
    if not snap.active_present:
        label, pct = _best(snap.candidates)
        if label is None:
            return Indeterminate("logged out and no stored account answered a probe")
        return Switched(to_label=label, reason=f"logged-out 복구 -> {label}({pct}%)")

    # 전환은 계정이 둘 이상이어야 의미가 있다.
    if len(snap.candidates) < 1:
        return NoOp("only one account registered")

    if snap.active_label is None:
        # 슬롯에 없는 계정으로 사용자가 직접 로그인했다. 건드리지 않는다.
        return NoOp("active account is not a registered slot")

    probe = snap.active_probe
    if probe is None or probe.outcome is ProbeOutcome.UNKNOWN:
        # 계정 상태를 모른다. 함부로 움직이지 않는다.
        return Indeterminate("active usage unreadable")

    auth_failed = probe.outcome is ProbeOutcome.AUTH_FAILED
    if auth_failed:
        # 비교용 상한. blocked 경로만 타므로 사다리 계산에는 쓰이지 않는다.
        active_pct, reached = 100, False
    else:
        assert probe.usage is not None
        active_pct, reached = probe.usage.used_percent, probe.usage.reached

    # 소진과 인증 실패는 같은 처지다 — 활성이 **지금 0 을 서비스한다**. 그래서 평상시의
    # 절제(사다리·마진·쿨다운·busy)를 전부 건너뛴다. 그 절제들은 "아직 쓸 수 있는 계정을
    # 성급히 떠나지 말라" 는 뜻인데 여기서는 지킬 것이 남아 있지 않다. busy 를 건너뛰는
    # 것이 특히 그렇다 — 그 가드는 진행 중인 턴을 401 로 끊지 않으려는 것인데, 활성이
    # 이미 소진·만료면 그 턴들은 어차피 실패하는 중이다.
    blocked = reached or auth_failed

    if not blocked:
        rung0 = first_rung(s.ladder)
        if rung0 is None:
            return NoOp("ladder is empty")
        if active_pct < rung0:
            return NoOp(f"active {active_pct}% below first rung {rung0}%")
        if snap.cooldown_active:
            return NoOp("cooldown")
        if snap.busy:
            return NoOp("recent job activity")

    best_label, best_pct = _best(snap.candidates)
    if best_label is None:
        return Indeterminate("no candidate answered a probe")

    # 관문은 가장 덜 쓴 계정을 기준으로 잡아야 둘이 같은 칸을 번갈아 밟는다.
    min_pct = min(active_pct, best_pct)

    if auth_failed:
        # 활성 토큰이 죽었다. 후보가 있다는 것 자체가 조건이다 — 그 계정은 방금 프로브에
        # **성공**했으므로 지금 살아 있다는 뜻이고, 죽은 계정보다는 무조건 낫다. 95% 라도
        # 살아 있는 쪽이 0% 인데 로그인이 풀린 쪽보다 낫다.
        reason = "auth(활성 토큰 만료·폐기)"
        show = "토큰만료"
    elif reached:
        # 소진은 사다리와 무관하다. 조금이라도 덜 쓴 계정이 있으면 무조건 낫다. 사다리
        # 끝이라고 붙들고 있으면 양쪽에 여유가 남았는데도 전부 막힌 채 끝난다.
        if best_pct >= active_pct:
            return NoOp(f"reached but no lighter account ({best_pct}% >= {active_pct}%)")
        reason, show = "reached", f"{active_pct}%"
    else:
        rung = rung_for(s.ladder, min_pct)
        if rung is None:
            return NoOp("ladder exhausted")
        if active_pct < rung:
            return NoOp(f"active {active_pct}% below rung {rung}%")
        # 동률 근처의 무의미한 교체를 막는다. 왕복 자체는 사다리가 막는다.
        if best_pct + s.margin > active_pct:
            return NoOp(f"margin ({best_pct}% + {s.margin} > {active_pct}%)")
        reason = f"rung({active_pct}%>={rung}%, min {min_pct}%)"
        show = f"{active_pct}%"

    return Switched(
        from_label=snap.active_label,
        to_label=best_label,
        reason=f"{reason} ({show} -> {best_pct}%)",
    )
