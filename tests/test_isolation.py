"""`tests/conftest.py` 의 격리가 살아 있는지 본다.

격리가 풀리면 증상이 **엉뚱한 곳에서** 나온다. 실제로 그랬다 — 이 기기의
`~/.codex/accounts/config.json` 에 사다리가 `90` 으로 저장돼 있었고, 코드를 한 줄도
건드리지 않았는데 `test_policy.py` 다섯 건이 빨개졌다. 원인을 찾는 데 걸린 시간은 전부
"정책 코드가 잘못됐나" 를 뒤지는 데 썼다.

그래서 격리 자체를 재는 테스트를 따로 둔다. 여기가 먼저 터지면 범인이 코드가 아니라
환경이라는 것을 그 자리에서 알 수 있다.

이 파일이 `conftest.py` 안에 있으면 안 되는 이유: **pytest 는 `conftest.py` 를 테스트
모듈로 수집하지 않는다.** 처음에 거기 뒀다가 한 번도 돌지 않았고, `HOME` 이관을 지워
보는 뮤테이션이 살아남아서야 알았다.
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path

from codex_swap.core import config

REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
"""passwd 엔트리의 홈. **`Path.home()` 을 쓰면 안 된다** — 그건 `$HOME` 을 먼저 읽으므로
픽스처가 옮겨 놓은 임시 홈을 돌려주고, `os.environ["HOME"] != Path.home()` 은 격리 여부와
무관하게 언제나 거짓이 된다. 처음 쓴 단언이 정확히 그 모양이었다.
"""


def test_home_points_away_from_the_real_home() -> None:
    assert Path(os.environ["HOME"]) != REAL_HOME


def test_settings_do_not_resolve_into_the_real_home() -> None:
    s = config.load()
    for path in (s.default_home, s.accounts_dir, s.rotate_state_root):
        assert REAL_HOME not in path.parents, path
        assert path != REAL_HOME, path


def test_policy_knobs_are_defaults_not_the_developers_saved_file() -> None:
    """실제로 났던 실패. 저장된 정책 파일이 새면 사다리부터 달라진다."""
    s = config.load()
    assert s.ladder == config.DEFAULT_LADDER
    assert s.margin == config.DEFAULT_MARGIN
    assert s.cooldown == config.DEFAULT_COOLDOWN
    assert s.cache_ttl == config.DEFAULT_CACHE_TTL


def test_a_saved_policy_in_the_isolated_home_is_still_read(_isolated_home: Path) -> None:
    """격리가 설정 읽기를 **망가뜨린** 것이 아님을 같이 잡아 둔다.

    이것이 없으면 위 세 건은 "설정을 아예 못 읽게 만들었다" 로도 통과한다.
    """
    config.save_policy(_isolated_home / ".codex/accounts", ladder=[42])
    assert config.load().ladder == (42,)
