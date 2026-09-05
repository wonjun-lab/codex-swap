"""정책 동치 테스트.

케이스 이름은 bash `tests/test-codex-account.sh` 의 것을 그대로 가져왔다. 그 파일이
사실상의 실행 가능한 명세이고, 여기 케이스가 그것과 1:1 로 대응해야 차등 테스트
(설계문 §7.3)의 픽스처로 쓸 수 있다.

bash 는 이 판정 하나를 보려고 가짜 프로브 `.mjs` 를 주입하고 슬롯마다 `.fakepct` 를
심어야 했다. 여기서는 파일도 네트워크도 없다 — 그게 이관의 첫 번째 이유다.
"""

from __future__ import annotations

import pytest

from codex_swap.core import config
from codex_swap.core.policy import Snapshot, decide, rung_for
from codex_swap.core.types import Indeterminate, NoOp, ProbeResult, Switched, Usage


def settings(**over) -> config.Settings:
    base = config.load({})
    return config.Settings(**{**base.__dict__, **over})


def ok(pct: int, *, reached: bool = False) -> ProbeResult:
    return ProbeResult.of(Usage(used_percent=pct, reached=reached))


def snap(**kw) -> Snapshot:
    kw.setdefault("settings", settings())
    kw.setdefault("active_present", True)
    kw.setdefault("active_label", "a")
    return Snapshot(**kw)


# ── 사다리 (50,70,85,95) ─────────────────────────────────────────────────────
# 관문은 가장 덜 쓴 계정의 사용량 바로 위 칸이다. 활성 기준으로 잡으면 앞선 쪽만 계속
# 올라가 번갈아 밟기가 성립하지 않는다 — L3/L4 가 그 뮤테이션을 잡는다.


@pytest.mark.parametrize(
    ("active", "backup", "switches", "name"),
    [
        (55, 0, True, "L1 첫 칸(50%) 도달 → 교체"),
        (45, 0, False, "L2 첫 칸 미만이면 예비가 0%여도 교체하지 않는다"),
        (55, 50, False, "L3 둘 다 첫 칸을 밟았으면 관문이 70%로 올라간다"),
        (75, 50, True, "L4 올라간 관문(70%)에 닿으면 교체"),
    ],
)
def test_ladder(active: int, backup: int, switches: bool, name: str) -> None:
    d = decide(snap(active_probe=ok(active), candidates={"b": ok(backup)}))
    assert isinstance(d, Switched) is switches, f"{name}: got {d}"


def test_rung_is_computed_from_the_least_used_account() -> None:
    # 활성 기준이면 55 -> 70 이지만, min(55,50)=50 기준이라 관문은 70 이 되어야 한다.
    assert rung_for((50, 70, 85, 95), 50) == 70
    assert rung_for((50, 70, 85, 95), 95) is None  # 사다리 끝


# ── 인증 실패 (활성 토큰이 죽었을 때) ────────────────────────────────────────


def test_t1_dead_active_token_moves_to_a_live_account() -> None:
    d = decide(snap(active_probe=ProbeResult.auth_failed(), candidates={"b": ok(10)}))
    assert isinstance(d, Switched) and d.to_label == "b"


def test_t2_dead_token_ignores_the_ladder_even_at_zero_percent() -> None:
    d = decide(snap(active_probe=ProbeResult.auth_failed(), candidates={"b": ok(0)}))
    assert isinstance(d, Switched)


def test_t3_dead_token_moves_even_to_a_95_percent_account() -> None:
    # 살아 있는 95% 가 로그인 풀린 0% 보다 낫다. 사용량 비교를 하지 않는 이유다.
    d = decide(snap(active_probe=ProbeResult.auth_failed(), candidates={"b": ok(95)}))
    assert isinstance(d, Switched)


def test_t4_dead_token_moves_even_while_busy() -> None:
    d = decide(snap(active_probe=ProbeResult.auth_failed(), candidates={"b": ok(10)}, busy=True))
    assert isinstance(d, Switched)


def test_t5_dead_token_moves_even_during_cooldown() -> None:
    d = decide(
        snap(
            active_probe=ProbeResult.auth_failed(),
            candidates={"b": ok(10)},
            cooldown_active=True,
        )
    )
    assert isinstance(d, Switched)


