"""환경변수 → Settings.

bash 의 파라미터 확장 의미를 한 곳에서 재현한다. 흩어 두면 호출부마다 미묘하게
달라지는데, 그 차이가 조용한 divergence 의 가장 흔한 원인이다 (설계문 §6.1).
"""

from __future__ import annotations

import contextlib
import json
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
    으로 접는다.
    """


def _raw(name: str) -> str | None:
    """bash `${X:-default}` 의미: 미설정과 **빈 문자열**을 똑같이 취급한다.

    `os.getenv(name, default)` 는 빈 문자열을 그대로 돌려주므로 그대로 쓰면 안 된다.
    `CODEX_ACCOUNTS_DIR=""` 는 bash 에서 기본 경로인데 `Path("")` 는 cwd 가 된다.
    """
    v = os.environ.get(name)
    return None if v is None or v == "" else v


CONFIG_NAME = "config.json"


def _file_config(accounts_dir: Path) -> dict[str, object]:
    """`<accounts_dir>/config.json` — TUI 가 정책을 저장하는 곳.

    사다리·마진은 원래 환경변수뿐이었다. 그래서 TUI 에서 바꿔도 남길 자리가 없었다.
    우선순위는 **환경변수 > 파일 > 기본값** 이다 — 환경변수를 이기게 두면 한 번의
    `CODEX_ROTATE_LADDER=…` 실험이 저장된 설정에 막혀 조용히 무시된다.

    깨진 파일은 무시한다. rotate 는 매 codex 호출에 실리므로 여기서 죽으면 안 된다.
    """
    path = accounts_dir / CONFIG_NAME
    try:
        loaded = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def file_config_error(accounts_dir: Path) -> str | None:
    """정책 파일이 **있는데 못 읽는** 경우의 사유. 멀쩡하거나 없으면 None.

    `_file_config` 의 침묵은 rotate 핫패스를 위한 것이다 — 매 codex 호출에 실리는 경로가
    파일 하나 때문에 시끄러우면 안 된다. 그런데 그 침묵이 대화형 표면까지 덮으면, TUI 로
    저장한 정책이 조용히 무시되는데 화면은 기본값을 자기 설정인 양 보여 준다. 사용자는
    저장이 안 된 줄 알고 같은 값을 다시 넣는다.

    그래서 판단은 그대로 조용히 두고, **묻는 쪽에만** 사유를 준다. 대가는 대화형 명령에서
    파일을 한 번 더 읽는 것뿐이다.
    """
    path = accounts_dir / CONFIG_NAME
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text())
    except OSError as exc:
        return f"{path}: {exc.strerror}"
    except json.JSONDecodeError as exc:
        return f"{path}: not valid JSON ({exc.msg}, line {exc.lineno})"
    if not isinstance(loaded, dict):
        return f"{path}: expected a JSON object, found {type(loaded).__name__}"
    return None


def parse_int(text: str) -> int:
    """숫자 노브 하나를 읽는다. 못 읽으면 `ConfigError`.

    환경변수·설정 파일·TUI 직접 입력이 **같은 함수**를 지난다. 화면이 자기만의 규칙을
    만들면 받아들이는 값이 갈려서, 터미널에서는 되는데 화면에서는 거부되는(또는 그 반대)
    상황이 생긴다.

    bash 는 `(( ))` 안에서 음수도 받았다. 여기서도 받아 두어야 `MARGIN=-5` 에서 우리만
    거부하는 일이 없다 — 값의 의미 검사는 여기가 아니라 정책의 몫이다.
    """
    # **부호는 하나만, 나머지는 ASCII 숫자만.** 예전에는 `lstrip("-")` 으로 부호를 **전부**
    # 벗겨서 `"--1"` 이 `"1"` 로 보였고, `str.isdigit()` 이 참인 `"²"` 도 통과했다. 둘 다
    # `int()` 에서 `ValueError` 가 되어 그대로 새어 나갔다.
    #
    # `int()` 에 통째로 맡기지 않는 것은 그러면 받는 값이 **넓어지기** 때문이다 —
    # `"1_0"`·`"٣"`·`"+5"` 가 전부 통과한다. 이 함수는 bash 의 `(( ))` 를 재현하는 자리라
    # 관용을 늘리면 그쪽과 갈린다.
    #
    # 그 유출이 CLI 오류 메시지 하나로 끝나지 않는다. 이 함수는 **환경변수도** 지나고,
    # `config.load()` 는 `rotate` 가 매 codex 호출에 부른다 — `CODEX_ROTATE_MARGIN=--1`
    # 하나로 모든 호출이 트레이스백을 낸다. `cache._seconds` 는 같은 함정을 알고
    # `isascii()` 를 걸어 두었는데 이쪽은 빠져 있었다.
    stripped = text.strip()
    body = stripped[1:] if stripped.startswith("-") else stripped
    if not (body.isascii() and body.isdigit()):
        raise ConfigError(f"not an integer: {text!r}")
    return int(stripped)


def parse_ladder(text: str) -> tuple[int, ...]:
    """`50,70,85,95` 형식. bash 는 숫자가 아닌 칸을 **조용히 건너뛴다**.

    그 관용을 그대로 옮긴다 — 한 칸이 깨졌다고 사다리를 통째로 거부하면, 자동 전환이
    매 codex 호출에서 죽는다. 다 걸러져 비면 호출부가 기본값으로 접는다.
    """
    return tuple(int(p) for p in (x.strip() for x in text.split(",")) if p.isdigit())


def _int_of(name: str, file_cfg: dict[str, object], default: int) -> int:
    v = _raw(name)
    if v is None:
        from_file = file_cfg.get(_file_key(name))
        if isinstance(from_file, bool):
            return default
        if isinstance(from_file, int):
            return from_file
        return default
    try:
        return parse_int(v)
    except ConfigError as exc:
        raise ConfigError(f"{name}={v!r} is not an integer") from exc


def _file_key(env_name: str) -> str:
    """`CODEX_ROTATE_MARGIN` → `margin`. 파일 안에서는 접두사가 잡음이다."""
    return env_name.removeprefix("CODEX_ROTATE_").removeprefix("CODEX_").lower()


def _flag_env(name: str) -> bool:
    """bash 의 `[[ -n "${X:-}" ]]` — 불리언이 아니라 **비어 있지 않음**이다.

    `CODEX_ROTATE_SKIP=0` 은 bash 에서 회전을 **끈다**. 일반적인 불리언 파서는 이를
    false 로 읽어 정반대로 동작한다.
    """
    return _raw(name) is not None


def _ladder_of(name: str, file_cfg: dict[str, object], default: tuple[int, ...]) -> tuple[int, ...]:
    """환경변수 또는 파일에서 사다리를 읽는다. 문자열 파싱은 `parse_ladder` 가 한다.

    **어느 경로든 다 걸러지면 기본값으로 접는다.** 파일 경로만 그렇게 하던 동안,
    `CODEX_ROTATE_LADDER=50;70` 같은 오타 하나가 빈 사다리를 만들어 임계 기반 전환을
    영구히 멈췄다 — `policy.decide` 가 `ladder is empty` 로 아무것도 하지 않는다.
    조용한 정지라 사용자는 도구가 도는 줄 안다. `parse_ladder` 의 docstring 이 이미
    "호출부가 기본값으로 접는다" 고 약속하고 있었다.

    `CODEX_ROTATE_MARGIN=abc` 가 `ConfigError` 로 죽는 것과 갈리는 이유는, 스칼라는
    물러설 기본값을 고를 수 없지만 사다리는 있기 때문이다. 일부만 걸러진 경우
    (`50,oops,90`)는 사용자의 뜻이 남은 것이므로 덮지 않는다.
    """
    v = _raw(name)
    if v is None:
        from_file = file_cfg.get(_file_key(name))
        if isinstance(from_file, list):
            rungs = tuple(
                x for x in from_file if isinstance(x, int) and not isinstance(x, bool) and x >= 0
            )
            return rungs if rungs else default
        return default
    return parse_ladder(v) or default


def save_policy(accounts_dir: Path, **values: object) -> Path:
    """정책을 파일에 병합해 저장한다. TUI 가 쓰는 유일한 쓰기 경로다.

    통째로 덮지 않고 병합하는 이유는, 이 파일이 나중에 다른 키를 갖게 되더라도 정책
    화면이 그것들을 지우지 않게 하기 위해서다.
    """
    accounts_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = accounts_dir / CONFIG_NAME
    current = _file_config(accounts_dir)
    current.update(values)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


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
    """busy 창. **초 단위 그대로** 쓴다.

    bash 가 남아 있던 동안에는 `ceil(window/60)*60` 으로 올려 실효 창을 맞췄다. bash 의
    `find -mmin` 폴백이 첫 `-newermt` 결과가 비면 무조건 실행되어 실효 창이 언제나 분
    단위였고, 맞추지 않으면 `window=100`·나이 110 초에서 두 구현의 판단이 갈려 차등
    테스트가 이 축에서 상시 불일치했기 때문이다. bash 가 없어졌으므로 맞출 대상도 없다.

    기본값 180 은 어느 쪽으로 계산해도 같다. 달라지는 것은 60 의 배수가 아닌 값을
    명시했을 때뿐이고, 그때는 이제 적은 그대로 동작한다.
    """

    skip: bool
    off_switch: Path


def _app_installed() -> bool:
    """공식 ChatGPT 데스크톱 앱이 깔려 있나.

    `wiring` 을 위에서 들여오지 않고 여기서 부른다 — `config` 는 거의 모든 모듈이 들여오는
    바닥이라, 여기에 의존을 하나 더 얹으면 순환이 생길 자리가 넓어진다.
    """
    from codex_swap.core import wiring

    return wiring.app_installed()


def load(environ: dict[str, str] | None = None) -> Settings:
    """현재 환경에서 설정을 읽는다.

    `~/.codex/.env` 는 읽지 **않는다**. wrapper 가 `set -a` 로 source 해서 변수가 이미
    export 된 채 도착하고, 여기서 다시 읽으면 오늘 `.env` 를 보지 못하는 두 경로(Claude
    훅 · `codex-swap` 직접 호출)에서 **새로** 유효해진다. 그건 이관이 아니라 동작
    변경이다 (설계문 §6.1.1).
    """
    # **인자는 덮어쓰기가 아니라 병합이다.** `load({})` 는 격리가 아니라 무동작이고,
    # `load({"X": "1"})` 은 그 값을 **프로세스에 영구히** 남긴다. 테스트가 이것을 격리로
    # 믿었다가 개발자 기기의 `~/.codex/accounts/config.json` 을 읽었다 — 사다리를 바꿔 둔
    # 기기에서 코드는 그대로인데 테스트가 빨개졌다.
    #
    # 시그니처를 그대로 두는 것은 호출부가 테스트 둘뿐이고, 진짜 격리는 `tests/conftest.py`
    # 의 autouse 픽스처가 하기 때문이다. 여기서 할 일은 그 함정을 적어 두는 것이다.
    if environ is not None:
        os.environ.update(environ)

    home = Path(os.environ.get("HOME") or Path.home())
    # **`CODEX_HOME` 을 여기서 보지 않는 것은 의도다.** codex 의 공식 환경변수이므로 그걸로
    # 홈을 옮긴 사용자를 따라가는 편이 자연스러워 보이지만, 그러면 `rotate` 의 재귀 방어가
    # 죽는다 — `add` 는 자식에게 `CODEX_HOME=<슬롯>` 을 주는데, `default_home` 이 그것을
    # 따라가면 `_codex_home_mismatch` 의 두 값이 같아져 가드가 통과한다(실측 확인).
    # 남는 방어는 `CODEX_ROTATE_SKIP` 한 겹뿐이고, 원래 두 겹으로 막던 자리다.
    #
    # 홈을 옮긴 사용자는 `CODEX_ACCOUNT_DEFAULT_HOME` 으로 이 도구에 따로 알려 준다.
    # 그 안내는 `cmd_adopt` 의 오류 메시지와 README 에 있다.
    # ── 공식 앱이 있으면 활성 자리를 비켜 준다 ──
    #
    # `~/.codex/auth.json` 의 주인은 ChatGPT 데스크톱 앱이다. 그 앱은 자기 codex 를
    # `CODEX_HOME=~/.codex` 로 띄워 두고 그 파일을 자기 세션으로 되돌려 놓는데, 우리는
    # 서드파티이므로 같은 파일을 놓고 다투지 않는다. 다투면 사용자에게는 "로그인이 자꾸
    # 풀린다" 로만 보이고 원인을 짐작할 길이 없다 — 실제로 한 기기에서 그랬다.
    #
    # **계정 저장소는 따라 옮기지 않는다.** 앱이 건드리는 것은 `auth.json` 하나이고
    # `accounts/` 는 앱이 모른다. 홈을 통째로 옮기면 등록해 둔 계정까지 잃어버리므로,
    # 활성 자리만 비켜 주고 슬롯은 원래 자리에 묶어 둔다.
    told = _raw("CODEX_ACCOUNT_DEFAULT_HOME")
    if told:
        # 사람이 정한 것이 이긴다. 그때는 계정도 그 홈을 따르던 기존 규칙 그대로다.
        default_home = Path(told)
        accounts_default = default_home / "accounts"
    elif _app_installed():
        default_home = home / ".codex-cli"
        accounts_default = home / ".codex/accounts"
    else:
        default_home = home / ".codex"
        accounts_default = default_home / "accounts"
    accounts_dir = Path(_raw("CODEX_ACCOUNTS_DIR") or accounts_default)
    state_root = Path(
        _raw("CODEX_ROTATE_STATE_ROOT") or home / ".claude/plugins/data/codex-openai-codex/state"
    )

    # **활성 홈은 여기서 만든다.** 자리를 비켜 주기로 한 순간 `~/.codex-cli` 는 codex 가 한
    # 번도 본 적 없는 경로가 된다 — 예전 기본값 `~/.codex` 는 codex 자신이 만들어 주므로
    # 아무도 이 일을 할 필요가 없었고, 그래서 만드는 코드가 어디에도 없다. 실기기에서 앱을
    # 깔고 분리된 뒤 두 가지가 같이 터졌다.
    #
    # `Error finding codex home: CODEX_HOME points to "…/.codex-cli", but that path does not
    # exist` — codex 는 없는 `CODEX_HOME` 을 거부하고 아예 뜨지 않는다(비어 있기만 한
    # 디렉토리는 괜찮다. "Not logged in" 으로 끝난다). `home`·`exec` 가 건네는 그 경로다.
    #
    # `Switch failed: [Errno 2] No such file or directory: '…/.codex-cli/auth.json.tmp.59753'`
    # — `store._install` 은 활성 `auth.json` **옆에** temp 를 쓰고 rename 한다. 그 자리가
    # 없으면 첫 전환이 열기부터 실패한다.
    #
    # 홈을 건네는 쪽·쓰는 쪽마다 `mkdir` 을 흩어 두면 새 진입점이 생길 때마다 같은 구멍이
    # 다시 열린다. 홈을 **정하는** 자리가 한 곳이니 만드는 자리도 여기 한 곳이다.
    # 실패는 삼킨다 — `load` 는 `rotate` 가 매 codex 호출에 부르는 자리라 여기서 예외가
    # 새면 그 트레이스백이 모든 호출에 실린다. 못 만들었으면 codex 가 위 메시지로 말한다.
    with contextlib.suppress(OSError):
        default_home.mkdir(mode=0o700, parents=True, exist_ok=True)

    file_cfg = _file_config(accounts_dir)

    return Settings(
        default_home=default_home,
        accounts_dir=accounts_dir,
        rotate_state_root=state_root,
        ladder=_ladder_of("CODEX_ROTATE_LADDER", file_cfg, DEFAULT_LADDER),
        margin=_int_of("CODEX_ROTATE_MARGIN", file_cfg, DEFAULT_MARGIN),
        cache_ttl=_int_of("CODEX_ROTATE_CACHE_TTL", file_cfg, DEFAULT_CACHE_TTL),
        check_interval=_int_of("CODEX_ROTATE_CHECK_INTERVAL", file_cfg, DEFAULT_CHECK_INTERVAL),
        cooldown=_int_of("CODEX_ROTATE_COOLDOWN", file_cfg, DEFAULT_COOLDOWN),
        busy_window=_int_of("CODEX_ROTATE_BUSY_WINDOW", file_cfg, DEFAULT_BUSY_WINDOW),
        skip=_flag_env("CODEX_ROTATE_SKIP"),
        off_switch=home / ".claude/.codex-rotate-off",
    )
