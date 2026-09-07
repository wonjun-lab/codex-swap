"""리셋 시각 표시.

raw epoch 은 사람이 읽을 수 없다. 그리고 이 표시는 쿠폰으로 사용량을 리셋했을 때
"먹었나" 를 확인하는 자리이기도 하다 — 리셋 시각이 앞으로 당겨지면 그게 신호다.
"""

from __future__ import annotations

import datetime

import pytest

from codex_swap.cli import _reset_text

NOW = datetime.datetime(2026, 9, 6, 12, 0, 0).timestamp()


def at(**kw) -> float:
    return (datetime.datetime(2026, 9, 6, 12, 0, 0) + datetime.timedelta(**kw)).timestamp()


@pytest.mark.parametrize(
    ("resets_at", "expected_tail"),
    [
        (at(minutes=30), "(in 30m)"),
        (at(hours=5), "(in 5h)"),
        (at(days=4, hours=2), "(in 4d)"),
        (at(hours=-1), "(past)"),
        (at(seconds=59), "(in 0m)"),
    ],
)
def test_relative_time(resets_at: float, expected_tail: str) -> None:
    assert _reset_text(resets_at, now=NOW).endswith(expected_tail)


def test_absolute_time_is_shown_too() -> None:
    """상대 시간만으로는 부족하다 — 'in 4d' 가 몇 시인지 알아야 계획을 세운다."""
    text = _reset_text(at(days=4, hours=2), now=NOW)
    assert text.startswith("09-10 14:00")


@pytest.mark.parametrize("bad", [None, "1789081030", True, False, {}, []])
def test_non_numeric_is_a_dash(bad: object) -> None:
    """`True` 가 dash 여야 한다 — bool 은 int 하위 타입이라 그냥 두면 1970 년으로 찍힌다."""
    assert _reset_text(bad, now=NOW) == "-"


def test_a_coupon_reset_moves_the_time_earlier() -> None:
    """쿠폰이 먹으면 리셋 시각이 앞으로 당겨진다 — 그 차이가 표시로 드러나야 한다."""
    before = _reset_text(at(days=5), now=NOW)
    after = _reset_text(at(hours=2), now=NOW)
    assert before != after
    assert "(in 5d)" in before and "(in 2h)" in after
