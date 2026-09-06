"""사용량 캐시 (TTL).

bash `codex_account_cache_read` / `codex_account_cache_write` 의 포트다. 파일 위치와
형식을 그대로 유지한다 — 병행 기간에는 bash 판과 **같은 파일을 같은 자리에** 쓰므로,
키 하나만 달라져도 두 판이 서로의 항목을 미스로 읽고 프로브가 두 배로 돈다.

여기에 담기는 것은 `Ok` 뿐이다. bash 에서 rc=3(인증 실패) 반환이 캐시 쓰기보다 **앞에**
있어서 인증 실패는 절대 캐시되지 않고, 어떤 종류의 음성 캐싱도 없다. 그래서 히트 경로는
구조상 rc=3 을 낼 수 없다 — 프로브를 띄우기 전에 반환하기 때문이다.

그 대가로 **죽은 토큰이 최대 한 TTL(기본 300 초) 동안 가려진다.** 이건 결함이 아니라
정해진 지연 예산이다 (설계문 §6.4.1). 히트할 때 재검증하거나 결과 variant 를 페이로드와
함께 저장해 "개선" 하면 안 된다. 재검증은 이 캐시가 없애려던 비용을 그대로 되돌리고,
variant 를 저장하면 음성 캐싱이 뒷문으로 들어와 한 번의 네트워크 실패가 TTL 내내
계정을 후보에서 지운다.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Mapping
from typing import Any

from codex_swap.core import paths
from codex_swap.core.config import Settings


def _seconds(value: object) -> int | None:
    """bash 의 `jq -r '.ts // 0'` 와 `^[0-9]+$` 를 합친 술어. 받으면 초, 아니면 None.

    두 단계를 하나로 합치는 이유는 중간값(문자열로 렌더된 ts)이 아무 데도 쓰이지 않기
    때문이다. 통과하는 값의 집합만 같으면 된다.

    jq 의 `//` 는 missing·null·**false** 를 접어 `0` 으로 만들지만 `true` 는 접지 않아
    `true` 로 렌더되고 정규식에서 떨어진다. 그리고 jq 는 정수값 double 을 소수점 없이
    렌더하므로 `ts: 1788137073.0` 은 `"1788137073"` 이 되어 **통과한다** —
    `str(value).isdigit()` 로 쓰면 바로 여기서 갈린다 (설계문 §6.4).

    문자열에 `isascii()` 를 함께 거는 것은 bash 를 좁히는 게 아니라 맞추는 것이다.
    `"²"` 는 `str.isdigit()` 이 참인데 `int()` 는 던진다 — 캐시 파일은 사용자가 고칠 수
    있고, 여기서 던지면 stderr 로 트레이스백이 나가 계약 2 를 깬다.
    """
    if value is None:
        return 0
    if isinstance(value, bool):
        return 0 if value is False else None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value >= 0 else None
    if isinstance(value, str):
        return int(value) if value.isascii() and value.isdigit() else None
    return None


def _opener(path: str, flags: int) -> int:
    """`open()` 이 temp 를 처음부터 0600 으로 만들게 한다."""
    return os.open(path, flags, 0o600)


def read(settings: Settings, label: str, *, now: float | None = None) -> dict[str, Any] | None:
    """살아 있는 캐시 항목. 미스면 None.

    라벨은 **JSON 키로만** 쓴다. 경로 요소가 되지 않으므로 여기서는 라벨 검증이 필요
    없다 — 검증은 슬롯 디렉토리를 실제로 여는 `store` 의 몫이다.
    """
    path = paths.cache_path(settings)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # 없다 · 못 읽는다 · 깨졌다. bash 는 셋 다 `return 1` 로 접는다. 미스의 대가는
        # 프로브 한 번뿐이라 시끄러울 이유가 없다.
        return None

    entry = doc.get(label) if isinstance(doc, dict) else None
    ts = _seconds(entry.get("ts")) if isinstance(entry, dict) else None
    # bash 의 `date +%s` 는 초를 버린 정수다. 실수 시계로 비교하면 TTL 경계의 1 초 안에서
    # 우리만 먼저 만료시킨다 — 잃는 것은 히트 하나뿐이라 크지 않지만, 정수로 두면
    # 캐시 나이가 재현 가능한 값이 되어 테스트가 경계를 정확히 집을 수 있다.
    at = int(time.time() if now is None else now)

    # 히트 조건은 이 셋의 연언 **하나**다. bash 는 jq 추출 → 정규식 → 산술 비교의 3 단
    # 이지만 어느 단에서 어긋나든 결말은 똑같이 미스라, 단계로 쪼개면 갈래만 늘고 얻는
    # 것이 없다. 특히 "객체인가" 를 빼면 안 된다 — 항목이 숫자면 bash 의 `.ts` 가 jq
    # 오류로 죽어 미스가 되는데, Python 에서는 그 자리가 AttributeError 다.
    hit = isinstance(entry, dict) and ts is not None and at - ts <= settings.cache_ttl
    return entry if hit else None


def write(
    settings: Settings,
    label: str,
    payload: Mapping[str, Any],
    *,
    now: float | None = None,
) -> bool:
    """`Ok` 페이로드 하나를 캐시에 얹는다. 성공이면 True.

    실패는 조용하다 — bash 도 `codex_account_cache_write ... || true` 로 부른다. 캐시를
    못 쓴 대가는 다음 호출의 프로브 한 번이지 회전 실패가 아니다.
    """
    at = int(time.time() if now is None else now)

    # 루트를 만드는 경로는 **전부** 여기를 지난다 (설계문 §7.5 D5). bash 의
    # `cache_write` 는 자기가 `mkdir -p` 만 하고 `chmod 700` 을 하지 않아서, rotate 가
    # 먼저 돌면 자격증명 디렉토리가 umask 모드(흔히 0755)로 생긴다.
    try:
        paths.ensure_root(settings)
    except OSError:
        return False

    path = paths.cache_path(settings)

    # 문서 하나를 통째로 읽고 라벨 키만 갈아끼워 다시 쓴다. 두 프로세스가 동시에 쓰면
    # 나중 쓰기가 먼저 쓰기의 항목을 덮어 지운다 — bash 도 같은 위험을 진다. 잃는 것이
    # 캐시 항목 하나, 즉 다음 호출의 프로브 한 번이라서 여기에 락을 들이지 않는다.
    # 진짜 락(mkdir)은 자격증명을 바꾸는 `store.switch` 에만 있다.
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {}
    if not isinstance(doc, dict):
        doc = {}

    # bash 는 `ts` 를 호출자(`codex_account_usage`)가 jq 로 얹었다. 읽는 쪽 TTL 판정과
    # 한 쌍이므로 이쪽으로 옮긴다 — 디스크에 남는 바이트는 같다.
    doc[label] = {**payload, "ts": at}

    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        # bash 는 rename **뒤에** chmod 해서 그 사이 캐시가 umask 모드로 보이는 창이
        # 있다. 만들 때부터 0600 이면 그 창이 아예 없다 — 여기 담기는 것은 계정 email 이다.
        with open(tmp, "w", encoding="utf-8", opener=_opener) as fh:
            json.dump(doc, fh)
        # 앞선 실행이 남긴 temp 를 덮어썼다면 `O_CREAT` 의 mode 는 걸리지 않는다.
        os.chmod(tmp, 0o600)
        # 같은 디렉토리 안의 rename 이라 원자적이다 — 반쪽 쓰인 캐시가 생기지 않는다.
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        # 직렬화되지 않는 payload 도 여기로 온다. bash 도 `jq --argjson` 이 그것을
        # 거절해 같은 자리에서 1 을 돌려주므로, 예외로 새어 나가면 계약 2 를 깬다.
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        return False
    return True


def clear(settings: Settings) -> None:
    """캐시를 통째로 버린다. 전환 직후 `store` 가 부른다.

    무효해지는 것은 활성 라벨의 항목뿐인데도 파일째 지우는 것은 bash 와 같다. 한 키만
    빼내려면 위와 같은 read-modify-write 를 한 번 더 돌아야 하고, 남는 항목도 어차피
    TTL 안에서만 유효하다.
    """
    with contextlib.suppress(OSError):
        paths.cache_path(settings).unlink(missing_ok=True)
