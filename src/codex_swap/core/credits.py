"""사용량 리셋 — 읽기와 **소비**.

`cli` 와 `tui` 가 **같은 함수**를 지나야 하는 자리다. 화면과 파이프가 각자 구현하면 갈리고,
갈렸을 때 약한 쪽이 이 도구의 실제 안전 수준이 된다 — 이 프로젝트에서 여러 번 그랬다.
그리고 여기 있는 것은 **되돌릴 수 없는 유일한 동작**이라 그 대가가 가장 크다.

`tui` 가 `cli` 를 들여올 수 없다는 사정도 있다(`cli` 가 화면을 띄우므로 순환이 된다).
그래서 공통분모는 위가 아니라 **아래**에 둔다.

여기 있는 것은 판단과 I/O 뿐이다 — 무엇을 물을지, 어떻게 보여 줄지는 각 표면의 몫이다.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from codex_swap.core import config, discovery, identity, probe, store
from codex_swap.core.types import Credit, ProbeOutcome, Usage

SPENT_NOTHING = frozenset({probe.CreditOutcome.NOTHING_TO_RESET, probe.CreditOutcome.NO_CREDIT})
"""**아무것도 쓰이지 않았고 사용량도 그대로**인 결과들.

캐시를 비울지 가르는 데 쓴다. 이 둘은 사용량 창을 건드리지 않았으므로 캐시에 있는 숫자가
여전히 맞다 — 지우면 다음 `rotate` 가 공짜로 프로브를 한 번 더 돈다.

나머지는 전부 비운다. `UNKNOWN` 도 포함이다 — 썼는지 모르는 상태에서 낡은 숫자를 믿는
쪽이 더 나쁘다.

