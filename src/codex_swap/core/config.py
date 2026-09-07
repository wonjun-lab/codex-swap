"""환경변수 → Settings.

bash 의 파라미터 확장 의미를 한 곳에서 재현한다. 흩어 두면 호출부마다 미묘하게
달라지는데, 그 차이가 조용한 divergence 의 가장 흔한 원인이다 (설계문 §6.1).
"""

from __future__ import annotations

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
    stripped = text.strip()
    if not (stripped.lstrip("-").isdigit() and stripped.lstrip("-") != ""):
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