def test_t6_counterexample_both_dead_stays_put() -> None:
    d = decide(
        snap(
            active_probe=ProbeResult.auth_failed(),
            candidates={"b": ProbeResult.auth_failed()},
        )
    )
    assert isinstance(d, Indeterminate)


def test_t7_counterexample_a_general_probe_failure_is_not_an_auth_failure() -> None:
    # 1(네트워크·파싱)은 계정 상태에 대해 아무 말도 하지 않는다. 움직이면 안 된다.
    d = decide(snap(active_probe=ProbeResult.unknown(), candidates={"b": ok(0)}))
    assert isinstance(d, Indeterminate)


def test_t8_exhausted_moves_even_while_busy() -> None:
    d = decide(snap(active_probe=ok(95, reached=True), candidates={"b": ok(50)}, busy=True))
    assert isinstance(d, Switched)


def test_exhausted_does_not_move_to_a_heavier_account() -> None:
    d = decide(snap(active_probe=ok(95, reached=True), candidates={"b": ok(97)}))
    assert isinstance(d, NoOp)


def test_exhausted_ignores_the_exhausted_ladder() -> None:
    # 사다리 끝(양쪽 95 이상)이라도 소진이면 덜 쓴 쪽으로 간다.
    d = decide(snap(active_probe=ok(99, reached=True), candidates={"b": ok(96)}))
    assert isinstance(d, Switched)


# ── 완전 로그아웃 복구 ───────────────────────────────────────────────────────


def test_t9_logged_out_restores_the_least_used_account() -> None:
    d = decide(snap(active_present=False, active_label=None, candidates={"a": ok(80), "b": ok(3)}))
    assert isinstance(d, Switched) and d.to_label == "b"


def test_t10_logged_out_restores_even_with_a_single_slot() -> None:
    # 전환은 둘이 있어야 의미가 있지만 복구는 하나로도 성립한다.
    d = decide(snap(active_present=False, active_label=None, candidates={"only": ok(42)}))
    assert isinstance(d, Switched) and d.to_label == "only"


def test_t11_counterexample_logged_out_with_all_stored_accounts_dead() -> None:
    d = decide(
        snap(
            active_present=False,
            active_label=None,
            candidates={"a": ProbeResult.auth_failed()},
        )
    )
    assert isinstance(d, Indeterminate)


def test_t12_counterexample_an_unregistered_active_account_is_left_alone() -> None:
    d = decide(snap(active_label=None, active_probe=ok(95), candidates={"b": ok(1)}))
    assert isinstance(d, NoOp)


# ── 절제 ─────────────────────────────────────────────────────────────────────


def test_cooldown_blocks_an_ordinary_rung_switch() -> None:
    d = decide(snap(active_probe=ok(75), candidates={"b": ok(10)}, cooldown_active=True))
    assert isinstance(d, NoOp) and d.reason == "cooldown"


def test_margin_blocks_a_near_tie() -> None:
    """사다리는 통과하지만 차이가 마진(5) 미만이면 무의미한 교체다.

    이 케이스를 실제로 마진까지 도달시키려면 값을 좁게 골라야 한다. 관문은 **가장 덜 쓴**
    계정 기준이므로, 활성 51 · 예비 48 이면 관문은 50 이고 활성이 그것을 넘는다. 그제서야
    마진(48+5 > 51)이 판정을 맡는다. 예컨대 72/70 으로 쓰면 관문이 85 로 올라가 마진에
    닿기도 전에 막히므로, 마진 게이트를 지우는 뮤테이션이 살아남는다.
    """
    d = decide(snap(active_probe=ok(51), candidates={"b": ok(48)}))
    assert isinstance(d, NoOp) and "margin" in d.reason


def test_margin_allows_a_clear_gap_at_the_same_rung() -> None:
    # 위와 같은 관문(50)인데 차이가 마진 이상이면 교체된다 — 마진 검사의 반례.
    d = decide(snap(active_probe=ok(55), candidates={"b": ok(48)}))
    assert isinstance(d, Switched)


def test_a_single_account_never_switches() -> None:
    d = decide(snap(active_probe=ok(99), candidates={}))
    assert isinstance(d, NoOp)
