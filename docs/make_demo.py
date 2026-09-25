"""README 의 데모 GIF(`docs/demo.gif`)를 만든다.

    uv run --with pillow python docs/make_demo.py

**화면을 손으로 그리지 않는다.** 실제 `tui.render_screen` 이 내는 줄과 속성을 그대로 그림으로
옮긴다. 그래서 화면이 바뀌면 이것을 다시 돌리기만 하면 되고, README 의 그림이 실제 화면과
갈라지지 않는다.

계정은 전부 가짜다(`*.name@gmail.com`). 실제 HOME 도 읽지 않는다 — 설정은 임시 HOME 에서
기본값으로 만든다. 공개 저장소에 들어가는 그림이라 실제 계정이 섞일 틈을 두지 않는다.

글꼴은 DejaVu Sans Mono 를 찾는다. 없으면 `DEMO_FONT`·`DEMO_FONT_BOLD` 로 지정한다.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ["HOME"] = tempfile.mkdtemp()
for key in [k for k in os.environ if k.startswith("CODEX_")]:
    del os.environ[key]

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from codex_swap import tui  # noqa: E402
from codex_swap.core import config  # noqa: E402

COLS, ROWS = 110, 26
OUT = Path(__file__).with_name("demo.gif")

_FONT_DIRS = ("/usr/share/fonts/truetype/dejavu", "/Library/Fonts", "/System/Library/Fonts")


def _font(env: str, name: str) -> str:
    if os.environ.get(env):
        return os.environ[env]
    for base in _FONT_DIRS:
        path = Path(base) / name
        if path.exists():
            return str(path)
    sys.exit(f"font {name} not found — set {env}")


SIZE = 15
REGULAR = ImageFont.truetype(_font("DEMO_FONT", "DejaVuSansMono.ttf"), SIZE)
BOLD = ImageFont.truetype(_font("DEMO_FONT_BOLD", "DejaVuSansMono-Bold.ttf"), SIZE)
CW = round(REGULAR.getlength("M"))
CH = SIZE + 5
PAD = 14

BG = (24, 26, 30)
FG = {
    "plain": (212, 214, 218),
    "dim": (118, 122, 130),
    "ok": (120, 184, 100),
    "warn": (229, 192, 123),
    "danger": (240, 96, 96),
    "accent": (97, 175, 239),
}
BRIGHT = (250, 250, 250)

ACCOUNTS = (
    tui.Row(
        "work", "account.name@gmail.com", "58%", "09-26 03:10 (in 5h)", True, percent=58, credits=1
    ),
    tui.Row("personal", "other.name@gmail.com", "81%", "09-26 08:40 (in 11h)", False, percent=81),
    tui.Row(
        "team", "third.name@gmail.com", "~24%", "09-27 01:05 (in 1d)", False, stale=True, percent=24
    ),
)


def _view(cursor: int, active: str = "work", **kw) -> tui.View:
    rows = tuple(tui.replace(row, active=row.label == active) for row in ACCOUNTS)
    return tui.View(rows=rows, cursor=cursor, settings=config.load(), current_rung=70, **kw)


def _draw(lines, caption: str) -> Image.Image:
    height = ROWS * CH + PAD * 2 + CH + 10
    img = Image.new("RGB", (COLS * CW + PAD * 2, height), BG)
    d = ImageDraw.Draw(img)
    for row, (text, style) in enumerate(lines[:ROWS]):
        x, y = PAD, PAD + row * CH
        for chunk, span in tui.segments(text, style.spans, COLS):
            st = span or style
            color = FG.get(st.tone, FG["plain"])
            if st.bold and st.tone == "plain":
                color = BRIGHT
            font = BOLD if st.bold else REGULAR
            for ch in chunk:
                w = CW * tui._width(ch)
                if st.reverse:
                    d.rectangle([x, y, x + w - 1, y + CH - 1], fill=FG["plain"])
                    d.text((x, y + 2), ch, font=font, fill=BG)
                else:
                    d.text((x, y + 2), ch, font=font, fill=color)
                x += w
    d.line([(0, height - CH - 16), (img.width, height - CH - 16)], fill=(48, 52, 58))
    d.text((PAD, height - CH - 8), caption, font=REGULAR, fill=FG["warn"])
    return img


def main() -> None:
    n = len(ACCOUNTS)
    scenes: list[tuple[tui.View, str, int]] = [
        (_view(0), "codex-swap — every account, its usage and when it renews", 2600),
        (_view(1), "↓  move to an account", 900),
        (_view(1, "personal", message="Switched to personal"), "enter  switch to it", 1800),
        (_view(n, "personal"), "↓  past the accounts into the menu", 1100),
        (_view(n + 1, "personal"), "↓  each entry opens with the bold letter in its name", 1600),
        (tui.open_help(_view(n + 1, "personal")), "?  every key, on one screen", 2600),
    ]
    frames = [_draw(tui.render_screen(v, width=COLS, height=ROWS), cap) for v, cap, _ in scenes]
    durations = [ms for _, _, ms in scenes]
    frames = [f.quantize(colors=64, method=Image.Quantize.MEDIANCUT) for f in frames]
    frames[0].save(
        OUT, save_all=True, append_images=frames[1:], duration=durations, loop=0, optimize=True
    )
    print(f"{OUT} ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
