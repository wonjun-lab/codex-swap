"""TUI 를 진짜 터미널에 띄우고 화면을 되읽는 하니스.

`render_screen` 은 순수 함수라 터미널 없이 검증된다. 그런데 이 프로젝트에서 값비쌌던
결함은 전부 **그 바깥**, 즉 `_loop`·`_paint`·`_prompt` 에 있었다. 넷을 세션 하나에서
밟았다.

| 결함 | 증상 | 단위 테스트가 잡았나 |
| --- | --- | --- |
| 자동 조회가 동기 | 화면이 5.3 초 굳고 `q` 가 안 먹음 | 아니오 |
| `_prompt` 가 논블로킹 | `master` 를 넣으면 `ster` 슬롯이 생김 | 아니오 |
| 조회 결과가 모드를 덮음 | 정책 화면이 튀어나가고 편집이 날아감 | 아니오 |
| 행 전체 bold | 같은 문자열인 바가 활성 행에서만 길어 보임 | 아니오 |

넷 다 `pragma: no cover` 구간이라 497 건이 전부 통과하는 채로 지나갔다. 여기가 그
구간의 유일한 방어선이다.

**격리가 이 파일의 절반이다.** 실제 계정·네트워크·설치본에 닿으면 테스트가 아니라 그
기기의 상태를 재는 것이 된다. HOME 을 tmp 로 돌리고, 프로브가 부를 바이너리는
`tests/fixtures/probe/fake_app_server.py` 를 가리키게 한다 — 프로브 테스트가 이미 쓰는
그 자식이라, 여기서도 **진짜 subprocess·진짜 NDJSON** 이 오간다.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import select
import struct
import sys
import termios
import time
from dataclasses import dataclass
from pathlib import Path

from codex_swap.tui import _width

FIXTURES = Path(__file__).parent / "fixtures" / "probe"

_CSI = re.compile(r"\x1b\[([0-9;?]*)([A-Za-z])")
_OTHER_ESC = re.compile(r"\x1b[()][B0]|\x1b[=>]|\x1b\][^\x07]*\x07")

_ZERO_WIDTH = frozenset("\x0e\x0f\x07\x00")
"""칸을 차지하지 않는 제어 문자. SO/SI 는 ncurses 가 속성 전환에 쓴다."""

DEFAULT_COLS = 140
DEFAULT_ROWS = 24


@dataclass(frozen=True)
class Screen:
    """되읽은 화면. `lines` 는 그리기가 끝난 뒤의 격자다."""

    lines: tuple[str, ...]
    attrs: tuple[tuple[frozenset[str], ...], ...]
    """셀별 SGR 코드 집합. 글자와 **같은 좌표**다.

    바이트 스트림에서 정규식으로 속성을 찾으면 안 된다 — curses 가 속성 구간을 어떻게
    쪼개는지에 의존하게 되고, 실제로 그 방식은 "행 전체 bold" 뮤테이션을 놓쳤다.
    무엇이 어떤 속성으로 그려졌는지는 격자로 물어야 한다.
    """

    raw: str
    exit_code: int

    def attrs_of(self, needle: str) -> frozenset[str]:
        """`needle` 이 그려진 셀들의 SGR 코드 합집합."""
        found: set[str] = set()
        for row, line in enumerate(self.lines):
            at = line.find(needle)
            while at >= 0:
                for col in range(at, at + len(needle)):
                    if col < len(self.attrs[row]):
                        found |= self.attrs[row][col]
                at = line.find(needle, at + 1)
        return frozenset(found)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def row(self, needle: str) -> str:
        """`needle` 을 담은 첫 줄. 없으면 명확히 실패한다."""
        for line in self.lines:
            if needle in line:
                return line
        raise AssertionError(f"{needle!r} 가 화면에 없다:\n{self.text}")

    def has_sgr(self, code: str) -> bool:
        """`\\x1b[…m` 시퀀스가 나왔나. 색·굵기 검증용이다."""
        return f"\x1b[{code}m" in self.raw


def render(
    data: str, cols: int, rows: int
) -> tuple[tuple[str, ...], tuple[tuple[frozenset[str], ...], ...]]:
    """커서 이동을 해석해 격자를 복원한다.

    curses 는 최적화 갱신 때 **글자 사이에 커서 이동을 끼워 넣는다.** 그래서 바이트
    스트림에서 escape 를 지우고 문자열을 찾으면, 화면에 붙어 보이는 글자들이 떨어져
    나와 못 찾는다 — 실제로 `e 직접 입력` 을 그렇게 놓쳤다. 위치를 따라가며 격자에
    놓아야 화면이 무엇을 보여 주는지 알 수 있다.

    완전한 VT 에뮬레이터가 아니다. curses 가 이 화면에서 실제로 쓰는 것만 다룬다 —
    커서 주소지정·상대 이동·지우기. 더 넣으면 하니스가 검증 대상보다 복잡해진다.

    `2J`(전체 지우기)가 커서를 옮기지 않는다는 것이 특히 중요하다. 옮기는 것으로 두면
    이어지는 부분 갱신이 엉뚱한 줄에 얹혀 화면이 통째로 뭉개진다.
    """
    grid = [[" "] * cols for _ in range(rows)]
    attr_grid: list[list[frozenset[str]]] = [[frozenset()] * cols for _ in range(rows)]
    sgr: frozenset[str] = frozenset()
    row = col = 0

    def clamp() -> None:
        nonlocal row, col
        row = max(0, min(row, rows - 1))
        col = max(0, min(col, cols - 1))

    i = 0
    while i < len(data):
        ch = data[i]
        if ch == "\x1b":
            m = _CSI.match(data, i)
            if m:
                args, cmd = m.group(1), m.group(2)
                i = m.end()
                if args.startswith("?"):
                    # 사설 모드(`?1049h` 대체 화면, `?25l` 커서 숨김 …). 격자에 영향이
                    # 없고 인자가 숫자가 아니라 파싱하면 죽는다.
                    continue
                nums = [int(x) if x else 0 for x in args.split(";")] if args else []
                first = nums[0] if nums else 0
                if cmd in "Hf":
                    row = (nums[0] - 1) if nums else 0
                    col = (nums[1] - 1) if len(nums) > 1 else 0
                    clamp()
                elif cmd == "A":
                    row -= max(first, 1)
                    clamp()
                elif cmd == "B":
                    row += max(first, 1)
                    clamp()
                elif cmd == "C":
                    col += max(first, 1)
                    clamp()
                elif cmd == "D":
                    col -= max(first, 1)
                    clamp()
                elif cmd == "J":
                    # 커서는 그대로 둔다. 옮기면 이어지는 갱신이 어긋난다.
                    if first == 0:
                        for k in range(col, cols):
                            grid[row][k] = " "
                        for r in range(row + 1, rows):
                            grid[r] = [" "] * cols
                    elif first == 1:
                        for r in range(row):
                            grid[r] = [" "] * cols
                        for k in range(col + 1):
                            grid[row][k] = " "
                    else:
                        grid = [[" "] * cols for _ in range(rows)]
                elif cmd == "d":
                    # VPA — 줄만 절대 지정한다. 칸은 그대로다.
                    row = (first - 1) if nums else 0
                    clamp()
                elif cmd == "G":
                    # CHA — 칸만 절대 지정한다.
                    col = (first - 1) if nums else 0
                    clamp()
                elif cmd == "X":
                    # ECH — 커서 자리부터 n 칸을 지운다. 커서는 움직이지 않는다.
                    for k in range(col, min(col + max(first, 1), cols)):
                        grid[row][k] = " "
                        attr_grid[row][k] = frozenset()
                elif cmd == "m":
                    # **왼쪽부터 차례로** 적용한다. `0;1` 은 "초기화한 뒤 굵게" 라,
                    # `0` 이 있다고 통째로 비우면 같은 시퀀스의 `1` 까지 잃는다 —
                    # 실제로 그래서 라벨 강조와 단축키 색이 안 보였다.
                    active = set(sgr)
                    for code in args.split(";") if args else ["0"]:
                        if code in ("", "0"):
                            active.clear()
                        else:
                            active.add(code)
                    sgr = frozenset(active)
                elif cmd == "K":
                    if first == 0:
                        for k in range(col, cols):
                            grid[row][k] = " "
                    elif first == 1:
                        for k in range(col + 1):
                            grid[row][k] = " "
                    else:
                        grid[row] = [" "] * cols
                continue
            m2 = _OTHER_ESC.match(data, i)
            i = m2.end() if m2 else i + 1
            continue
        if ch == "\n":
            row, col = min(row + 1, rows - 1), 0
            i += 1
            continue
        if ch == "\r":
            col = 0
            i += 1
            continue
        if ch == "\b":
            col = max(col - 1, 0)
            i += 1
            continue
        if ch in _ZERO_WIDTH:
            # ncurses 는 속성을 걸 때 SO/SI(문자셋 전환)를 끼워 넣는다. 인쇄 문자로 세면
            # 그 줄만 한 칸씩 밀려서, 하니스가 없는 정렬 결함을 보고한다.
            i += 1
            continue
        if 0 <= row < rows and 0 <= col < cols:
            grid[row][col] = ch
            attr_grid[row][col] = sgr
            # 한글은 두 칸을 먹는다. 뒤 칸을 비워 두지 않으면 다음 글자가 거기 얹혀
            # 화면이 한 칸씩 밀린다.
            if _width(ch) == 2 and col + 1 < cols:
                grid[row][col + 1] = ""
        col += _width(ch)
        if col >= cols:
            col, row = 0, min(row + 1, rows - 1)
        i += 1
    return (
        tuple("".join(r).rstrip() for r in grid),
        tuple(tuple(r) for r in attr_grid),
    )


class Session:
    """격리된 HOME 과 가짜 app-server 를 갖춘 TUI 한 판."""

    def __init__(self, home: Path, *, term: str = "xterm-256color") -> None:
        self.home = home
        self.term = term
        self.accounts = home / ".codex/accounts"
        self.accounts.mkdir(parents=True, exist_ok=True)
        (home / ".claude").mkdir(parents=True, exist_ok=True)
        self.scenario = home / "scenario.json"
        self.events = home / "events.ndjson"
        self.codex_bin = home / "fakecodex"
        self.codex_bin.write_text(
            f"#!/bin/sh\nexec {sys.executable!r} "
            f'{str(FIXTURES / "fake_app_server.py")!r} "$@"\n'.replace("'", '"')
        )
        self.codex_bin.chmod(0o700)
        self.delay(0.0)

    def delay(self, seconds: float) -> None:
        """프로브 응답을 늦춘다.

        "조회가 도는 **동안** 무엇이 일어나는가" 를 보는 테스트는 타이밍에 기대면 안 된다.
        자연 속도에 맡기면 빠른 기기에서는 조회가 먼저 끝나 검사 자체가 공허해진다.
        """
        messages = json.loads((FIXTURES / "ok.json").read_text())["messages"]
        self.scenario.write_text(json.dumps({"messages": messages, "delays": {"3": seconds}}))

    def slot(self, label: str, email: str) -> None:
        import base64

        path = self.accounts / label / "auth.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        claims = base64.urlsafe_b64encode(json.dumps({"email": email}).encode())
        path.write_text(json.dumps({"tokens": {"id_token": f"h.{claims.decode().rstrip('=')}.s"}}))
        os.chmod(path, 0o600)

    def activate(self, email: str) -> None:
        import base64

        path = self.home / ".codex/auth.json"
        claims = base64.urlsafe_b64encode(json.dumps({"email": email}).encode())
        path.write_text(json.dumps({"tokens": {"id_token": f"h.{claims.decode().rstrip('=')}.s"}}))
        os.chmod(path, 0o600)

    def cache(
        self,
        label: str,
        percent: int,
        *,
        credits: int | None = None,
        resets_in: float | None = None,
    ) -> None:
        """사용량을 미리 심는다. 이걸 안 하면 화면이 열자마자 프로브를 돌린다.

        `resets_in` 은 지금부터 몇 초 뒤에 풀리는지다. 절대 epoch 을 받으면 테스트가
        "몇 시간 뒤" 라는 **표시**를 고정하지 못한다 — 그 문구가 열 폭에 걸리는 자리다.
        """
        path = self.accounts / ".usage-cache.json"
        doc = json.loads(path.read_text()) if path.exists() else {}
        doc[label] = {
            "usedPercent": percent,
            "resetsAt": None if resets_in is None else int(time.time() + resets_in),
            "resetCredits": credits,
            "ts": int(time.time()),
        }
        path.write_text(json.dumps(doc))
        os.chmod(path, 0o600)

    def env(self) -> dict[str, str]:
        return {
            **os.environ,
            "TERM": self.term,
            "HOME": str(self.home),
            "CODEX_ACCOUNT_DEFAULT_HOME": str(self.home / ".codex"),
            "CODEX_ACCOUNTS_DIR": str(self.accounts),
            "CODEX_ROTATE_STATE_ROOT": str(self.home / "state"),
            # 프로브가 부를 바이너리. 실제 codex 를 찾아 나서면 격리가 깨진다.
            "CODEX_ACCOUNT_BIN": str(self.codex_bin),
            "CODEX_REAL_BIN": str(self.codex_bin),
            "PROBE_SCENARIO": str(self.scenario),
            "PROBE_EVENTS": str(self.events),
            # 상속된 설정이 화면의 전제를 조용히 바꾸지 않게 한다.
            "CODEX_ROTATE_SKIP": "",
            "COLUMNS": str(DEFAULT_COLS),
            "LINES": str(DEFAULT_ROWS),
        }

    def run(
        self,
        keys: list[bytes],
        *,
        cols: int = DEFAULT_COLS,
        rows: int = DEFAULT_ROWS,
        settle: float = 0.6,
        total: float = 30.0,
        wait_for: str | None = None,
    ) -> Screen:
        """키를 차례로 넣고 화면을 되읽는다.

        키는 **화면이 조용해진 뒤**에 보낸다. 고정 지연으로 보내면 느린 러너에서
        입력이 그리기보다 앞서 도착해, 실패가 타이밍 문제인지 결함인지 갈리지 않는다.

        `wait_for` 는 키를 다 보낸 뒤 **그 문구가 화면에 뜰 때까지** 더 읽는다. 배경
        조회처럼 스스로 끝나는 일을 기다릴 때 쓴다 — 조용해졌다고 끊으면 아직 도는
        중에 캡처해서, 검사가 아무것도 안 본 채 통과한다.
        """
        import pty

        pid, fd = pty.fork()
        if pid == 0:  # pragma: no cover - 자식
            os.environ.clear()
            os.environ.update(self.env())
            os.execv(
                sys.executable, [sys.executable, "-c", "from codex_swap.cli import main; main()"]
            )
        import fcntl

        with contextlib.suppress(OSError):  # pragma: no cover - 플랫폼 차이
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        buf = bytearray()
        start = time.monotonic()
        quiet = None
        index = 0
        while time.monotonic() - start < total:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                quiet = time.monotonic()
                continue
            if quiet is None:
                continue
            idle = time.monotonic() - quiet
            if index < len(keys) and idle > settle:
                os.write(fd, keys[index])
                index += 1
                quiet = time.monotonic()
            elif index >= len(keys) and idle > settle:
                if wait_for is None:
                    break
                lines, _ = render(bytes(buf).decode("utf-8", "replace"), cols, rows)
                if any(wait_for in line for line in lines):
                    break
        with contextlib.suppress(OSError):
            os.close(fd)
        _, status = os.waitpid(pid, 0)
        raw = buf.decode("utf-8", "replace")
        lines, attrs = render(raw, cols, rows)
        return Screen(lines, attrs, raw, os.waitstatus_to_exitcode(status))
