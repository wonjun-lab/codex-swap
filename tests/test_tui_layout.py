"""화면의 **왼쪽 기준선**과 폭 한계.

정렬은 눈으로는 결함으로 안 보이고 어수선함으로만 느껴진다. 그래서 조용히 어긋나고,
어긋난 채로 오래 간다 — 꼬리말만 두 칸 들여쓴 채로 계속 돌고 있었고 어떤 테스트도 물지
않았다. 표·메뉴·축은 3 열에서 시작하는데 조작법·경고·메시지만 2 열이라 화면에 왼쪽 끝이
두 개였다.

폭도 같다. 넘치는 줄은 그리기 단계가 **말없이** 잘라서, 하필 실패를 알리는 유일한 줄의
뒤쪽이 사라진다.

여기서 재는 것은 문구가 아니라 그 두 가지다.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import pytest

from codex_swap import tui
from codex_swap.core import config

_MARKERS = set("   >*")
"""0~2 열에 나타나도 되는 글자. 커서(`>`)와 활성 표시(`*`) 뿐이다."""


def _auth(path: Path, email: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    claims = base64.urlsafe_b64encode(json.dumps({"email": email}).encode())
    path.write_text(json.dumps({"tokens": {"id_token": f"h.{claims.decode().rstrip('=')}.s"}}))


@pytest.fixture
def view(_isolated_home: Path) -> tui.View:
    s = config.load()
    _auth(s.accounts_dir / "master/auth.json", "a@example.com")
    _auth(s.accounts_dir / "shared/auth.json", "b@example.com")
    _auth(s.default_home / "auth.json", "b@example.com")
    now = int(time.time())
    (s.accounts_dir / ".usage-cache.json").write_text(
        json.dumps(
            {
                "master": {
                    "email": "a@example.com",
                    "usedPercent": 47,
                    "resetsAt": now + 82800,
                    "resetCredits": 0,
                    "reached": False,
                    "ts": now - 120,
                },
                "shared": {
                    "email": "b@example.com",
                    "usedPercent": 77,
                    "resetsAt": now + 507600,
                    "resetCredits": 2,
                    "reached": False,
                    "ts": now - 9000,
                },
            }
        )
    )
    return tui.build_view(s)


def _screens(view: tui.View) -> dict[str, tui.View]:
    """한 화면만 재면 다른 화면이 어긋난 채 남는다. 실제로 두 화면이 따로 어긋나 있었다."""
    long_message = "Switched to master (someone.with.a.long.address@example.com)"
    return {
        "accounts": view,
        "accounts+message": tui.replace(view, message=long_message),
        "accounts+menu-cursor": tui.replace(view, cursor=len(view.rows)),
        "accounts+empty": tui.replace(view, rows=()),
        "accounts+unregistered": tui.replace(view, active_registered=False),
        "policy": tui.replace(view, mode="policy"),
        "policy+message": tui.replace(view, mode="policy", message=long_message),
    }


@pytest.mark.parametrize("width", [40, 52, 60, 80, 100, 120])
def test_nothing_but_the_cursor_lives_left_of_the_text_column(view: tui.View, width: int) -> None:
    """제목 말고는 전부 3 열에서 시작한다.

    커서와 활성 표시만 그 왼쪽에 설 수 있다. 다른 글자가 거기 있으면 그 줄은 나머지
    화면과 다른 왼쪽 끝을 갖는다.
    """
    for name, screen in _screens(view).items():
        lines = tui.render_lines(screen, width=width)
        for i, line in enumerate(lines):
            if i == 0 or not line.strip():
                continue  # 0 행은 제목이다 — 유일하게 0 열에서 시작한다.
            assert set(line[:3]) <= _MARKERS, f"{name} @ {width} 행 {i}: {line!r}"


@pytest.mark.parametrize("width", [40, 52, 60, 80, 100, 120])
def test_no_line_runs_past_the_terminal(view: tui.View, width: int) -> None:
    for name, screen in _screens(view).items():
        for line in tui.render_lines(screen, width=width):
            assert tui._width(line) <= width, f"{name} @ {width}: {line!r}"


def test_the_footer_starts_exactly_at_the_text_column() -> None:
    """위 검사는 "3 열보다 왼쪽에 없다" 만 본다. 4 열로 밀려도 통과한다."""
    assert tui._note("x", 80).index("x") == 3
    keys, _ = tui.keys_line(tui.ACCOUNT_KEYS, width=200)
    assert keys.index("enter") == 3
    assert tui._help_line("Auto switch: on", width=80).index("A") == 3


def test_rows_do_not_trail_padding(view: tui.View) -> None:
    """행 끝의 채움은 눈에 안 보이지만 행에 걸린 색이 거기까지 칠한다."""
    for width in (52, 80, 100, 120):
        for line in tui.render_lines(view, width=width):
            assert line == line.rstrip(), f"@ {width}: {line!r}"
