"""upstream codex 바이너리 해석 — `lib/codex-path.sh` 포트.

보조 유틸이 아니라 1 급 컴포넌트다. wrapper(`codex.sh`)는 자기가 이미 해석한 경로를
`CODEX_ACCOUNT_BIN` 으로 넘겨주지만, Claude 훅(`SessionStart`)과 사람이 터미널에서 직접
부르는 두 경로는 넘기지 않는다 (설계문 §8). 그 둘에게는 여기가 유일한 해석 수단이다.

탐색 순서는 bash 와 같고, 순서 자체가 계약이다.

    1. `CODEX_REAL_BIN`                 사용자가 직접 지정
    2. `~/.local/bin` 을 뺀 PATH        wrapper 가 거기 설치되므로 빼야 자기 자신을 안 집는다
    3. standalone 설치 경로             PATH 에 없을 수 있는 공식 설치 위치
    4. nvm 버전 디렉토리, 최신부터      축소 PATH 로 부르는 호출자(훅·systemd)의 마지막 보루
    5. `~/.local/bin/codex` 직접        2 티어가 뺀 자리를 맨 뒤에서 되살린다 (아래 참조)

5 티어는 2 티어의 배제가 지나쳤던 것을 되돌린다. 그 배제는 wrapper 재귀를 막으려는
것인데 재귀를 실제로 막는 것은 `is_wrapper()` 이고, 디렉토리를 통째로 빼면 codex 를
거기에 평범하게 설치한 기기가 아무것도 못 찾는다. 맨 뒤에 두므로 우선순위는 그대로다.

`§6.2` 의 PATH 보정도 여기 함께 둔다. node 의존이 사라진 것은 **우리 코드에서**지
시스템에서가 아니다 — 탐색 결과가 `#!/usr/bin/env node` 스크립트라 자식에게 node 를
찾을 수 있는 PATH 를 줘야 한다.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Mapping
from pathlib import Path

WRAPPER_MARKER = b"lib/codex-path.sh"
"""wrapper 판별 marker. wrapper 는 이 lib 을 source 하는 셸 스크립트다."""

WRAPPER_SCAN_BYTES = 8192
"""marker 를 찾는 창. bash 의 `head -c 8192` 와 **정확히** 같아야 한다."""

STANDALONE_TAIL = "packages/standalone/current/bin/codex"

_DIGIT_RUN = re.compile(r"(\d+)")


class UpstreamNotFound(Exception):
    """codex 바이너리를 해석하지 못했다.

    bash 는 이 자리에서 stderr 에 진단을 찍고 1 을 돌려주지만, 우리는 예외로 올린다.
    rotate 경로의 stderr 는 매 codex 호출마다 사용자 터미널에 그대로 노출되는 채널이라
    의도된 통지 외에는 아무것도 실으면 안 되기 때문이다 (계약 2, 설계문 §5.1). 진단은
    `reason` 으로 들고 올라가서, CLI 는 그대로 찍고 `rotate.py` 는 fail-open 경계에서
    "전환 안 함" 으로 접는다.
    """


def _env_of(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _raw(env: Mapping[str, str] | None, name: str) -> str | None:
    """bash `${X:-}` 의미 — 미설정과 **빈 문자열**이 같다 (설계문 §6.1).

    `CODEX_REAL_BIN=""` 는 bash 에서 "지정 안 함" 이지, 빈 경로를 지정한 것이 아니다.
    """
    v = _env_of(env).get(name)
    return None if v is None or v == "" else v


def _home(env: Mapping[str, str] | None) -> Path:
    return Path(_env_of(env).get("HOME") or Path.home())


def _normalize_dir(directory: str) -> str:
    """`codex_normalize_path_directory` — 후행 슬래시 제거 + 물리 경로 해소.

    PATH 항목 비교를 문자열로 하면 `~/.local/bin/` 처럼 슬래시 하나로, 또는 심링크
    경유 경로로 필터가 뚫린다. 실재하지 않는 경로는 bash 와 마찬가지로 손대지 않고
    그대로 돌려준다.
    """
    while directory != "/" and directory.endswith("/"):
        directory = directory[:-1]
    if directory and os.path.isdir(directory):
        return os.path.realpath(directory)
    return directory


def is_wrapper(candidate: str | os.PathLike[str]) -> bool:
    """후보가 wrapper(우리 자신)인가.

    wrapper 를 upstream 으로 집으면 무한 재귀다. 판정은 두 조건의 **곱**이고 둘 다
    필요하다 — shebang 검사는 259 MB 짜리 진짜 바이너리를 통째로 훑지 않게 하고,
    marker 검사는 아무 셸 스크립트나 wrapper 로 몰지 않게 한다. 8192 바이트 창도
    그대로 둔다. 넓히면 어떤 파일이 wrapper 로 분류되는지가 달라진다.
    """
    path = os.fspath(candidate)
    if not os.path.isfile(path):
        return False
    try:
        with open(path, "rb") as fh:
            head = fh.read(WRAPPER_SCAN_BYTES)
    except OSError:
        # bash 의 `head -c` 도 읽기 실패면 빈 출력이라 shebang 검사에서 탈락한다.
        return False
    # dotfiles wrapper 만 알아보던 판정이다. codex-swap 이 직접 놓는 wrapper(`wiring.MARKER`)도
    # 우리 자신이다 — 못 알아보면 upstream 이 사라진 기기에서 그 wrapper 를 진짜 codex 로 집고,
    # `exec` 가 자기 자신을 끝없이 다시 띄운다. 두 marker 가 갈라지지 않는지는 테스트가 묶는다.
    return head[:2] == b"#!" and (WRAPPER_MARKER in head or b"# codex-swap wrapper" in head)


def is_usable_binary(candidate: str | os.PathLike[str]) -> bool:
    """실행 가능한 upstream 후보인가.

    디렉토리도 `-x` 를 통과한다. 그대로 넘기면 exec 가 "Is a directory" 로 126 에
    죽으므로 bash 가 `-f` 를 함께 건 것처럼 여기서도 일반 파일인지까지 본다.
    """
    path = os.fspath(candidate)
    if not path or not os.path.isfile(path) or not os.access(path, os.X_OK):
        return False
    return not is_wrapper(path)


def path_without_local_bin(env: Mapping[str, str] | None = None) -> str:
    """`~/.local/bin` 을 뺀 PATH 문자열.

    wrapper 가 바로 거기 설치되므로(`~/.local/bin/codex` → `codex.sh` 심링크) 이 한
    항목을 빼는 것이 자기 자신을 upstream 으로 집는 재귀를 막는 장치다.
    """
    raw = _env_of(env).get("PATH") or ""
    entries = raw.split(os.pathsep) if raw else []
    # bash `IFS=: read -r -a entries` 는 **후행** 구분자 뒤에 빈 필드를 만들지 않는다.
    # Python `split` 은 만든다. 그 차이가 `PATH=/usr/bin:` 처럼 콜론으로 끝나는 환경에서
    # 갈린다 — 남은 빈 항목은 곧 cwd 이고, `shutil.which` 가 거기서 찾은 후보를 디렉토리
    # 성분 없는 상대 이름 `codex` 로 돌려준다. discovery 가 실제로 도는 경로는
    # CODEX_ACCOUNT_BIN 이 없는 훅과 터미널 호출(§8)이라 그 순간의 cwd 가 임의인데,
    # 그 값이 자격증명 슬롯을 CODEX_HOME 으로 든 채 Popen 에 넘어간다.
    if entries and entries[-1] == "":
        entries.pop()
    target = _normalize_dir(str(_home(env) / ".local/bin"))
    kept: list[str] = []
    for entry in entries:
        if _normalize_dir(entry) == target:
            continue
        # bash 는 결과가 아직 빈 동안 만난 항목을 append 가 아니라 **대입**으로 넣어서,
        # 선행 빈 항목("" = cwd)이 조용히 사라진다. 그 항목이 남으면 자식이 cwd 에서
        # codex 를 찾을 수 있으므로 재현하는 편이 안전하기도 하다.
        if not kept and entry == "":
            continue
        kept.append(entry)
    return os.pathsep.join(kept)


def _standalone_candidates(env: Mapping[str, str] | None) -> list[Path]:
    """공식 standalone 설치 위치. PATH 에 없을 수 있다.

    이 티어가 없으면 codex 를 `~/.local/bin` 에만 심어둔 기기에서 2 티어가 그 항목을
    빼는 순간 아무것도 못 찾고 wrapper 가 통째로 죽는다. `CODEX_HOME` 이 없을 때 두
    후보가 같아지는 것은 bash 와 동일하며, 중복 검사는 해롭지 않다.
    """
    home = _home(env)
    codex_home = Path(_raw(env, "CODEX_HOME") or home / ".codex")
    return [codex_home / STANDALONE_TAIL, home / ".codex" / STANDALONE_TAIL]


def _version_key(text: str) -> tuple[tuple[int, int | str], ...]:
    """`sort -rV` 의 정렬 키.

    단순 문자열 역정렬은 v9 를 v20 앞에 둔다 (설계문 §2.3). 숫자 런을 정수로 비교해야
    최신 node 를 먼저 본다. 조각마다 태그(0=문자, 1=숫자)를 붙이는 것은 숫자와 문자를
    직접 비교해 TypeError 가 나는 것을 막기 위해서다.
    """
    return tuple((1, int(part)) if part.isdigit() else (0, part) for part in _DIGIT_RUN.split(text))


def _nvm_candidates(env: Mapping[str, str] | None) -> list[Path]:
    """nvm 전역 설치. 최신 node 부터.

    npm(nvm) 전역 설치는 인터랙티브 셸 밖에서 PATH 에 없다 — 축소 PATH 로 부르는
    호출자(systemd 유닛·데몬)가 여기까지 오지 못하면 exit 127 로 죽는다(실측
    2026-08-13).
    """
    root = _home(env) / ".nvm/versions/node"
    try:
        # bash 는 셸 경로명 확장(`ls -1 .../*/bin/codex`)을 쓰고, dotglob 이 꺼진 기본
        # 상태에서 `*` 는 `.` 로 시작하는 이름을 매치하지 않는다. `Path.glob` 은 매치하므로
        # 명시적으로 뺀다 — 안 그러면 정상 버전 디렉토리가 전부 탈락했을 때만 드러나는,
        # 재현하기 어려운 갈림이 남는다.
        found = [p for p in root.glob("*/bin/codex") if not p.parent.parent.name.startswith(".")]
    except OSError:
        return []
    return sorted(found, key=lambda p: _version_key(str(p)), reverse=True)


def _local_bin_candidates(env: Mapping[str, str] | None) -> list[Path]:
    """`~/.local/bin/codex`. **마지막** 티어다.

    PATH 조회는 이 디렉토리를 통째로 뺀다(`path_without_local_bin`). 그 배제는 wrapper
    재귀를 막으려는 장치인데, 재귀를 실제로 막는 것은 `is_wrapper()`(shebang 과 marker 의 곱)
    이고 디렉토리 배제는 그 정밀한 검사가 **도달하기 전에** 후보를 없앤다. 그 결과 codex
    를 여기에 평범하게 설치한 기기 — npm 전역 prefix · pipx · 수동 설치 — 에서는 다른
    티어가 없으면 아무것도 못 찾고 도구가 통째로 죽는다.

    그래서 후보로는 되살리되 **맨 뒤**에 둔다. 위 티어를 이기지 못하므로 wrapper 가 놓인
    기기의 우선순위는 그대로이고, 실제로 wrapper 면 `is_usable_binary` 가 거른다 — 배제의
    이유였던 그 경우만 정확히 남는다.
    """
    return [_home(env) / ".local/bin/codex"]


def find_upstream(env: Mapping[str, str] | None = None) -> Path:
    """upstream codex 실행 파일을 찾는다. 못 찾으면 `UpstreamNotFound`."""
    override = _raw(env, "CODEX_REAL_BIN")
    if override is not None:
        if is_usable_binary(override):
            return Path(override)
        # 다음 티어로 흘리지 않는 것이 핵심이다. 사용자가 명시한 경로를 조용히
        # 무시하고 다른 바이너리로 돌면 어느 codex 가 실행됐는지 알 수 없어진다.
        raise UpstreamNotFound(f"CODEX_REAL_BIN is not a runnable upstream binary: {override}")

    # bash 가 `command -v` 대신 `type -P` 를 쓴 이유가 그대로 적용된다 — 셸 함수·alias 를
    # 잡으면 경로가 아니라 이름("codex")이 나오고, 그 이름을 원래 PATH 로 다시 부르면
    # wrapper 로 되돌아가 재귀한다. `shutil.which` 는 애초에 PATH 위 파일만 본다.
    found = shutil.which("codex", path=path_without_local_bin(env))
    if found and is_usable_binary(found):
        # PATH 의 빈 항목에서 찾으면 `which` 가 디렉토리 성분 없는 `codex` 를 돌려준다.
        # 그대로 두면 §6.2 의 PATH 보정이 통째로 사라진다(부모 디렉토리가 빈 문자열이라
        # 붙일 것이 없다). bash 의 `type -P` 는 같은 경우 `./codex` 를 준다. 절대 경로로
        # 정규화해 보정이 항상 성립하게 만든다 — 이 값은 그대로 Popen 에 넘어간다.
        return Path(os.path.abspath(found))

    # 티어를 **게으르게** 잇는다. 튜플로 펼치면 standalone 이 이길 때도 nvm 쪽이 먼저
    # 평가되어, 쓰지도 않을 `~/.nvm/versions/node/*/bin/codex` glob 이 매번 돈다.
    # discovery 는 훅과 터미널 호출마다 실리는 경로라 그 한 번이 그냥 낭비다.
    for tier in (_standalone_candidates, _nvm_candidates, _local_bin_candidates):
        for candidate in tier(env):
            if is_usable_binary(candidate):
                return candidate

    raise UpstreamNotFound("could not find the codex binary")


def resolve_codex_bin(env: Mapping[str, str] | None = None) -> Path:
    """wrapper 가 넘긴 값을 우선하고, 없으면 탐색한다 (설계문 §8).

    `CODEX_ACCOUNT_BIN` 은 검증하지 않는다 — bash 도 그대로 쓴다. 이 값은 wrapper 가
    방금 `codex_find_upstream` 으로 해석해 넘긴 결과이므로 재검증은 핫패스에 파일 읽기를
    하나 더 얹을 뿐이고, 여기서 거부하면 wrapper 경로에서만 두 구현이 갈린다.
    """
    given = _raw(env, "CODEX_ACCOUNT_BIN")
    if given is not None:
        return Path(given)
    return find_upstream(env)


def path_with_bin_dir(
    codex_bin: str | os.PathLike[str], env: Mapping[str, str] | None = None
) -> str:
    """탐색된 경로의 **lexical** 부모를 앞에 붙인 PATH (설계문 §6.2, 검증됨).

    nvm 설치에서 탐색 결과는 네이티브 바이너리가 아니라 `#!/usr/bin/env node` 스크립트다.
    축소 PATH(`/usr/bin:/bin`)로 그대로 띄우면 실측 rc=127,
    stderr `/usr/bin/env: 'node': No such file or directory` 로 죽는다.

    `Path(...).resolve().parent` 를 쓰면 안 된다. 심링크를 따라가면
    `.../lib/node_modules/@openai/codex/bin` 으로 가는데 거기에 node 는 **없다**. node 가
    있는 쪽은 심링크가 놓인 자리, 즉 lexical 부모(`.../node/v24.14.0/bin`)다. `.mjs` 는
    이 보정을 하지 않았는데, 대화형 PATH 에 node 가 이미 있어 여태 드러나지 않았을 뿐이다.
    """
    bin_dir = os.path.dirname(os.fspath(codex_bin))
    current = _env_of(env).get("PATH") or ""
    if not bin_dir:
        # 디렉토리 성분이 없는 이름("codex")뿐이면 얹을 것이 없다. "." 로 채우면 자식
        # PATH 에 cwd 를 태우는 셈이라 보정이 아니라 새 구멍이 된다.
        return current
    return f"{bin_dir}{os.pathsep}{current}" if current else bin_dir


def env_with_bin_dir(
    codex_bin: str | os.PathLike[str], env: Mapping[str, str] | None = None
) -> dict[str, str]:
    """`path_with_bin_dir` 를 적용한 자식 환경. 프로브가 이걸 그대로 subprocess 에 넘긴다."""
    merged = dict(_env_of(env))
    path = path_with_bin_dir(codex_bin, env)
    if path:
        # 빈 값은 넣지 않는다. PATH="" 는 미설정과 달라서 자식의 탐색 규칙을 바꾼다.
        merged["PATH"] = path
    return merged
