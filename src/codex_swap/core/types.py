"""도메인 타입.

여기의 모든 타입은 불변이고 I/O 를 모른다. `policy` 가 순수 함수일 수 있는 이유가
이 파일에 있다 — 판단에 필요한 것이 전부 값으로 표현되므로, 판단 자체는 파일도
네트워크도 만지지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True)
class Credit:
    """사용량 리셋 쿠폰 하나.

    `account/rateLimits/read` 의 `rateLimitResetCredits.credits[]` 다. 지금까지는 이
    목록에서 `availableCount` 하나만 꺼내 쓰고 나머지를 버렸다. 그래서 화면은 "2 개 있다"
    까지만 말하고 **언제까지인지**는 말하지 못했다 — 쿠폰은 만료되는 자원이라 그 한 줄이
    쓸지 말지를 가른다.

    `id` 를 캐시에 남기지 않는 것은 의도다. 쿠폰을 쓰는 것은 되돌릴 수 없는 동작이라,
    그때는 낡은 식별자가 아니라 **그 순간 조회한 것**을 써야 한다. 캐시에 두면 그 규율이
    코드가 아니라 습관에 걸린다.
    """

    id: str
    status: str | None = None
    """`available` · `redeeming` 등. 서버가 정하는 문자열이라 열거형으로 좁히지 않는다."""

    granted_at: int | None = None
    expires_at: int | None = None
    title: str | None = None


@dataclass(frozen=True)
class Usage:
    """한 계정의 사용량. bash 프로브가 뱉던 한 줄 JSON 과 같은 내용이다."""

    used_percent: int
    """판단에 쓰이는 값. 두 창의 max 에 정수 게이트를 통과한 것만 여기 온다."""

    email: str | None = None
    plan_type: str | None = None

    # 아래 둘은 **표시용**이라 정수 게이트를 받지 않는다. bash 는 정수 판정을
    # `usedPercent` 하나에만 걸고 창별 값은 그대로 싣는다. primary=50 · secondary=37.5
    # 처럼 max 만 정수인 조합이 실제로 있으므로 float 을 받는다.
    primary_percent: float | None = None
    secondary_percent: float | None = None
    resets_at: int | None = None

    reset_credits: int | None = None
    """남은 사용량 리셋 쿠폰 수. 모르면 None.

    `account/rateLimits/read` 의 `rateLimitResetCredits.availableCount` 다. 정책은 이 값을
    쓰지 않는다 — 쿠폰을 쓰는 것은 사람의 결정이고, 자동 전환이 대신 판단할 일이 아니다.
    표시만 한다. 소진된 계정에 쿠폰이 남아 있으면 전환하는 대신 그것을 쓰는 선택지가
    생기는데, 지금까지는 화면에 그 정보가 없어서 그 선택 자체가 보이지 않았다.
    """

    credits: tuple[Credit, ...] = ()
    """쿠폰 하나하나의 상세. `reset_credits` 는 그중 쓸 수 있는 것의 개수다.

    **캐시에 싣지 않는다.** `resetCredits` 하나를 캐시에 넣는 데도 쓰는 곳이 여섯 군데고,
    한 곳이 빠뜨려서 캐시 히트일 때만 크레딧이 사라진 적이 있다. 키를 하나 더 얹으면 그
    자리가 여섯 개 더 생긴다. 상세가 필요한 화면은 `credits` 하나뿐이고 그것은 사용자가
    직접 친 명령이라, 그때 프로브하면 된다.
    """

    reached: bool = False
    """소진 여부.

    bash 는 `jq -r '.reached // empty'` 의 결과가 비어 있지 않은지로 판단했다. jq 의
    `// empty` 는 missing·null·**false** 를 접지만 `0` 은 `"0"` 으로 남긴다. 그래서
    `reached: 0` 은 bash 에서 **소진으로 처리된다** — 파싱 단계에서 이 술어를 재현하고
    (probe.py), 여기서는 이미 정규화된 bool 만 받는다.
    """


class ProbeOutcome(Enum):
    """프로브 결과의 세 갈래.

    bash 의 `codex_account_usage` 는 0 / 1 / 3 을 돌려주고, 이 셋은 정반대의 결론으로
    간다. 3(인증 실패)은 "이 계정으로는 지금 아무것도 못 한다" 이므로 떠나야 하고,
    1(네트워크·파싱)은 계정 상태에 대해 **아무 말도 하지 않으므로** 움직이면 안 된다.
    예전 bash 가 둘을 `|| return 1` 로 묶었다가, 하필 전환이 가장 절실한 순간에
    스위처가 손을 놓았다.
    """

    OK = "ok"
    AUTH_FAILED = "auth_failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProbeResult:
    outcome: ProbeOutcome
    usage: Usage | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is ProbeOutcome.OK

    @staticmethod
    def of(usage: Usage) -> ProbeResult:
        return ProbeResult(ProbeOutcome.OK, usage)

    @staticmethod
    def auth_failed() -> ProbeResult:
        return ProbeResult(ProbeOutcome.AUTH_FAILED)

    @staticmethod
    def unknown() -> ProbeResult:
        return ProbeResult(ProbeOutcome.UNKNOWN)


# ── 판단 결과 ────────────────────────────────────────────────────────────────
# 공개 exit code 는 0/1 만 쓰지만(§7.4), 내부적으로는 넷을 구분한다. bash 는 throttle ·
# cooldown · busy · 네트워크 실패 · 파싱 실패를 전부 1 로 접어서, rc 만 보고는 "판단했고
# 안 바꿨다" 와 "아무것도 못 읽었다" 를 가를 수 없었다. 그 구분이 필요한 이유는 차등
# 하니스(폐기됨) 때문이 아니라 두 상태의 **다음 행동이 다르기** 때문이다 — NoOp 은 정상
# 이고, Indeterminate 가 계속 나오면 프로브가 죽은 것이라 사람이 봐야 한다.


@dataclass(frozen=True)
class Switched:
    to_label: str
    reason: str
    from_label: str | None = None


@dataclass(frozen=True)
class NoOp:
    """판단했고, 바꾸지 않기로 했다."""

    reason: str


@dataclass(frozen=True)
class Indeterminate:
    """상태를 모른다. 네트워크·파싱 실패처럼 계정에 대해 아무 정보도 못 얻은 경우."""

    reason: str


@dataclass(frozen=True)
class Failed:
    """우리 잘못. 설정 파싱 실패, 락 파일이 디렉토리가 아님 등."""

    reason: str


Decision = Switched | NoOp | Indeterminate | Failed


def decision_exit_code(decision: Decision) -> int:
    """공개 exit code. bash 와 같은 0/1 계약을 유지한다."""
    return 0 if isinstance(decision, Switched) else 1
