"""codex 가 **어느 홈으로 뜨는지**를 우리가 책임진다 — 양보는 우리가 한다.

`~/.codex` 의 주인은 공식 ChatGPT 데스크톱 앱이다. 그 앱은 자기 codex 를
`CODEX_HOME=~/.codex` 로 띄워 두고 그 안의 `auth.json` 을 **제자리에서 갱신하고, 자기 세션으로
되돌려 놓는다.** 우리는 서드파티이므로 같은 파일을 놓고 다투지 않는다.

다투면 두 가지가 동시에 망가진다. 사용자에게는 "로그인이 자꾸 풀린다" 로 보이고, 앱의 codex
로그에는 `401 Encountered invalidated oauth token` 이 수만 줄 쌓인다 — 같은 refresh token 을 두
곳이 쥐고 있다가 한쪽이 갱신하는 순간 다른 쪽 것이 무효가 되기 때문이다. 한 기기에서 실제로
확인했다.

**그래서 활성 자격증명 자리만 옮긴다.** 슬롯 저장소(`~/.codex/accounts`)는 앱이 모르므로
그대로 둔다. 그리고 codex 가 그 자리로 뜨도록 **배선**을 책임진다 — 우리가 놓는 wrapper 든,
사용자가 dotfiles 로 심어 둔 wrapper 든, 결국 codex 에게 `CODEX_HOME` 을 넘겨야 분리가 성립한다.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

ISOLATED_HOME = "$HOME/.codex-cli"
"""앱에게 `~/.codex` 를 넘겨주고 우리가 쓸 자리. 안내 문구에서 셸이 풀게 문자열로 둔다."""

APP_PATHS = (
    "/Applications/ChatGPT.app",
    "~/Applications/ChatGPT.app",
)
"""공식 앱이 설치되는 자리. 사용자별 설치도 있다."""


def app_installed(paths: tuple[str, ...] = APP_PATHS) -> bool:
    """ChatGPT 데스크톱 앱이 깔려 있나.

    **돌고 있는지가 아니라 깔려 있는지를 본다.** 앱은 껐다 켜지는 것이라, 마침 꺼져 있는
    순간에 판단하면 다음에 켰을 때 같은 다툼이 시작된다.
    """
    return any(Path(p).expanduser().exists() for p in paths)


def shell_of(path: str | None = None) -> str:
    """쓰고 있는 셸. 알 수 없으면 `bash` — 문법이 가장 널리 통한다."""
    name = Path(path or os.environ.get("SHELL") or "bash").name
    return name if name in {"bash", "zsh", "fish"} else "bash"


DEFAULT_HOME = Path("~/.codex")
"""분리하기 전의 자리. 공식 앱도 여기를 쓴다."""


ISOLATED_PATH = Path("~/.codex-cli")
"""분리했을 때 우리가 쓰는 자리."""


def _same(a: str | os.PathLike[str], b: str | os.PathLike[str]) -> bool:
    return os.path.realpath(os.path.expanduser(a)) == os.path.realpath(os.path.expanduser(b))


def apart_from_app(default_home: Path) -> bool:
    """앱이 깔려 있고, **실제로** 우리 홈이 앱의 홈과 다른가.

    앱이 있다는 것만으로 분리됐다고 말하면 안 된다. `CODEX_ACCOUNT_DEFAULT_HOME` 으로 앱의 홈을
    직접 가리킨 설정에서 `init` 이 "나눠 뒀다" 고 말한 적이 있다 — 실제로는 여전히 같은 파일을
    다투는 중이었다.
    """
    return app_installed() and not _same(default_home, DEFAULT_HOME)


def inherits_app_home(default_home: Path, environ: dict[str, str] | None = None) -> bool:
    """분리된 기기인데 **물려받은 `CODEX_HOME` 이 앱의 홈**을 가리키나.

    옛 셸 설정의 `export CODEX_HOME=~/.codex` 나 앱이 띄운 환경에서 흘러온 값이다. 그대로 두면
    codex 가 앱의 계정으로 뜨고, 우리 전환 가드는 "호출자가 다른 홈을 골랐다" 며 조용히 멈춘다.
    """
    table = os.environ if environ is None else environ
    raw = table.get("CODEX_HOME")
    return bool(raw) and apart_from_app(default_home) and _same(raw, DEFAULT_HOME)


def seed_source(default_home: Path) -> Path | None:
    """지금 홈은 비었는데 **다른 자리에 로그인이 남아 있으면** 그 경로. 아니면 None.

    홈이 바뀌는 순간에만 생기는 상태다. 자격증명은 따라오지 않으므로, 사용자에게는 잘
    쓰던 도구가 갑자기 로그아웃된 것처럼 보인다.

    **두 방향 다 본다.** 앱을 깔면 `~/.codex` → `~/.codex-cli` 로 옮겨 가지만, 앱을
    **지우면** 그 반대로 돌아온다. 뒤쪽을 빠뜨리기 쉬운데 사용자가 겪는 증상은 똑같고,
    오히려 더 당황스럽다 — 앱을 지웠을 뿐인데 codex 가 로그아웃되기 때문이다.

    **알려 주기만 한다. 복사하라고 하지 않는다.** 앱 쪽 `auth.json` 을 우리 자리로 베끼면 같은
    refresh token 을 앱과 우리가 나눠 쥐게 되고, 먼저 갱신하는 쪽이 다른 쪽을 로그아웃시킨다 —
    이 모듈이 막으려는 바로 그 다툼이다. 채우는 길은 슬롯에서의 전환이나 새 로그인뿐이다.
    """
    here = default_home.expanduser()
    if (here / "auth.json").is_file():
        return None  # 지금 자리에 이미 있다
    for other in (DEFAULT_HOME.expanduser(), ISOLATED_PATH.expanduser()):
        if other != here and (other / "auth.json").is_file():
            return other
    return None


WRAPPER = Path("~/.local/bin/codex")
"""우리가 놓는 wrapper 자리. **PATH 에 있기만 하면 셸이 무엇이든 통한다.**"""

MARKER = "# codex-swap wrapper"
"""우리가 놓은 것인지 알아보는 표시.

