"""상태 파일 경로와 루트 생성.

병행 기간 동안 bash 판과 **같은 상태를 공유**한다. 그래서 파일 이름은 한 글자도
달라서는 안 된다 — 두 구현이 같은 `.last-check` 로 스로틀을 걸고 같은 `.lock` 으로
직렬화하며 같은 캐시를 읽어야 한다. 이름이 어긋나면 오류가 나는 게 아니라 상태가
조용히 두 벌 생기고, 그건 나중에 "rotate 가 두 번 돌았다" 로만 관찰된다.

슬롯 경로(`<root>/<label>`)는 여기 없다. 라벨은 검증을 통과한 뒤에만 경로로
조립해도 되는 값이라(설계문 §6.6), 조립과 검증을 한 곳에 두려고 `store` 가 갖는다.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

from codex_swap.core.config import Settings

# bash 의 `codex_account_*_path` 가 만들던 이름 그대로. 상수로 뽑아 두는 것은 차등
# 테스트가 경로 조립이 아니라 **이름 자체**를 단언할 수 있게 하기 위해서다.
LOG_NAME = "rotate.log"
CACHE_NAME = ".usage-cache.json"
ROTATE_STAMP_NAME = ".last-rotate"
CHECK_STAMP_NAME = ".last-check"
LOCK_NAME = ".lock"

# 상태 파일이 슬롯과 같은 디렉토리에 산다. 충돌하지 않는 것은 방어가 아니라 규약
# 덕이다 — 라벨은 `.` 로 시작할 수 없고 슬롯 열거는 디렉토리만 센다. 점 없는 이름은
# `rotate.log` 하나뿐인데 이 이름은 라벨 정규식을 통과하므로, 같은 이름의 슬롯을
# 만들려 하면 파일이 이미 있어서 mkdir 이 실패한다.


def root(settings: Settings) -> Path:
    """`codex_account_root` — 슬롯과 상태 파일이 함께 사는 디렉토리.

    파생 자체는 `config` 에서 끝났다(`CODEX_ACCOUNTS_DIR` 또는 기본 홈 아래
    `accounts`). 그래도 한 번 감싸 두는 것은 호출부가 `settings.accounts_dir` 를
    직접 조립하지 않게 하기 위해서다. 조립이 흩어지면 루트를 옮길 때 한 곳이 남는다.
    """
    return settings.accounts_dir


def log_path(settings: Settings) -> Path:
    """전환 원장. 여기에 토큰을 남기지 않는다 (계약 7)."""
    return settings.accounts_dir / LOG_NAME


def cache_path(settings: Settings) -> Path:
    """사용량 캐시. 전환하면 활성 항목이 무효라 통째로 지운다.

    이 파일을 쓰는 경로가 루트의 두 생성자 중 하나다 — 그래서 `ensure_root` 를
    지나야 한다 (§7.5 D5).
    """
    return settings.accounts_dir / CACHE_NAME


def rotate_stamp_path(settings: Settings) -> Path:
    """`codex_account_stamp_path` — 쿨다운 기준 시각.

    전환에 **성공**했을 때만 찍힌다. 아래 `check_stamp_path` 와 헷갈리면 안 된다 —
    이름이 비슷하지만 하나는 성공 경로, 하나는 진입 직후다.
    """
    return settings.accounts_dir / ROTATE_STAMP_NAME


def check_stamp_path(settings: Settings) -> Path:
    """스로틀 기준 시각. 판단 **전에** 찍는다 (계약 11 · 설계문 §5.2).

    bash 에는 이 경로만 함수가 없어 `$(codex_account_root)/.last-check` 가 스로틀
    검사와 rotate 진입 두 곳에 따로 적혀 있다. 두 문자열이 어긋나면 검사와 기록이
    다른 파일을 보게 되어 스로틀이 영영 걸리지 않고, 매 codex 호출이 프로브를 돈다.
    한 함수로 묶어 그 실패 자체를 없앤다.
    """
    return settings.accounts_dir / CHECK_STAMP_NAME


def lock_path(settings: Settings) -> Path:
    """전환 직렬화용 mkdir 락. **파일이 아니라 디렉토리**다.

    병행 기간에는 bash 와 같은 프로토콜이어야 하므로 `fcntl` 이나 owner 파일로
    바꾸지 않는다 (설계문 §10). 이 경로가 디렉토리가 아닌 경우(0바이트 파일·깨진
    심링크)는 bash 에서 stale 판정이 아예 돌지 않아 영구 교착이 된다 — `store` 가
    그것을 세 번째 갈래로 잡아 보고한다 (§7.5 D2).
    """
    return settings.accounts_dir / LOCK_NAME


def ensure_root(settings: Settings) -> Path:
    """루트를 만들고 0700 을 **강제**한다. 루트를 만드는 경로는 전부 여기를 지난다.

    bash 에는 생성자가 둘이고 모드가 다르다 — CLI 의 `ensure_root` 는 `mkdir -p`
    뒤에 `chmod 700` 을 하지만, `codex_account_cache_write` 는 부모에 `mkdir -p` 만
    한다. rotate 는 codex 스폰마다 돌고 그 안에서 프로브가 캐시를 쓰므로, 실제로는
    캐시 쓰기가 먼저 루트를 만드는 쪽이 흔하다. 그러면 자격증명 디렉토리가 umask
    모드(보통 0755)로 생긴다 (설계문 §7.5 D5 · 계약 8).

    `mkdir(mode=0o700)` 만으로는 못 고친다. mode 는 umask 로 깎이는 데다 **이미 있는
    디렉토리에는 아예 무시되므로**, 한 번 0755 로 생긴 루트는 그대로 남는다. 그래서
    둘 다 건다 — 생성 모드는 0755 로 보이는 창을 없애고, chmod 는 이미 있는 루트를
    고친다.

    생성 실패는 올린다. CLI 는 그것으로 죽고, rotate 는 fail-open 경계에서 "전환 안
    함" 으로 접는다.
    """
    p = settings.accounts_dir
    p.mkdir(mode=0o700, parents=True, exist_ok=True)
    # bash 의 `chmod ... || true` 와 같게 실패는 삼킨다. 루트가 남의 소유라 모드를
    # 못 고치는 것이 전환을 통째로 막을 이유는 아니다 — 여기서 죽으면 rotate 가
    # fail-open 이 아니라 fail-closed 가 된다. 중간 부모는 `mkdir -p` 와 같이
    # 건드리지 않는다(보통 `~/.codex` 이고 codex 자신의 것이다).
    with contextlib.suppress(OSError):
        os.chmod(p, 0o700)
    return p
