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

from codex_swap.core import wiring

# 앱 공존 테스트 파일들이 함께 쓰는 임시 HOME 픽스처(`box`). 여기서 한 번 등록하는 까닭은
# `_coexist.py` 의 docstring 에 적었다.
pytest_plugins = ("_coexist",)

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
    # 마지막 줄은 오늘 기준 **중복**이다 — `config.load` 가 `default_home / "accounts"` 로
    # 접으므로 위 두 줄만으로도 격리된다(뮤테이션으로 확인: 이 줄을 지워도 아무 테스트도
    # 빨개지지 않는다). 그래도 남긴다. 픽스처가 지켜야 할 것은 "격리" 지 "`config.load` 의
    # 현재 파생 규칙" 이 아니고, 그 규칙이 바뀌는 날 조용히 새면 증상은 또 엉뚱한 곳에서
    # 나온다. 값이 셋 다 명시돼 있으면 그 결합이 끊긴다.
    monkeypatch.setenv("CODEX_ACCOUNTS_DIR", str(home / ".codex/accounts"))
    # macOS 개발기에는 실제 `/Applications/ChatGPT.app`가 있을 수 있다. HOME만 바꾸면
    # 전역 설치 경로가 여전히 보여 앱 설치/제거 테스트의 가짜 기계가 항상 "설치됨"으로
    # 굳는다. 기본 경로도 임시 HOME 아래로 옮기며, 앱이 필요한 fixture는 직접 만든다.
    # `~`를 남겨야 자기 fixture가 HOME을 다시 잡는 테스트도 그 새 임시 홈을 따른다.
    monkeypatch.setattr(wiring, "APP_PATHS", ("~/Applications/ChatGPT.app",))
    # `app_installed(paths=APP_PATHS)`의 기본 인자는 import 시점 튜플을 붙든다. APP_PATHS만
    # 갈아끼우면 인자를 생략한 실제 호출은 여전히 호스트 `/Applications`를 본다. 명시한
    # paths는 그대로 존중하면서, 생략한 경우에만 현재 테스트 경로를 전달한다.
    original_app_installed = wiring.app_installed

    def isolated_app_installed(paths=None):
        return original_app_installed(wiring.APP_PATHS if paths is None else paths)

    monkeypatch.setattr(wiring, "app_installed", isolated_app_installed)
    return home
