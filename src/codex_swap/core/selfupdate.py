"""자기 자신을 갱신한다 — **어떻게 깔렸는지 알아내는 것**이 일의 대부분이다.

사용자가 알아야 할 것은 "새 버전 받아라" 하나인데, 그 한 줄을 실행하려면 uv 로 깔았는지
pipx 인지 pip 인지, 깃에서 왔는지 로컬 경로에서 왔는지를 알아야 한다. 그걸 사람에게
물으면 도구가 할 일을 사람에게 미루는 것이다.

**추측하지 않는다.** 파이썬은 이 둘을 이미 기록해 둔다:

- `direct_url.json` (PEP 610) — 어디서 설치했나. 깃 URL 과 그때의 커밋까지 들어 있다.
- `sys.prefix` — 어느 도구의 우리에 들어앉았나. uv 와 pipx 는 경로가 다르다.

그래서 여기 있는 것은 **읽기와 조립**뿐이다. 실행은 `cli` 가 하고, 무엇을 물을지도 그쪽
몫이다. 이 갈래는 전부 순수 함수라 실제로 무언가를 갈아엎지 않고도 테스트할 수 있다 —
자기 자신을 교체하는 코드에서 그 성질이 특히 중요하다.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

PACKAGE = "codex-swap"

FALLBACK_SOURCE = "git+https://github.com/wonjun-lab/codex-swap.git"
"""출처를 못 읽었을 때 사람에게 보여 줄 주소.

