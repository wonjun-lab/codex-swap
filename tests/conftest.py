"""모든 테스트를 실제 홈에서 떼어 놓는다.

`test_policy.py` 는 `config.load({})` 로 격리했다고 믿고 있었지만, 그 함수는 인자를
**`os.environ` 에 병합**할 뿐이라 빈 딕셔너리는 아무 일도 하지 않는다. 그래서 그 테스트는
개발자 기기의 `~/.codex/accounts/config.json` 을 읽었고, 사다리를 `90` 으로 바꿔 둔
기기에서 통째로 실패했다 — 코드는 그대로인데 테스트가 빨개진다.

여기서 막지 않으면 같은 함정이 다음 테스트에서 다시 열린다. 격리는 파일마다 기억해야 하는
것이 아니라 **기본값**이어야 한다.
"""

from __future__ import annotations

import pytest

_LEAKY = (
    "HOME",
    "CODEX_HOME",
    "CODEX_ACCOUNT_DEFAULT_HOME",
    "CODEX_ACCOUNTS_DIR",
    "CODEX_ROTATE_STATE_ROOT",
    "CODEX_ROTATE_LADDER",
    "CODEX_ROTATE_MARGIN",
    "CODEX_ROTATE_COOLDOWN",
    "CODEX_ROTATE_CACHE_TTL",
    "CODEX_ROTATE_CHECK_INTERVAL",
    "CODEX_ROTATE_BUSY_WINDOW",
    "CODEX_ROTATE_SKIP",
    "CODEX_ROTATE_FORCE",
    "CODEX_ACCOUNT_BIN",
    "CODEX_REAL_BIN",
)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory, monkeypatch):
    """실제 홈·설정·자격증명이 어떤 테스트에도 닿지 않게 한다.

    자기 픽스처로 다시 덮어쓰는 테스트는 그대로 동작한다 — 여기서 하는 일은 **바닥을
    안전한 곳에 까는 것**이지 값을 강요하는 것이 아니다.
    """
    home = tmp_path_factory.mktemp("home")
    (home / ".codex/accounts").mkdir(parents=True)
    for name in _LEAKY:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_ACCOUNT_DEFAULT_HOME", str(home / ".codex"))
    monkeypatch.setenv("CODEX_ACCOUNTS_DIR", str(home / ".codex/accounts"))
    return home


def test_the_suite_cannot_see_the_real_config() -> None:
    """이 파일 자체가 지키는 것. 격리가 풀리면 여기서 먼저 터진다."""
    import os
    from pathlib import Path

    assert Path(os.environ["HOME"]) != Path.home()
    from codex_swap.core import config

    s = config.load()
    assert Path.home() not in s.accounts_dir.parents, s.accounts_dir
