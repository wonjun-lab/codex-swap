"""활성 계정의 신원 — `auth.json` 의 id_token 에서 email 을 뽑는다.

계약 1(설계문 §5): 활성 계정은 `~/.codex/auth.json` 의 email **로만** 판정한다. 별도
상태 필드를 두지 않는 이유는, 사용자가 손으로 `codex login` 을 해도 표식과 실제가
어긋나면 안 되기 때문이다. 그래서 이 모듈이 신원 판정의 유일한 출처다.

여기는 자격증명을 직접 다루는 자리다. **토큰은 어떤 경로로도 밖으로 나가지 않는다** —
반환값·로그·예외 메시지 어디에도. rotate 경로의 stderr 는 매 codex 호출마다 사용자
터미널로 그대로 흐르므로(§5.1), 여기서 새는 한 줄이 곧 화면에 실린 토큰이다. bash 가
모든 단계를 `2> /dev/null` 로 덮고 `return 1` 로만 답한 것도 같은 이유였고, 그 규율을
그대로 옮긴다 — 이 모듈의 함수는 실패를 전부 `None` 으로 접고 예외를 올리지 않는다.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Iterable
from pathlib import Path

_AUTH_FILENAME = "auth.json"

# bash 의 `tr '_-' '/+'` — base64url 알파벳을 표준 알파벳으로 되돌린다. `-` 가 집합
# 끝에 와서 범위가 아니라 리터럴인 것까지 같은 매핑이다.
_B64URL_TO_STD = str.maketrans("_-", "/+")


def email_of(auth_file: Path) -> str | None:
    """`auth.json` 한 개에서 email 을 읽는다. 못 읽으면 `None`.

    bash `codex_account_email_of` 의 포트다. 실패 갈래(파일 없음·권한 없음·JSON 깨짐·
    JWT 아님·email 없음)를 구분하지 않는 것은 호출부가 그 구분으로 하는 일이 없기
    때문이다 — 어느 쪽이든 "이 슬롯의 신원을 모른다" 로 같다.
    """
    try:
        raw = auth_file.read_bytes()
    except OSError:
        # bash 의 `[[ -r "$auth_file" ]]` 자리. 디렉토리·심링크 깨짐도 여기로 온다.
        return None

    try:
        doc = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None

    tokens = doc.get("tokens") if isinstance(doc, dict) else None
    id_token = tokens.get("id_token") if isinstance(tokens, dict) else None
    # jq 의 `// empty` 는 missing·null·**false** 를 접는다. 문자열이 아닌 값(숫자 등)은
    # jq -r 이 문자열로 렌더링해 통과시키지만, 그런 auth.json 은 codex 가 만들지 않고
    # 어차피 base64 단계에서 깨진다. 문자열이 아니면 신원이 없는 것으로 본다.
    if not isinstance(id_token, str) or not id_token:
        return None

    claims = _decode_jwt_payload(_payload_segment(id_token))
    if claims is None:
        return None

    email = claims.get("email")
    return email if isinstance(email, str) and email else None


def active_email(default_home: Path) -> str | None:
    """지금 codex 가 실제로 쓰는 계정의 email.

    슬롯 사본이 아니라 **기본 홈**을 본다. 슬롯 사본은 토큰이 갱신되며 뒤처지고,
    기본 홈이 언제나 사실이다.
    """
    return email_of(default_home / _AUTH_FILENAME)


def active_label(accounts_root: Path, email: str, labels: Iterable[str]) -> str | None:
    """활성 email 과 같은 계정을 담은 **첫** 슬롯. 없으면 `None`.

    `None` 은 오류가 아니라 정상 상태다 — 슬롯에 없는 계정으로 사용자가 직접 로그인한
    경우이며, 그때 정책은 아무것도 건드리지 않는다(`policy.decide` 의 `active_label is
    None` 갈래).

    라벨을 **인자로 받는다.** 여기서 디렉토리를 열거하지 않는 것은 게으름이 아니라
    경계다. 슬롯 열거는 라벨 검증(단일 경로 요소 · 심링크 슬롯 거부 · 슬롯 안
    `auth.json` 의 realpath 봉쇄, §6.6)과 한 몸이고 그 관문은 `store` 가 소유한다. 이
    모듈이 자기 열거를 따로 가지면 검증을 우회하는 두 번째 경로가 생기는데, bash 에서
    터진 사고가 정확히 그것이었다 — `use` 가 거부하던 심링크 슬롯을 열거 경로로 들어온
    rotate 가 골라 루트 밖 자격증명으로 실제 전환했다.

    **순서가 결과를 바꾼다.** 같은 email 을 여러 라벨로 등록하는 것을 아무도 막지
    않으므로(§6.5), 일치가 둘이면 먼저 열거된 쪽이 이긴다. 그리고 그 라벨이 떠날 때
    자격증명 sync-back 을 받는 슬롯이다(계약 5) — 진 쪽의 사본은 갱신되지 않고 만료
    토큰으로 썩는다. bash 의 glob 순서와 Python `iterdir()` 순서는 같지 않으므로,
    호출자는 결정적인 순서로 넘겨야 한다(`store` 는 정렬 순서로 넘긴다).
    """
    if not email:
        # 활성 email 을 못 읽은 상태. 어떤 슬롯과도 일치시키지 않는다 — bash 도
        # `codex_account_active_email` 이 실패하면 라벨 순회 전에 끝냈다.
        return None

    for label in labels:
        if not label:
            continue
        if email_of(accounts_root / label / _AUTH_FILENAME) == email:
            return label
    return None


def _payload_segment(id_token: str) -> str:
    """JWT 의 두 번째 점 구분 조각.

    bash 는 `cut -d. -f2` 를 쓰는데, `-s` 없는 cut 은 구분자가 하나도 없는 줄을
    **줄 전체 그대로** 낸다. 점 없는 값이 오면 그 값 자체가 payload 가 되는 셈이다.
    실질적으로는 뒤 단계에서 깨지므로 결과가 같지만, 조각 선택 규칙 자체를 갈라 두면
    언젠가 다른 입력에서 갈라진다.
    """
    parts = id_token.split(".")
    return parts[1] if len(parts) > 1 else id_token


def _decode_jwt_payload(segment: str) -> dict[str, object] | None:
    """base64url payload → 클레임 dict. 실패는 전부 `None`.

    이 함수가 예외를 흘리면 안 되는 이유가 §5.1 이다 — 트레이스백은 프레임에 실린
    payload 까지 함께 stderr 로 내보낸다. 그래서 잡은 예외를 다시 포장해 올리지도
    않는다(포장한 메시지에 입력을 담기 쉽다).
    """
    payload = segment.translate(_B64URL_TO_STD)
    # bash 는 길이 % 4 가 2·3 일 때만 패딩을 붙이고 1 이면 그대로 넘겨 `base64 -d` 를
    # 실패시킨다. `-len % 4` 는 나머지 1 에 `=` 셋을 붙이지만 Python 역시 "4 의 배수보다
    # 1 많은 데이터 문자" 로 거부하므로, 네 나머지 전부에서 두 판의 결론이 같다.
    payload += "=" * (-len(payload) % 4)

    try:
        # validate=True 가 필수다. GNU `base64 -d` 는 `-i` 없이 **개행 외의** 알파벳 밖
        # 문자를 오류로 끝내는데(개행은 문서상 허용된다), Python 기본값(False)은 그것들을
        # 조용히 **버리고** 디코드한다. 그러면 bash 가 거부하는 쓰레기 payload 에서
        # 우리만 email 을 만들어낸다.
        claims = json.loads(base64.b64decode(payload.encode("utf-8"), validate=True))
    # `UnicodeError` 로 잡는다. `UnicodeDecodeError` 만 잡으면 **형제**인
    # `UnicodeEncodeError` 가 새어 나간다 — Python 의 `json.loads` 는 짝 없는
    # 서로게이트(`"\ud800"`)를 통과시키므로, id_token 에 그런 이스케이프가 든 auth.json 은
    # 위의 `.encode("utf-8")` 에서 예외가 되어 rotate 경로 밖으로 트레이스백이 나간다.
    # rotate 의 stderr 는 매 codex 호출에서 사용자 터미널로 흐르므로(계약 2 · §5.1)
    # 그건 그대로 사고다. 게다가 슬롯 열거 순서에 따라 터지고 안 터지고가 갈려서,
    # 오염된 슬롯 하나가 그 뒤에 정렬된 모든 슬롯의 신원 해석을 막는다.
    #
    # bash 는 같은 입력에서 jq 가 파싱 실패하고 `2> /dev/null` 이 삼켜 조용히 rc=1 이다.
    except (binascii.Error, UnicodeError, json.JSONDecodeError):
        return None

    # jq 의 `.email` 은 객체가 아닌 값에서 오류로 끝난다(→ 빈 출력 → 신원 없음).
    return claims if isinstance(claims, dict) else None
