"""upstream codex 를 새 판으로 — **어떻게 깔렸는지는 codex 가 안다.**

`codex update` 는 자기 설치 방식(npm · bun · pnpm · brew cask · standalone)을 스스로 가려
맞는 명령을 돈다. 그런데 wrapper 를 PATH 앞에 둔 기기에서는 그 명령에 닿기가 번거롭다:

- `codex update` 를 wrapper 로 치면 계정 정책부터 돈다. 갱신에는 계정이 필요 없다.
- T3 Code 같은 앱은 `codex` 의 실제 경로로 설치 방식을 가리는데, 그 경로가 wrapper 라
  "직접 갱신하라" 로 접는다. 그래서 사용자가 매번 `npm i -g @openai/codex` 를 손으로 쳤다.

여기서 하는 일은 **upstream 을 찾아 판을 견주고, 새 판이 있을 때만 그 `update` 를 부르는
것**이다. 설치 방식을 우리가 다시 가리지 않는다 — codex 가 이미 하는 일을 두 벌로 두면
새 설치 방식이 생길 때마다 한쪽이 틀린다.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

PACKAGE = "@openai/codex"

_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)(-[0-9A-Za-z.-]+)?")

Runner = Callable[..., subprocess.CompletedProcess]


def parse_version(text: str | None) -> str | None:
    """`codex-cli 0.158.0` 같은 출력에서 판 번호만. 못 읽으면 None."""
    match = _VERSION.search(text or "")
    return match.group(0) if match else None


def _key(version: str) -> tuple[int, int, int, int]:
    match = _VERSION.fullmatch(version)
    if match is None:
        raise ValueError(version)
    major, minor, patch, pre = match.groups()
    # 같은 번호면 정식판이 사전판보다 새것이다 (`0.158.0-alpha.1` < `0.158.0`).
    return int(major), int(minor), int(patch), 0 if pre else 1


def is_newer(latest: str, current: str) -> bool:
    """`latest` 가 `current` 보다 새것인가.

    **거꾸로인 경우를 갱신으로 치지 않는다.** 사전판을 일부러 깔아 둔 사람에게 `latest` 는
    옛 판이고, 그리로 "갱신" 하면 내려가는 것이다.
    """
    return _key(latest) > _key(current)


def installed_version(
    codex_bin: Path,
    *,
    env: Mapping[str, str] | None = None,
    runner: Runner | None = None,
) -> str | None:
    """지금 깔린 판. 못 읽으면 None — codex 가 깨져 있다는 뜻일 수 있다."""
    run = subprocess.run if runner is None else runner
    try:
        out = run(
            [os.fspath(codex_bin), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            env=None if env is None else dict(env),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_version(out.stdout) if out.returncode == 0 else None


def latest_version(
    *,
    env: Mapping[str, str] | None = None,
    runner: Runner | None = None,
) -> str | None:
    """npm 의 `latest` 태그. 모르면 None.

    **HTTP 를 직접 하지 않는다.** 이 패키지는 HTTP 클라이언트가 없다고 README 가 약속하고,
    그 약속이 자격증명을 옮기는 도구를 믿을 근거다. `selfupdate` 가 `git ls-remote` 를 부르듯
    여기서는 codex 옆의 `npm view` 를 부른다 — `env` 가 그 npm 을 PATH 앞에 둔다.

    brew cask 판은 몇 시간 늦을 수 있다. 그때 `codex update` 는 `brew upgrade` 가 할 일이
    없다고 끝나고, 다음 회차에 따라잡는다.

    **모르는 것과 같은 것은 다르다** (`selfupdate.latest_commit` 과 같은 규칙). npm 이 없거나
    네트워크가 없어 못 읽었을 때 "최신이다" 로 접으면 갱신이 있는데도 없다고 믿게 된다.
    호출부는 None 이면 견주지 않고 `codex update` 에 맡긴다.
    """
    run = subprocess.run if runner is None else runner
    try:
        out = run(
            ["npm", "view", PACKAGE, "version"],
            capture_output=True,
            text=True,
            timeout=60,
            env=None if env is None else dict(env),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_version(out.stdout) if out.returncode == 0 else None


def update_command(codex_bin: Path) -> list[str]:
    """codex 자신의 갱신 명령. 설치 방식은 codex 가 가린다."""
    return [os.fspath(codex_bin), "update"]