`discovery.is_wrapper` 도 이 표시를 본다 — 못 보면 upstream 이 사라진 기기에서 이 wrapper 를
진짜 codex 로 집어 `exec` 가 자기 자신을 끝없이 다시 띄운다. 두 곳이 갈라지지 않는지는 테스트가
묶는다.
"""

WRAPPER_BODY = f"""#!/bin/sh
{MARKER} — do not edit; regenerate with: codex-swap init
exec codex-swap exec "$@"
"""
"""**얇게 둔다.** 판단은 전부 `codex-swap exec` 안에 있다."""

LEGACY_ROTATOR = "codex-account"
"""codex-swap 이전의 bash 전환기. 우리와 같은 원장·락·스탬프를 쓴다."""

HOME_LINE = 'export CODEX_HOME="$(codex-swap home)"'
"""외부 wrapper 에 넣으라고 안내할 한 줄. **`rotate` 보다 앞에** 둔다."""


# ── 셸 스크립트를 "실행되는 줄" 로만 읽는다 ─────────────────────────────────────
#
# 문자열이 어딘가 들어 있는지로 판정하던 때가 있었다. 그러면 설명 주석 하나(`# CODEX_HOME …`),
# `unset CODEX_HOME`, `CODEX_HOME="$HOME/.codex"` 가 전부 "홈을 넘긴다" 로 읽혔고, 옛 전환기를
# 부르는 wrapper 에 `# codex-swap 으로 옮길 것` 이라는 주석만 붙어도 옛 전환기 판정이 사라졌다.
#
# 셸을 해석하는 것이 아니다. 조건문·함수·동적 source 까지 문자열로 증명할 수는 없다. 여기서
# 하는 것은 **흔한 두 모양만 인정하고 나머지는 인정하지 않는 것** — 해석 못 한 것을 "됐다" 로
# 접으면 전환이 codex 에 안 닿는 기기를 ready 라고 부르게 된다.

_INLINE_COMMENT = re.compile(r"(^|\s)#.*$")
_EXEC_DELEGATE = re.compile(r"\bcodex-swap\s+exec\b")
_HOME_CALL = re.compile(r'^(?:export\s+)?(\w+)=.*(?:\bcodex-swap|"?\$\{?\w+\}?"?)\s+home\b')
_SET_HOME = re.compile(r"^(?:export\s+)?CODEX_HOME=(.*)$")
_VAR_REF = re.compile(r"\$\{?(\w+)\}?")
_UNSET_HOME = re.compile(r"\bunset\s+(?:-v\s+)?CODEX_HOME\b")
_ROTATE_CALL = re.compile(r'(?:\bcodex-swap|"?\$\{?\w+\}?"?)\s+rotate\b')


def _code_lines(text: str) -> list[str]:
    """주석을 걷어낸 줄들. 줄 번호를 지키려고 빈 줄도 남긴다."""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        out.append("" if line.startswith("#") else _INLINE_COMMENT.sub("", line).strip())
    return out


def passes_home(text: str) -> bool:
    """이 스크립트가 codex 에게 **codex-swap 이 정한 홈**을 넘기는가.

    인정하는 모양은 둘이다.

    - `codex-swap exec` 로 통째로 넘긴다.
    - `CODEX_HOME` 을 `codex-swap home` 의 출력으로 정한다. 직접이든(`CODEX_HOME="$(codex-swap
      home)"`), 변수를 거치든(`h="$("$swap" home)"` → `export CODEX_HOME="$h"`). 그리고 그것이
      **`rotate` 보다 앞**이고, 뒤에서 `unset` 되지 않는다.

    순서를 보는 이유가 있다. 홈을 정하기 전에 `rotate` 를 부르면, 물려받은 `CODEX_HOME` 이 앱의
    홈일 때 전환 가드가 "다른 홈을 골랐다" 며 멈추고 그 뒤로 전환이 한 번도 안 일어난다.
    """
    lines = _code_lines(text)
    if any(_EXEC_DELEGATE.search(line) for line in lines):
        return True

    from_home: dict[str, int] = {}
    for i, line in enumerate(lines):
        found = _HOME_CALL.search(line)
        if found:
            from_home.setdefault(found.group(1), i)

    set_at: int | None = None
    for i, line in enumerate(lines):
        assigned = _SET_HOME.match(line)
        if not assigned:
            continue
        if from_home.get("CODEX_HOME") == i:
            set_at = i
            break
        ref = _VAR_REF.search(assigned.group(1))
        if ref and from_home.get(ref.group(1), i + 1) <= i:
            set_at = i
            break
    if set_at is None:
        return False
    if any(_UNSET_HOME.search(line) for line in lines[set_at + 1 :]):
        return False
    rotate_at = next((i for i, line in enumerate(lines) if _ROTATE_CALL.search(line)), None)
    return rotate_at is None or rotate_at > set_at


def calls_us(text: str) -> bool:
    """실행되는 줄에서 codex-swap 을 부르는가."""
    return any("codex-swap" in line for line in _code_lines(text))


def uses_legacy_rotator(text: str) -> bool:
    """실행되는 줄에서 **옛 bash 전환기**를 부르고, codex-swap 은 부르지 않는가."""
    lines = _code_lines(text)
    return any(LEGACY_ROTATOR in line for line in lines) and not calls_us(text)


def _head(path: Path) -> str:
    """스크립트 앞부분. 진짜 바이너리를 통째로 읽지 않으려고 8KB 만 본다."""
    try:
        with path.open("rb") as fh:
            raw = fh.read(8192)
    except OSError:
        return ""
    return raw.decode("utf-8", "replace") if raw[:2] == b"#!" else ""


def wrapper_here(path: Path | None = None) -> bool:
    """그 자리에 **우리가 놓은** wrapper 가 있나."""
    target = (path or WRAPPER).expanduser()
    return MARKER in _head(target)


def occupied_by_other(path: Path | None = None) -> bool:
    """그 자리에 **남의 것**이 있나. 덮으면 안 되는 상태다."""
    target = (path or WRAPPER).expanduser()
    return target.exists() and not wrapper_here(target)


OURS = "ours"
EXTERNAL = "external"
LEGACY = "legacy"
PROFILE = "profile"
FOREIGN = "foreign"
NONE = "none"


@dataclass(frozen=True)
class Wiring:
    """codex 가 지금 어떻게 뜨는가."""

    kind: str
    path: Path | None = None
    passes_home: bool = False

    def complete(self, isolate: bool) -> bool:
        """이 배선으로 전환이 **실제로 codex 에 닿는가.**

        앱과 나뉘어 있지 않으면(`isolate` 가 거짓) 전환만 되면 된다. 나뉘어 있으면 홈까지 넘겨야
        한다.
        """
        if self.kind == OURS:
            return True
        if self.kind in (EXTERNAL, PROFILE):
            return self.passes_home or not isolate
        return False


def inspect(shell: str) -> Wiring:
    """codex 가 **실제로 어떤 경로로 뜨는지** 본다.

    PATH 를 먼저 본다. 셸 프로필만 뒤지면 dotfiles 로 심어 둔 wrapper 를 못 보고, 멀쩡히
    돌아가는 기기에 "배선이 빠졌다" 고 말하게 된다 — 실제로 그렇게 오탐했다.
    """
    found = shutil.which("codex")
    if found:
        target = Path(found)
        head = _head(target)
        if MARKER in head:
            return Wiring(OURS, target, passes_home=True)
        if uses_legacy_rotator(head):
            return Wiring(LEGACY, target)
        if calls_us(head):
            return Wiring(EXTERNAL, target, passes_home=passes_home(head))

    for name in (profile_for(shell), "~/.bashrc", "~/.zshrc", "~/.profile"):
        path = Path(name).expanduser()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        code = "\n".join(_code_lines(text))
        if "codex-swap rotate" in code or "codex-swap exec" in code:
            return Wiring(PROFILE, path, passes_home=passes_home(text))

    own = WRAPPER.expanduser()
    if own.exists() and not wrapper_here(own):
        return Wiring(LEGACY if uses_legacy_rotator(_head(own)) else FOREIGN, own)
    return Wiring(NONE)


def already_wired(shell: str) -> Path | None:
    """전환이 걸려 있는 배선의 위치. 없으면 None. (홈까지 넘기는지는 `inspect` 가 가린다.)"""
    state = inspect(shell)
    return state.path if state.kind in (OURS, EXTERNAL, PROFILE) else None


def install_wrapper(path: Path | None = None) -> Path:
    """wrapper 를 놓는다. 이미 남의 것이 있으면 건드리지 않고 예외를 올린다."""
    target = (path or WRAPPER).expanduser()
    if occupied_by_other(target):
        raise FileExistsError(str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(WRAPPER_BODY, encoding="utf-8")
    target.chmod(0o755)
    return target


def on_path(path: Path | None = None) -> bool:
    """그 자리가 PATH 에 들어 있나. 놓았는데 PATH 에 없으면 아무 일도 일어나지 않는다."""
    target = (path or WRAPPER).expanduser()
    entries = [Path(p).expanduser() for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    return target.parent in entries


def profile_for(shell: str) -> str:
    """그 셸이 읽는 파일. 안내에만 쓴다 — **우리가 쓰지는 않는다.**"""
    return {
        "zsh": "~/.zshrc",
        "fish": "~/.config/fish/config.fish",
    }.get(shell, "~/.bashrc")
