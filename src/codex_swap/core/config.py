"""환경변수 → Settings.

bash 의 파라미터 확장 의미를 한 곳에서 재현한다. 흩어 두면 호출부마다 미묘하게
달라지는데, 그 차이가 조용한 divergence 의 가장 흔한 원인이다 (설계문 §6.1).
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LADDER = (50, 70, 85, 95)
DEFAULT_MARGIN = 5
DEFAULT_CACHE_TTL = 300
DEFAULT_CHECK_INTERVAL = 60
DEFAULT_COOLDOWN = 900
DEFAULT_BUSY_WINDOW = 180


class ConfigError(Exception):
    """설정값이 숫자가 아니다.

    bash 는 값을 파싱하지 않고 `(( ))` 안에 그대로 보간해서, 숫자가 아니면 그것을
    변수 이름으로 해석하고 `set -u` 아래서 프로세스가 죽는다. 어느 노브가 먼저
    터지는지가 어떤 가드가 먼저 단락되는지에 달려 있고, 일부는 명령 치환 안에서 죽어
    서브셸만 죽고 rotate 는 0 으로 전환까지 한다.

    그 동작은 흉내내지 않는다. 여기서 올리고 rotate 의 fail-open 경계에서 "전환 안 함"
    으로 접는다. 대신 차등 테스트에서 이 축은 제외한다 — bash 쪽 결과가 비교 가능한
    형태가 아니다.
    """


def _raw(name: str) -> str | None:
    """bash `${X:-default}` 의미: 미설정과 **빈 문자열**을 똑같이 취급한다.

    `os.getenv(name, default)` 는 빈 문자열을 그대로 돌려주므로 그대로 쓰면 안 된다.
    `CODEX_ACCOUNTS_DIR=""` 는 bash 에서 기본 경로인데 `Path("")` 는 cwd 가 된다.
    """
    v = os.environ.get(name)
    return None if v is None or v == "" else v


def _int_env(name: str, default: int) -> int:
    v = _raw(name)
    if v is None:
        return default
    s = v.strip()
    # bash 는 `(( ))` 안에서 음수도 받는다. 여기서도 받아 두어야 MARGIN=-5 같은 값에서
    # 우리만 거부하는 일이 없다.
    if not (s.lstrip("-").isdigit() and s.lstrip("-") != ""):
        raise ConfigError(f"{name}={v!r} is not an integer")
    return int(s)


def _flag_env(name: str) -> bool:
    """bash 의 `[[ -n "${X:-}" ]]` — 불리언이 아니라 **비어 있지 않음**이다.

    `CODEX_ROTATE_SKIP=0` 은 bash 에서 회전을 **끈다**. 일반적인 불리언 파서는 이를
    false 로 읽어 정반대로 동작한다.
    """
    return _raw(name) is not None


def _ladder_env(name: str, default: tuple[int, ...]) -> tuple[int, ...]:
    """`50,70,85,95` 형태. bash 는 숫자가 아닌 칸을 조용히 건너뛴다."""
    v = _raw(name)
    if v is None:
        return default
    rungs = tuple(int(p) for p in (x.strip() for x in v.split(",")) if p.isdigit())
    return rungs


@dataclass(frozen=True)
class Settings:
    default_home: Path
    accounts_dir: Path
    rotate_state_root: Path
    ladder: tuple[int, ...]
    margin: int
    cache_ttl: int
    check_interval: int
    cooldown: int
    busy_window: int
    skip: bool
    off_switch: Path

    @property
    def effective_busy_window(self) -> int:
        """bash 의 실효 busy 창 (설계문 §2.3.1).

        `-mmin` 폴백은 BSD 전용이 아니다. 첫 `-newermt` 결과가 비면 **무조건** 실행되고
        GNU find 도 `-mmin` 을 지원하므로, Linux 에서도 실효 창은 언제나 분 단위로
        올림된 값이다. 초 단위로 좁히면 차등 테스트가 이 축에서 상시 불일치한다.

        bash 를 걷어내는 PR 에서 이 프로퍼티를 지우고 `busy_window` 를 직접 쓴다.
        """
        return math.ceil(self.busy_window / 60) * 60 if self.busy_window > 0 else 0


def load(environ: dict[str, str] | None = None) -> Settings:
    """현재 환경에서 설정을 읽는다.

    `~/.codex/.env` 는 읽지 **않는다**. wrapper 가 `set -a` 로 source 해서 변수가 이미
    export 된 채 도착하고, 여기서 다시 읽으면 오늘 `.env` 를 보지 못하는 두 경로(Claude
    훅 · `codex-swap` 직접 호출)에서 **새로** 유효해진다. 그건 이관이 아니라 동작
    변경이다 (설계문 §6.1.1).
    """
    if environ is not None:
        os.environ.update(environ)

    home = Path(os.environ.get("HOME") or Path.home())
    default_home = Path(_raw("CODEX_ACCOUNT_DEFAULT_HOME") or home / ".codex")
    accounts_dir = Path(_raw("CODEX_ACCOUNTS_DIR") or default_home / "accounts")
    state_root = Path(
        _raw("CODEX_ROTATE_STATE_ROOT") or home / ".claude/plugins/data/codex-openai-codex/state"
    )

    return Settings(
        default_home=default_home,
        accounts_dir=accounts_dir,
        rotate_state_root=state_root,
        ladder=_ladder_env("CODEX_ROTATE_LADDER", DEFAULT_LADDER),
        margin=_int_env("CODEX_ROTATE_MARGIN", DEFAULT_MARGIN),
        cache_ttl=_int_env("CODEX_ROTATE_CACHE_TTL", DEFAULT_CACHE_TTL),
        check_interval=_int_env("CODEX_ROTATE_CHECK_INTERVAL", DEFAULT_CHECK_INTERVAL),
        cooldown=_int_env("CODEX_ROTATE_COOLDOWN", DEFAULT_COOLDOWN),
        busy_window=_int_env("CODEX_ROTATE_BUSY_WINDOW", DEFAULT_BUSY_WINDOW),
        skip=_flag_env("CODEX_ROTATE_SKIP"),
        off_switch=home / ".claude/.codex-rotate-off",
    )