`cli` 와 `tui` 가 각자 이 목록을 적으면 갈린다. 한쪽만 `NO_CREDIT` 을 빠뜨리는 식으로.
"""


def too_early(settings: config.Settings, label: str, usage: Usage | None) -> str | None:
    """지금 쓰면 **버리는 셈**인가. 그렇다면 이유를, 쓸 만하면 None.

    리셋은 남은 창을 늘려 주지 않는다 — **지우고 새로 준다.** 아직 한도가 많이 남았을 때
    쓰면 그 남은 만큼이 그대로 사라진다. 쿠폰 하나를 내고 오히려 손해를 보는 유일한
    경우라, 되돌릴 수 없는 동작 중에서도 여기만 "너무 이르다" 는 판단이 성립한다.

    문턱은 **사다리의 마지막 칸**이다(기본 95%). 노브를 새로 만들지 않는 이유는 그 칸이
    이미 같은 것을 뜻하기 때문이다 — "더 올라갈 데가 없다", 즉 전환으로는 해결이 안 되는
    지점. 쿠폰을 쓸 시점이 정확히 거기다. 사다리를 조정하면 이 문턱도 같이 따라온다.

    **사용량을 모르면 막는 쪽으로 기운다.** 되돌릴 수 없는 일 앞에서 "모른다" 를 "괜찮다"
    로 읽으면, 프로브가 실패한 순간이 하필 가장 위험한 순간이 된다. 다만 영영 못 쓰게
    하지는 않는다 — 두 표면 모두 밀고 나갈 길이 있다.
    """
    ladder = settings.ladder
    if not ladder:
        return None
    gate = ladder[-1]
    if usage is None or usage.used_percent is None:
        return f"cannot read {label}'s usage right now, so it is unclear whether a reset would help"
    if usage.used_percent < gate:
        left = 100 - usage.used_percent
        return (
            f"{label} is at {usage.used_percent}% — a reset replaces the window, "
            f"so the {left}% still left would be thrown away"
        )
    return None


def said_yes(answer: str | None) -> bool:
    """되돌릴 수 없는 일을 해도 좋다는 대답인가.

    **두 표면이 같은 어휘를 쓰게 하려고** 여기 둔다. 한동안 화면만 라벨을 그대로 치게
    했는데(`Type shared to spend:`), 되돌릴 수 없으니 더 세게 막자는 뜻이었다. 실제로는
    무엇을 치라는 것인지부터 애매했고 — 계정 이름? `use`? 쿠폰 이름? — 파이프에서는 `y`
    면 되는 일이 화면에서만 달랐다.

    답을 못 받은 것(`None`)은 **거절이다.** 프롬프트가 끊기거나 사용자가 빠져나온 자리라,
    침묵을 승낙으로 읽으면 아무도 승인하지 않은 소비가 일어난다.
    """
    return answer is not None and answer.strip().lower() in {"y", "yes"}


class CreditError(Exception):
    """쿠폰을 다루다 멈췄다. **소비는 일어나지 않았다.**

    이 예외가 나가는 모든 자리는 소비 요청 **전**이다. 요청이 나간 뒤의 불확실은 예외가
    아니라 `CreditOutcome.UNKNOWN` 으로 온다 — 그 둘을 섞으면 사용자가 쓰지도 않은 쿠폰을
    잃었다고 믿거나, 이미 쓴 것을 하나 더 태운다.
    """


@dataclass(frozen=True)
class Account:
    """한 계정의 쿠폰 상황. 못 읽었으면 `usage` 가 None."""

    label: str
    email: str
    active: bool
    usage: Usage | None = None

    @property
    def readable(self) -> bool:
        return self.usage is not None

    @property
    def credits(self) -> tuple[Credit, ...]:
        return () if self.usage is None else self.usage.credits

    @property
    def count(self) -> int | None:
        """서버가 말한 **쓸 수 있는 개수**. 모르면 None.

        목록 길이로 다시 세지 않는다. 둘이 어긋날 수 있고(만료·`redeeming` 이 섞이거나
        상세가 일부만 오거나), 그때 다시 세면 화면이 서버와 다른 말을 한다.
        """
        return None if self.usage is None else self.usage.reset_credits


def _codex_bin() -> str:
    try:
        return str(discovery.resolve_codex_bin())
    except Exception as exc:
        raise CreditError(
            f"cannot run codex: {exc}. Check that `codex --version` works, "
            "or set CODEX_ACCOUNT_BIN to its path"
        ) from exc


def _home_for(settings: config.Settings, label: str, active: str | None) -> Path:
    """활성 계정은 **기본 홈**으로 읽는다.

    슬롯 사본은 토큰이 갱신되며 뒤처지고 기본 홈이 언제나 사실이다 — `rotate._home_for`,
    `cli.refresh_all` 이 같은 이유로 같은 선택을 한다.
    """
    return settings.default_home if label == active else store.slot_dir(settings, label)


def load(settings: config.Settings) -> list[Account]:
    """슬롯마다 쿠폰을 읽는다. **캐시를 쓰지 않는다.**

    상세를 캐시에 얹으려면 `resetCredits` 를 쓰는 여섯 자리에 키를 하나씩 더 넣어야 하고,
    그중 한 곳이 빠져서 캐시 히트일 때만 크레딧이 사라진 적이 이미 있다. 이 화면은 사용자가
    직접 열었을 때만 도므로 그때 조회하면 된다 — `list` 와 `rotate` 의 값싼 경로는 그대로다.

    한 슬롯이 실패해도 나머지는 계속한다.
    """
    codex_bin = _codex_bin()
    active = store.active_label(settings)
    out: list[Account] = []
    for label in store.labels(settings):
        email = identity.email_of(store.slot_auth(settings, label)) or "?"
        usage: Usage | None = None
        try:
            result = probe.probe(codex_bin, str(_home_for(settings, label, active)))
        except Exception:
            result = None
        if result is not None and result.outcome is ProbeOutcome.OK and result.usage is not None:
            u = result.usage
            # 조회 도중 다른 rotate 가 전환을 끝내면 기본 홈에 이미 다른 계정이 들어 있다.
            # 그대로 적으면 남의 쿠폰이 이 이름으로 화면에 뜬다.
            want = identity.email_of(store.slot_auth(settings, label))
            if u.email is None or want is None or u.email == want:
                usage = u
        out.append(Account(label=label, email=email, active=label == active, usage=usage))
    return out


def pick(
    credits: Sequence[Credit], wanted: str | None = None, *, now: float | None = None
) -> Credit | None:
    """쓸 쿠폰 하나를 고른다. 없으면 None.

    **먼저 잃을 것부터 쓴다** — 만료가 가장 이른 것. 쿠폰은 되돌릴 수 없으므로 "어느 것을
    쓸까" 는 실제로 "어느 것을 잃어도 되나" 이고, 답은 어차피 곧 사라질 것이다.

    만료를 모르는 쿠폰은 **뒤로** 보낸다. 모르는 것을 급한 것으로 취급하면, 날짜가 찍힌
    쿠폰을 놔두고 정체 모를 것을 먼저 태운다.

    **만료된 것은 자동으로 고르지 않는다.** 규칙을 그대로 두면 이미 지난 쿠폰의 정렬 키가
    가장 작아 **1 순위로 뽑힌다** — 서버가 `available` 이라고 적어 둔 채 날짜만 지난 항목이
    실제로 그렇게 뽑혔다.

    `wanted` 로 지목하면 만료를 따지지 않는다. 사용자가 보고 골랐고 서버는 여전히
    `available` 이라고 말한다 — 우리 시계가 틀렸을 수도 있다. 자동으로 고르지 않는 것과
    못 고르게 막는 것은 다르다.
    """
    usable = [c for c in credits if c.status == "available"]
    if wanted is not None:
        return next((c for c in usable if c.id == wanted), None)
    at = time.time() if now is None else now
    fresh = [c for c in usable if c.expires_at is None or c.expires_at > at]
    if not fresh:
        return None
    return min(fresh, key=lambda c: (c.expires_at is None, c.expires_at or 0))


def spend(
    settings: config.Settings, label: str, credit: Credit, expect: str
) -> probe.CreditOutcome:
    """쿠폰 하나를 쓴다. **되돌릴 수 없다.**

    확인은 묻지 않는다 — 그 자리는 표면이다. 여기까지 왔으면 이미 결정된 것으로 본다.

    `expect` 는 **사용자가 승인한 계정의 이메일**이다. 확인을 기다리는 동안 `rotate` 가
    전환을 끝내면 기본 홈에 이미 다른 계정이 들어 있다 — 승인한 것은 A 인데 요청은 B 의
    자격증명으로 나간다. 락을 잡고, 락 안에서 **소비 직전에 다시** 확인한다.

    락은 소비하는 몇 초만 잡는다. 확인 프롬프트는 밖에 둔다 — 사람이 자리를 비우면 그동안
    rotate 가 멈춘다.
    """
    codex_bin = _codex_bin()
    try:
        with store.switch_lock(settings):
            here = _home_for(settings, label, store.active_label(settings))
            confirmed = identity.email_of(here / "auth.json")
            if confirmed != expect:
                raise CreditError(
                    f"{label} now holds {confirmed or 'an unknown account'}, not {expect}. "
                    "Nothing was spent"
                )
            return probe.consume_credit(codex_bin, str(here), credit.id)
    except store.LockBusy:
        raise CreditError(
            "another switch is in progress. Nothing was spent. Try again in a moment"
        ) from None
    except probe.ProbeError as exc:
        # 여기 오는 것은 소비 요청 **전**의 실패뿐이다. 요청 뒤의 불확실은
        # `consume_credit` 이 `UNKNOWN` 으로 돌려준다.
        raise CreditError(f"could not spend the usage reset: {exc}") from exc
