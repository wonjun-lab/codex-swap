r"""전환 원장.

bash `codex_account_switch` 의 마지막 append 한 줄을 옮긴 것이다. 원장이 남기는 것은
"언제 · 어디서 어디로 · 왜" 셋뿐이고, **자격증명은 한 조각도 담지 않는다** (설계문 §5
계약 7). bash 판에는 그것만 확인하는 테스트가 따로 있다.

레코드는 탭으로 나뉜 세 칸이며 줄 하나가 전환 하나다.

    2026-09-05T11:28:33+09:00\twork -> personal\treached (95% -> 40%)
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from codex_swap.core import paths
from codex_swap.core.config import Settings

UNKNOWN_LABEL = "unknown"
"""떠나는 라벨을 모를 때 적는 값.

bash 의 `${active_label:-unknown}` 이다. `codex_account_active_label` 은 실패하면
**빈 문자열**을 돌려주고 `:-` 는 미설정과 빈 문자열을 똑같이 접으므로, `None` 뿐 아니라
빈 문자열도 여기로 접어야 같은 줄이 나온다 (설계문 §6.1 과 같은 함정).
"""

DEFAULT_REASON = "manual"
"""bash `reason="${2:-manual}"`. 사유가 비면 세 번째 칸이 통째로 비어 레코드가 두 칸처럼
보이므로, 형식 불변식을 쓰는 쪽에서 지킨다."""

# 탭과 개행은 레코드 구분자 그 자체다. bash 는 reason 을 `printf` 에 그대로 보간해서,
# 개행이 든 문자열이 오면 전환 하나가 두 줄로 갈라지고 뒤쪽 줄은 타임스탬프가 없는
# 쓰레기가 된다. 지금 호출부의 사유는 전부 policy 가 만든 제어문자 없는 문자열이라
# 관측된 적은 없지만, 형식은 값이 아니라 쓰는 자리에서 지켜야 깨지지 않는다.
_SEPARATORS = str.maketrans("\t\n\r", "   ")


def _stamp(now: datetime | None = None) -> str:
    """bash `date -Iseconds` 와 같은 문자열.

    GNU 와 BSD 의 `date` 는 이 옵션에서 출력이 갈린다(설계문 §2.3). 그래서 흉내내지 않고
    GNU 형태 하나로 못박는다 — 초까지, 오프셋 포함, 소수점 없음.

    `datetime.now()` 만 쓰면 naive 라 `isoformat()` 에 오프셋이 붙지 않는다. UTC 로 받아
    `astimezone()` 으로 로컬 존을 명시적으로 입히고, `timespec="seconds"` 로 마이크로초를
    떨어뜨린다.
    """
    moment = datetime.now(UTC) if now is None else now
    return moment.astimezone().isoformat(timespec="seconds")


def format_record(
    from_label: str | None,
    to_label: str,
    reason: str,
    now: datetime | None = None,
) -> str:
    """원장 한 줄. 개행까지 포함한다.

    I/O 를 하지 않으므로 "토큰이 새지 않는가" 를 파일 없이 검사할 수 있다.
    """
    departing = (from_label or "").strip() or UNKNOWN_LABEL
    why = reason.translate(_SEPARATORS).strip() or DEFAULT_REASON
    return f"{_stamp(now)}\t{departing} -> {to_label}\t{why}\n"


def append(
    settings: Settings,
    *,
    to_label: str,
    reason: str,
    from_label: str | None = None,
    now: datetime | None = None,
) -> bool:
    """원장에 한 줄 덧붙인다. 실제로 썼으면 True.

    `reason` 은 사람이 읽을 짧은 사유 문자열이다 — **여기에 자격증명이 들어오지 않게 하는
    책임은 호출자에게 있다.** 그래서 이 함수는 라벨 둘과 사유 하나만 받는다. 프로브 응답
    · `Usage` · `auth.json` 조각처럼 페이로드를 통째로 넘길 자리를 만들지 않으면, 실수로
    토큰을 흘리려면 문자열을 일부러 조립해 넣어야 한다.

    bash 는 이 append 를 `2>/dev/null || true` 로 감싼다. 여기까지 왔다는 것은 자격증명
    교체가 **이미 끝났다**는 뜻이라, 기록에 실패했다고 rotate 를 실패로 만들면 성공한
    전환을 실패로 보고하게 된다. 게다가 rotate 의 stderr 는 매 codex 호출마다 사용자
    터미널에 그대로 흘러서(설계문 §5.1), 여기서 올라간 트레이스백은 화면에 실린다.
    그래서 `OSError` 를 삼키고 불리언으로만 알린다.

    루트 생성은 `paths.ensure_root` 하나만 지난다. bash 의 캐시 쓰기가 자기만의
    `mkdir -p` 를 써서 `chmod 700` 을 빠뜨렸고, 그쪽이 먼저 돌면 자격증명 디렉토리가
    umask 모드로 생겼다 (설계문 D5). 파일명도 같은 이유로 여기서 조립하지 않는다 —
    경로를 만드는 곳이 둘이 되는 순간 그 둘이 갈라질 자리가 생기고, 이 이관이 지우려는
    것이 정확히 그 종류의 drift 다.
    """
    try:
        paths.ensure_root(settings)
        with paths.log_path(settings).open("a", encoding="utf-8") as fh:
            fh.write(format_record(from_label, to_label, reason, now))
    except OSError:
        return False
    return True


def last_switch(settings: Settings) -> str | None:
    """원장이 기억하는 **마지막으로 걸어 둔 라벨**. 기록이 없으면 None.

    이것과 지금 활성이 어긋나면 누군가 우리를 거치지 않고 자격증명을 바꾼 것이다. 실제로
    그런 환경이 있다 — ChatGPT 데스크톱 앱이 자기 codex 를 `CODEX_HOME=~/.codex` 로 띄워
    두고 같은 `auth.json` 을 쓴다. 그쪽이 자기 세션으로 파일을 되돌려 놓으면 우리 원장에는
    `A -> B` 만 남고 `B -> A` 는 남지 않아, 출발점이 계속 A 인 이상한 이력이 된다.

    **마지막 줄만 읽는다.** 원장은 계속 자라는 파일이고 여기서 필요한 것은 한 줄뿐이라,
    통째로 읽으면 오래 쓴 기기에서 이 검사 하나가 가장 비싼 일이 된다.
    """
    try:
        with paths.log_path(settings).open("rb") as fh:
            try:
                fh.seek(-4096, os.SEEK_END)
            except OSError:
                fh.seek(0)  # 4KB 보다 짧은 파일
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return None

    for line in reversed(tail.splitlines()):
        # `시각 \t 출발 -> 도착 \t 사유`
        parts = line.split("\t")
        if len(parts) < 2 or "->" not in parts[1]:
            continue
        arrived = parts[1].split("->", 1)[1].strip()
        if arrived and arrived != UNKNOWN_LABEL:
            return arrived
    return None
