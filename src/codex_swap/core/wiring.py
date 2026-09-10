"""셸에 붙일 배선을 만들어 준다 — **양보는 우리가 한다.**

`~/.codex` 의 주인은 공식 ChatGPT 데스크톱 앱이다. 그 앱은 자기 codex 를
`CODEX_HOME=~/.codex` 로 띄워 두고 그 안의 `auth.json` 을 자기 세션으로 유지한다. 우리는
서드파티이므로 같은 파일을 놓고 다투지 않는다 — 다투면 사용자에게는 "로그인이 자꾸
풀린다" 로 보이고, 원인을 짐작할 방법이 없다.

**그래서 활성 자격증명 자리만 옮긴다.** 앱이 건드리는 것은 `~/.codex/auth.json` 하나이고,
슬롯 저장소(`~/.codex/accounts`)는 앱이 모른다. 홈을 통째로 옮기면 등록해 둔 계정까지 다시
만들어야 하지만, 활성 자리 하나만 비켜 주면 그럴 일이 없다.

이 배선을 사람이 손으로 적게 하지 않는다. 자동 전환을 쓰려면 어차피 셸 함수가 필요하고,
그 함수에 홈 한 줄을 더 넣는 것이 **사용자가 아무것도 몰라도 되는 유일한 지점**이다.
"""

from __future__ import annotations

import os
from pathlib import Path

ISOLATED_HOME = "$HOME/.codex-cli"
"""앱에게 `~/.codex` 를 넘겨주고 우리가 쓸 자리.

문자열로 두는 것은 셸이 풀게 하기 위해서다. 여기서 절대 경로로 펴 버리면 홈이 다른
기기에서 같은 프로필을 쓰는 사람의 배선이 깨진다.
"""

APP_PATHS = (
    "/Applications/ChatGPT.app",
    "~/Applications/ChatGPT.app",
)
"""공식 앱이 설치되는 자리. 사용자별 설치도 있다."""


def app_installed(paths: tuple[str, ...] = APP_PATHS) -> bool:
    """ChatGPT 데스크톱 앱이 깔려 있나.

    **돌고 있는지가 아니라 깔려 있는지를 본다.** 앱은 껐다 켜지는 것이라, 마침 꺼져 있는
    순간에 배선을 만들면 다음에 켰을 때 같은 다툼이 시작된다.
    """
    return any(Path(p).expanduser().exists() for p in paths)


def shell_of(path: str | None = None) -> str:
    """쓰고 있는 셸. 알 수 없으면 `bash` — 문법이 가장 널리 통한다."""
    name = Path(path or os.environ.get("SHELL") or "bash").name
    return name if name in {"bash", "zsh", "fish"} else "bash"


def snippet(shell: str, *, isolate: bool) -> str:
    """프로필에 넣을 배선. `eval` 로 먹이는 것을 전제로 한다.

    `rotate` 를 감싸는 규칙은 README 와 같은 이유로 여기서도 지킨다 — 실패해도 codex 는
    떠야 하고(`|| true`), stderr 는 삼키지 않는다(전환이 일어난 그 한 줄이 거기로 나온다).

    `CODEX_HOME` 은 **export 하지 않는다.** 그 변수는 codex 를 부르는 그 한 번에만 걸어야
    한다 — 셸 전체에 남기면 사용자가 여는 다른 도구까지 따라오고, 그중에는 앱이 띄운
    것도 있다.
    """
    home_prefix = f"CODEX_HOME={ISOLATED_HOME} " if isolate else ""
    if shell == "fish":
        lines = []
        if isolate:
            lines.append(f"set -gx CODEX_ACCOUNT_DEFAULT_HOME {ISOLATED_HOME}")
        lines += [
            "function codex",
            "    command codex-swap rotate >/dev/null; or true",
            (
                f"    env CODEX_HOME={ISOLATED_HOME} command codex $argv"
                if isolate
                else "    command codex $argv"
            ),
            "end",
        ]
        return "\n".join(lines) + "\n"

    lines = []
    if isolate:
        lines.append(f'export CODEX_ACCOUNT_DEFAULT_HOME="{ISOLATED_HOME}"')
    lines += [
        "codex() {",
        "  command codex-swap rotate >/dev/null || true",
        f'  {home_prefix}command codex "$@"',
        "}",
    ]
    return "\n".join(lines) + "\n"


def line_for(shell: str) -> str:
    """프로필에 적어 둘 **한 줄**. 이것만 넣으면 나머지는 매번 새로 만들어진다.

    배선을 통째로 붙여 넣게 하면 그 사본이 낡는다 — 규칙이 바뀌어도 사용자의 프로필에는
    옛 판이 그대로 남고, 그것을 고치라고 알릴 방법이 없다.
    """
    if shell == "fish":
        return "codex-swap shell-init | source"
    return 'eval "$(codex-swap shell-init)"'


DEFAULT_HOME = Path("~/.codex")
"""분리하기 전의 자리. 공식 앱도 여기를 쓴다."""


def seed_source(default_home: Path) -> Path | None:
    """지금 홈은 비었는데 **원래 자리에 로그인이 남아 있으면** 그 경로. 아니면 None.

    분리로 넘어가는 순간에만 생기는 상태다. 배선은 셸이 뜰 때마다 다시 판단하므로 앱을
    나중에 깔아도 다음 셸부터 홈이 바뀌는데, 자격증명은 따라오지 않는다. 사용자에게는
    잘 쓰던 도구가 갑자기 로그아웃된 것처럼 보인다.

    슬롯이 남아 있으면 전환 한 번으로 채워지지만(슬롯 저장소는 옮기지 않는다), 아직 계정을
    등록하지 않았다면 그마저 없다 — `adopt` 가 "로그인이 없다" 며 막히고, 사용자는 방금까지
    멀쩡히 쓰던 로그인이 어디로 갔는지 알 길이 없다.
    """
    here = default_home.expanduser()
    if (here / "auth.json").is_file():
        return None  # 지금 자리에 이미 있다
    original = DEFAULT_HOME.expanduser()
    if original == here:
        return None  # 분리하지 않은 설치다
    return original if (original / "auth.json").is_file() else None


def already_wired(shell: str) -> Path | None:
    """배선이 이미 프로필에 있으면 그 파일. 없으면 None.

    **두 번 넣지 않게 하려는 것**이다. `init` 은 사용자가 여러 번 부르는 명령이고, 그때마다
    같은 줄을 또 넣으라고 하면 프로필에 사본이 쌓인다. 손으로 적어 둔 옛 방식(`rotate` 를
    직접 감싼 함수)도 배선으로 친다 — 그것도 제 몫을 하고 있고, 우리가 시킨 적 없는 것을
    "빠졌다" 고 말하면 사용자는 자기가 뭘 잘못했나 찾게 된다.
    """
    for name in (profile_for(shell), "~/.bashrc", "~/.zshrc", "~/.profile"):
        path = Path(name).expanduser()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "codex-swap shell-init" in text or "codex-swap rotate" in text:
            return path
    return None


def profile_for(shell: str) -> str:
    """그 셸이 읽는 파일. 안내에만 쓴다 — **우리가 쓰지는 않는다.**"""
    return {
        "zsh": "~/.zshrc",
        "fish": "~/.config/fish/config.fish",
    }.get(shell, "~/.bashrc")
