"""버전 번호는 세 곳에 적힌다 — `pyproject.toml`, `codex_swap.__version__`, `uv.lock`.

`--version` 은 `__version__` 을 읽고, 패키지 메타데이터와 brew formula 는 `pyproject.toml` 을
읽는다. 한쪽만 올리면 사용자가 보는 번호와 설치된 판이 갈린다. 첫 릴리스를 찍으며 셋을 묶는다.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import codex_swap

ROOT = Path(__file__).resolve().parent.parent


def _declared() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]


def test_the_cli_reports_the_version_the_package_declares() -> None:
    assert codex_swap.__version__ == _declared()


def test_the_lockfile_was_refreshed_with_the_version() -> None:
    """CI 는 `uv sync --frozen` 이라 낡은 잠금 파일을 고치지 않고 그대로 쓴다."""
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    ours = [p for p in lock["package"] if p["name"] == "codex-swap"]
    assert [p["version"] for p in ours] == [_declared()]