**여기서 자동으로 실행하지는 않는다.** 어디서 깔렸는지 모르는 채로 이 주소를 밀어 넣으면,
포크나 사내 미러에서 깔아 쓰던 사람의 설치를 조용히 원본으로 바꿔 놓는다.
"""


class UpdateError(Exception):
    """갱신을 시작할 수 없다. **아직 아무것도 바뀌지 않았다.**"""


@dataclass(frozen=True)
class Install:
    """지금 돌고 있는 이 프로그램이 어디서 왔는가."""

    manager: str
    """`uv` · `pipx` · `pip`. 갱신 명령의 앞부분을 정한다."""

    source: str
    """다시 설치할 때 건네줄 것. 깃 URL 이거나 로컬 경로다."""

    commit: str | None = None
    """깃에서 왔다면 **설치된 그 커밋**. 최신과 견주는 기준이 된다."""

    editable: bool = False
    """`pip install -e` 로 깔린 경우. 재설치가 아니라 `git pull` 이 맞는 자리다."""

    local_path: Path | None = None
    """로컬 경로에서 깔렸다면 그 경로. 거기가 깃이면 무엇이 바뀌는지까지 보여 줄 수 있다."""

    @property
    def is_git(self) -> bool:
        return self.source.startswith("git+")


def _dist_direct_url() -> dict | None:
    """설치 기록을 읽는다. 없으면 None(PyPI 의 일반 설치 등)."""
    try:
        from importlib.metadata import distribution

        raw = distribution(PACKAGE).read_text("direct_url.json")
    except Exception:
        return None
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _manager(prefix: str | None = None) -> str:
    """어느 도구의 우리에 들어앉았나.

    경로로 가른다. uv 와 pipx 는 각자 정해진 자리에 가상환경을 만들고, 그 자리가 곧
    갱신 명령을 정한다 — uv 로 깔린 것을 `pip install` 로 덮으면 그 도구가 관리하던
    실행 파일과 어긋난다.
    """
    where = (prefix if prefix is not None else sys.prefix).replace(os.sep, "/")
    if "/uv/tools/" in where:
        return "uv"
    if "/pipx/venvs/" in where:
        return "pipx"
    if "/Cellar/" in where or "/homebrew/" in where or "/linuxbrew/" in where:
        # **여기를 놓치면 pip 이 brew 의 설치를 헤집는다.** brew 는 자기 Cellar 안의
        # 가상환경을 스스로 관리하는데, 그 안에서 `pip install --force-reinstall` 을
        # 돌리면 brew 가 아는 상태와 실제가 갈린다. 갱신은 brew 에게 맡긴다.
        return "brew"
    return "pip"


def detect(prefix: str | None = None) -> Install | None:
    """이 설치의 출처. 알아낼 수 없으면 None.

    None 은 실패가 아니라 **모른다**이다. 호출부는 그때 사람에게 명령을 보여 주고 손을
    뗀다 — 자기 자신을 갈아엎는 일에서 추측으로 진행하면 안 된다.
    """
    info = _dist_direct_url()
    if info is None:
        return None
    url = info.get("url")
    if not isinstance(url, str) or not url:
        return None

    manager = _manager(prefix)
    vcs = info.get("vcs_info")
    if isinstance(vcs, dict) and vcs.get("vcs") == "git":
        commit = vcs.get("commit_id")
        revision = vcs.get("requested_revision")
        # 요청한 가지가 있으면 그대로 따라간다. `@main` 을 빼고 다시 깔면 기본 가지로
        # 옮겨가, 사용자가 고정해 둔 가지에서 조용히 벗어난다.
        source = f"git+{url}" + (f"@{revision}" if isinstance(revision, str) and revision else "")
        return Install(
            manager=manager,
            source=source,
            commit=commit if isinstance(commit, str) else None,
        )

    if url.startswith("file://"):
        path = Path(url[len("file://") :])
        editable = bool(isinstance(info.get("dir_info"), dict) and info["dir_info"].get("editable"))
        return Install(
            manager=manager,
            source=str(path),
            editable=editable,
            local_path=path,
            commit=head_of(path),
        )
    return None


def head_of(repo: Path) -> str | None:
    """그 디렉토리가 깃 저장소면 지금 HEAD. 아니면 None."""
    try:
        out = subprocess.run(
            ["git", "-C", os.fspath(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def latest_commit(install: Install, timeout: int = 20) -> str | None:
    """받아올 쪽의 최신 커밋. 모르면 None.

    **모르는 것과 같은 것은 다르다.** 네트워크가 없어 못 읽었을 때 "최신이다" 라고 하면
    사용자는 갱신이 있는데도 없다고 믿는다. 그래서 실패는 None 이고, 호출부는 비교를
    건너뛰고 그냥 갱신한다.
    """
    if install.local_path is not None:
        return head_of(install.local_path)
    if not install.is_git:
        return None

    url, _, revision = install.source[len("git+") :].partition("@")
    args = ["git", "ls-remote", url, revision or "HEAD"]
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    first = out.stdout.split("\n", 1)[0].split("\t", 1)[0].strip()
    return first or None


def upgrade_command(install: Install) -> list[str]:
    """갱신 명령. **재설치이지 업그레이드가 아니다.**

    `uv tool upgrade` · `pipx upgrade` 는 버전 번호를 견주는데, 깃에서 깔린 것은 번호가
    안 움직인다(`0.1.0` 그대로 새 커밋이 온다). 그래서 그 명령들은 "이미 최신" 이라며
    아무것도 안 하는 수가 있다. 강제 재설치는 그 함정을 통째로 피한다.
    """
    if install.editable:
        raise UpdateError(
            f"this is an editable install at {install.source}. "
            "Update it with git instead: git -C "
            f"{install.source} pull"
        )
    if install.manager == "brew":
        # 우리가 실행하지 않는다. brew 가 관리하는 것을 pip 으로 덮으면 brew 가 아는
        # 상태와 실제가 갈리고, 그 뒤로는 brew 쪽 명령이 전부 어긋난다.
        raise UpdateError(
            "this was installed with Homebrew, which manages its own updates. "
            "Run: brew upgrade codex-swap"
        )
    if install.manager == "uv":
        return ["uv", "tool", "install", "--force", install.source]
    if install.manager == "pipx":
        return ["pipx", "install", "--force", install.source]
    pip = [sys.executable, "-m", "pip", "install", "--upgrade", "--force-reinstall"]
    return [*pip, install.source]


def changes_between(install: Install, old: str, new: str, limit: int = 20) -> list[str]:
    """두 커밋 사이에 무엇이 들어왔는지 한 줄씩. 알 수 없으면 빈 목록.

    로컬 저장소에서 깔린 경우에만 답할 수 있다 — 원격만 아는 상태로는 커밋 목록을 받아올
    방법이 없고, 그것 때문에 저장소를 통째로 내려받는 것은 이 명령이 할 일이 아니다.
    """
    if install.local_path is None or old == new:
        return []
    try:
        out = subprocess.run(
            ["git", "-C", os.fspath(install.local_path), "log", "--oneline", f"{old}..{new}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return []
    if out.returncode != 0:
        return []
    lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    return lines[:limit]


def short(commit: str | None) -> str:
    """커밋을 사람이 읽는 길이로. 없으면 `?`."""
    return "?" if not commit else commit[:7]
