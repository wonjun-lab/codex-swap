"""bash ↔ Python 정책 차등 테스트 (설계문 §7.3).

이관 기간 동안 두 구현이 **같은 입력에서 같은 결정**을 내는지 본다. 이것이 bash 를
지울 근거다 — 그 전까지는 "같아 보인다" 와 "같다" 가 구별되지 않는다.

런타임 병행은 성립하지 않는다(§7.2). wrapper 는 한쪽만 부르고, `rotate` 의 exit code 는
throttle·cooldown·busy·네트워크 실패·파싱 실패를 전부 1 로 접어서 두 구현이 똑같이 1 을
돌려줘도 동치인지 한쪽이 죽은 건지 알 수 없다. 그래서 **스냅샷**을 견준다: 상태를 한 번
만들어 양쪽에 같은 것을 먹이고, 나온 결정을 비교한다. 실제 계정은 어느 경로로도 닿지
않는다.

비교하는 것은 **(전환하는가, 어디로)** 다. 이유 문자열은 서식이 달라도 무방하고, 실제로
Python 쪽이 더 자세하다.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

from codex_swap.core import config
from codex_swap.core.policy import Snapshot, decide
from codex_swap.core.types import ProbeResult, Switched, Usage

BASH_LIB = Path.home() / "dotfiles/codex/lib/codex-account.sh"
BASH_PATH_LIB = Path.home() / "dotfiles/codex/lib/codex-path.sh"

pytestmark = pytest.mark.skipif(
    not (BASH_LIB.is_file() and BASH_PATH_LIB.is_file() and shutil.which("node")),
    reason="bash 구현(dotfiles) 또는 node 가 없다 — CI 와 이관 완료 후에는 건너뛴다",
)

FAKE_PROBE = """\
import fs from "node:fs";
const i = process.argv.indexOf("--home");
const home = i >= 0 ? process.argv[i + 1] : "";
if (fs.existsSync(`${home}/.fakeauthfail`)) { process.exit(3); }
let pct = null;
try { pct = Number(fs.readFileSync(`${home}/.fakepct`, "utf8").trim()); } catch { }
if (pct === null || Number.isNaN(pct)) { process.exit(1); }
let reached = null;
try { reached = fs.readFileSync(`${home}/.fakereached`, "utf8").trim() || null; } catch { }
process.stdout.write(JSON.stringify({
  email: "probe@example.com", planType: "pro", usedPercent: pct,
  primaryPercent: pct, secondaryPercent: null, resetsAt: 1, reached,
}) + "\\n");
"""


@dataclass(frozen=True)
class Case:
    name: str
    active: int | str
    """활성 사용량. 'auth' 는 인증 실패, 'unknown' 은 일반 프로브 실패."""
    other: int | str
    reached: bool = False
    cooldown: bool = False
    busy: bool = False
    ladder: str = "50,70,85,95"
    margin: int = 5


# 축을 하나씩 흔든다. 사다리·마진은 경계 근처를, 소진·인증실패는 절제를 건너뛰는지를,
# 쿨다운·busy 는 그 절제가 평상시엔 실제로 걸리는지를 본다.
CASES = [
    Case("사다리 첫 칸 도달", 55, 0),
    Case("첫 칸 미만", 45, 0),
    Case("둘 다 첫 칸 위 — 관문이 올라간다", 55, 50),
    Case("올라간 관문 도달", 75, 50),
    Case("사다리 끝", 99, 96),
    Case("마진 미달", 51, 48),
    Case("마진 충족", 55, 48),
    Case("마진 경계 정확히", 53, 48),
    Case("쿨다운이 막는다", 75, 10, cooldown=True),
    Case("busy 가 막는다", 75, 10, busy=True),
    Case("소진 — 쿨다운을 무시한다", 95, 50, reached=True, cooldown=True),
    Case("소진 — busy 를 무시한다", 95, 50, reached=True, busy=True),
    Case("소진 — 사다리 끝이어도 간다", 99, 96, reached=True),
    Case("소진 — 더 쓴 쪽으로는 안 간다", 95, 97, reached=True),
    Case("인증실패 — 95% 로도 간다", "auth", 95),
    Case("인증실패 — 쿨다운 무시", "auth", 10, cooldown=True),
    Case("인증실패 — busy 무시", "auth", 10, busy=True),
    Case("인증실패 — 후보도 죽었으면 안 간다", "auth", "auth"),
    Case("일반 프로브 실패는 인증실패가 아니다", "unknown", 0),
    Case("후보 프로브 실패", 95, "unknown"),
    Case("사다리 한 칸만", 75, 10, ladder="70"),
    Case("마진 0", 51, 50, margin=0),
]


def _auth(path: Path, email: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    path.write_text(json.dumps({"tokens": {"id_token": f"h.{payload}.s"}}))
    os.chmod(path, 0o600)


def _seed(root: Path, case: Case) -> Path:
    """양쪽이 함께 읽을 상태를 만든다. 이게 '스냅샷' 이다."""
    home = root / "home"
    acc = home / ".codex/accounts"
    acc.mkdir(parents=True)
    (home / ".claude").mkdir(parents=True)

    _auth(home / ".codex/auth.json", "a@example.com")
    _auth(acc / "a/auth.json", "a@example.com")
    _auth(acc / "b/auth.json", "b@example.com")

    def mark(where: Path, value: int | str, reached: bool) -> None:
        if value == "auth":
            (where / ".fakeauthfail").write_text("1")
        elif value == "unknown":
            pass  # .fakepct 가 없으면 프로브가 1 로 끝난다
        else:
            (where / ".fakepct").write_text(str(value))
        if reached:
            (where / ".fakereached").write_text("primary")

    # 활성의 프로브 홈은 슬롯 사본이 아니라 기본 홈이다 (계약 4).
    mark(home / ".codex", case.active, case.reached)
    mark(acc / "b", case.other, False)

    if case.cooldown:
        (acc / ".last-rotate").touch()
    if case.busy:
        jobs = root / "state/jobs"
        jobs.mkdir(parents=True)
        (jobs / "x.log").touch()
    return home


def _bash_decision(root: Path, home: Path, case: Case) -> tuple[bool, str | None]:
    """bash `codex_account_rotate --dry-run` 을 sourced 로 부른다."""
    lib = root / "fakelib"
    lib.mkdir()
    (lib / "codex-rate-limits.mjs").write_text(FAKE_PROBE)

    script = textwrap.dedent(f"""
        set -uo pipefail
        source '{BASH_PATH_LIB}'
        source '{BASH_LIB}'
        codex_account_rotate --dry-run
    """)
    env = {
        **os.environ,
        "HOME": str(home),
        "CODEX_ACCOUNT_DEFAULT_HOME": str(home / ".codex"),
        "CODEX_ACCOUNTS_DIR": str(home / ".codex/accounts"),
        "CODEX_ROTATE_STATE_ROOT": str(root / "state"),
        "CODEX_ACCOUNT_LIB_DIR": str(lib),
        "CODEX_ACCOUNT_BIN": "/bin/true",
        "CODEX_ROTATE_CHECK_INTERVAL": "0",
        "CODEX_ROTATE_LADDER": case.ladder,
        "CODEX_ROTATE_MARGIN": str(case.margin),
    }
    env.pop("CODEX_ROTATE_SKIP", None)
    env.pop("CODEX_HOME", None)

    out = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60
    )
    if out.returncode != 0:
        return (False, None)
    # "would switch: a(55%) -> b(0%) [reason]"
    text = out.stdout.strip()
    if "->" not in text:
        return (False, None)
    target = text.split("->", 1)[1].strip().split("(")[0].strip()
    return (True, target)


def _python_decision(home: Path, root: Path, case: Case) -> tuple[bool, str | None]:
    def probe_of(value: int | str, reached: bool) -> ProbeResult:
        if value == "auth":
            return ProbeResult.auth_failed()
        if value == "unknown":
            return ProbeResult.unknown()
        return ProbeResult.of(Usage(used_percent=int(value), reached=reached))

    os.environ.update(
        {
            "HOME": str(home),
            "CODEX_ACCOUNT_DEFAULT_HOME": str(home / ".codex"),
            "CODEX_ACCOUNTS_DIR": str(home / ".codex/accounts"),
            "CODEX_ROTATE_STATE_ROOT": str(root / "state"),
            "CODEX_ROTATE_LADDER": case.ladder,
            "CODEX_ROTATE_MARGIN": str(case.margin),
        }
    )
    for k in ("CODEX_ROTATE_SKIP", "CODEX_HOME"):
        os.environ.pop(k, None)

    settings = config.load()
    snap = Snapshot(
        settings=settings,
        active_present=True,
        active_label="a",
        active_probe=probe_of(case.active, case.reached),
        candidates={"b": probe_of(case.other, False)},
        cooldown_active=case.cooldown,
        busy=case.busy,
    )
    d = decide(snap)
    return (isinstance(d, Switched), d.to_label if isinstance(d, Switched) else None)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_both_implementations_agree(case: Case, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(os, "environ", dict(os.environ))
    home = _seed(tmp_path, case)
    bash = _bash_decision(tmp_path, home, case)
    python = _python_decision(home, tmp_path, case)
    assert bash == python, f"{case.name}: bash={bash} python={python}"
