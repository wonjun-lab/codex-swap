"""밝은 터미널에서도 읽히게 — **배경을 물어보고** 색을 고른다.

배경 자체는 예전부터 건드리지 않았다(`use_default_colors`). 문제는 전경이다. 기본 8 색의
노랑은 흰 바탕에서 거의 사라지고 시안도 옅어서, 밝은 테마를 쓰는 사람에게는 경고가 경고로
안 보인다. 어두운 바탕에 맞춘 색을 그대로 쓴 탓이다.

**추측하지 않고 묻는다.** 터미널에는 배경색을 되묻는 규약이 있다(OSC 11). 응답을 못 받으면
`COLORFGBG` 를 보고, 그것도 없으면 어두운 쪽으로 간다 — 어두운 배경이 더 흔하고, 밝은 색을
어두운 바탕에 쓰는 것이 그 반대보다 덜 위험하다.

여기 있는 것은 **판단뿐**이다. curses 를 만지지 않으므로 터미널 없이 시험할 수 있고, 실제
색 등록은 `tui` 가 한다.
"""

from __future__ import annotations

import os
import re

DARK = "dark"
LIGHT = "light"

ENV_VAR = "CODEX_SWAP_THEME"
"""`dark` · `light` · `auto`. **사람이 고른 것이 언제나 이긴다.**

자동 감지는 터미널이 정직하게 답할 때만 맞다. 원격 접속·멀티플렉서·응답하지 않는 터미널이
섞이면 틀릴 수 있고, 그때 사용자가 스스로 못 고치면 도구를 못 쓴다.
"""

PALETTES: dict[str, dict[str, int]] = {
    # 어두운 바탕 — 기본 8 색 그대로. 오래 쓰인 조합이고 어디서나 있다.
    DARK: {"ok": 2, "warn": 3, "danger": 1, "accent": 6},
    # 밝은 바탕 — 256 색의 **어두운** 쪽. 노랑은 흰 바탕에서 안 보이므로 갈색에 가까운
    # 호박색으로, 시안은 남색으로 내린다.
    LIGHT: {"ok": 28, "warn": 130, "danger": 124, "accent": 24},
}

FALLBACK_LIGHT: dict[str, int] = {"ok": 2, "warn": 5, "danger": 1, "accent": 4}
"""256 색이 없는 밝은 터미널. 노랑만은 못 쓴다 — 흰 바탕에서 글자가 사라진다.

대신 자홍을 쓴다. 상태 삼색의 한 칸을 다른 색상으로 바꾸는 것이지만, 안 보이는 경고보다
낫다.
"""


def palette(theme: str, colors: int = 256) -> dict[str, int]:
    """테마와 터미널의 색 수에 맞는 `tone → 색 번호`."""
    if theme != LIGHT:
        return dict(PALETTES[DARK])
    return dict(PALETTES[LIGHT]) if colors >= 256 else dict(FALLBACK_LIGHT)


def is_light(r: int, g: int, b: int) -> bool:
    """이 배경이 밝은가. 0-255 기준.

    사람 눈은 초록에 가장 민감하고 파랑에 둔하다 — 세 값을 그냥 평균 내면 남색 바탕이
    밝다고 나온다. 그래서 가중치를 준다(ITU-R BT.601).
    """
    return (0.299 * r + 0.587 * g + 0.114 * b) > 127.5


_OSC11 = re.compile(
    r"rgba?:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)",
)


def parse_osc11(reply: str) -> tuple[int, int, int] | None:
    """OSC 11 응답에서 RGB 를 꺼낸다. 못 읽으면 None.

    응답은 `rgb:` 뒤에 채널마다 1-4 자리 16 진수가 온다 — 터미널마다 자릿수가 다르다
    (`ffff/ffff/ffff` 도 있고 `ff/ff/ff` 도 있다). 자릿수로 최대값을 정해 0-255 로 맞춘다.
    """
    found = _OSC11.search(reply)
    if not found:
        return None
    out = []
    for part in found.groups():
        scale = (16 ** len(part)) - 1
        if scale <= 0:
            return None
        out.append(round(int(part, 16) * 255 / scale))
    return (out[0], out[1], out[2])


def from_colorfgbg(value: str | None) -> str | None:
    """`COLORFGBG` 로 테마를 읽는다. 알 수 없으면 None.

    `전경;배경` 이고 배경이 색 번호다. 0-6 과 8 이 어두운 쪽이라는 것이 관행이다.
    일부 터미널은 `15;default` 처럼 숫자가 아닌 것을 넣으므로 그때는 모른다고 답한다.
    """
    if not value:
        return None
    last = value.split(";")[-1].strip()
    if not last.isdigit():
        return None
    return DARK if int(last) in {0, 1, 2, 3, 4, 5, 6, 8} else LIGHT


def ask_background(timeout: float = 0.12) -> tuple[int, int, int] | None:
    """터미널에 배경색을 되묻는다(OSC 11). 응답이 없으면 None.

    **curses 를 켜기 전에** 불러야 한다. 화면이 올라온 뒤에 물으면 응답 문자열이 화면
    한복판에 찍히고, 그것을 지우는 것은 curses 의 그리기 순서와 싸우는 일이 된다.

    답하지 않는 터미널이 많다. 그래서 짧게 기다리고 조용히 포기한다 — 색을 정하려다
    프로그램이 멈추면 본말이 뒤집힌다. 응답을 안 하는 터미널에서 이 질의는 그냥 무시되고,
    우리가 보낸 바이트는 화면에 남지 않는다.

    `tty` 가 아니면 아예 묻지 않는다. 파이프 너머에는 답할 터미널이 없고, 보낸 바이트만
    출력에 섞인다.
    """
    import select
    import sys
    import termios
    import tty

    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return None
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
    except Exception:
        return None

    try:
        tty.setraw(fd)
        sys.stdout.write("\033]11;?\033\\")
        sys.stdout.flush()
        reply = ""
        # 한 번에 다 오지 않을 수 있다. 종결자를 볼 때까지 조금씩 모은다.
        while select.select([fd], [], [], timeout)[0]:
            chunk = os.read(fd, 64).decode("ascii", "replace")
            if not chunk:
                break
            reply += chunk
            if reply.endswith("\033\\") or reply.endswith("\a") or len(reply) > 128:
                break
    except Exception:
        return None
    finally:
        # **되돌리는 쪽이 더 중요하다.** raw 로 둔 채 나가면 터미널이 입력을 안 보여 준다.
        with __import__("contextlib").suppress(Exception):
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)

    return parse_osc11(reply)


def chosen(env: dict[str, str] | None = None) -> str | None:
    """사람이 골라 둔 테마. 안 골랐거나 `auto` 면 None."""
    table = os.environ if env is None else env
    want = (table.get(ENV_VAR) or "").strip().lower()
    return want if want in {DARK, LIGHT} else None


def detect(
    env: dict[str, str] | None = None, background: tuple[int, int, int] | None = None
) -> str:
    """쓸 테마. **고른 것 → 물어본 배경 → `COLORFGBG` → 어두움** 순으로 본다.

    마지막이 `DARK` 인 것은 그쪽이 더 흔해서이기도 하지만, 틀렸을 때의 손해가 작기
    때문이다. 어두운 바탕에 어두운 글자를 쓰면 아무것도 안 보이는 반면, 밝은 바탕에 밝은
    글자는 흐릿할지언정 읽힌다.
    """
    table = os.environ if env is None else env
    picked = chosen(table)
    if picked is not None:
        return picked
    if background is not None:
        return LIGHT if is_light(*background) else DARK
    return from_colorfgbg(table.get("COLORFGBG")) or DARK
