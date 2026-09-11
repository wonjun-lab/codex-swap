"""계정 슬롯의 이름 변경·삭제 경계와 파일시스템 변경."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from codex_swap.core import cache, identity, store
from codex_swap.core.config import Settings

RefusalReason = Literal["invalid_label", "missing_source", "destination_exists"]


class SlotRefusal(Exception):
    """사용자 입력이나 슬롯 상태 때문에 변경을 시작하지 않았다."""

    def __init__(self, reason: RefusalReason, label: str) -> None:
        """표면이 자기 문구로 설명할 수 있도록 이유와 라벨을 보존한다."""
        super().__init__(label)
        self.reason = reason
        self.label = label


@dataclass(frozen=True)
class SlotDescription:
    """삭제 확인에 필요한 슬롯 신원과 활성 여부."""

    label: str
    email: str | None
    active: bool


def _require_label(label: str) -> None:
    """라벨이 경로가 되기 전에 공통 문법 관문을 적용한다."""
    if not store.label_syntax_ok(label):
        raise SlotRefusal("invalid_label", label)


def _require_slot(settings: Settings, label: str) -> Path:
    """라벨 문법과 실제 비심링크 디렉토리 조건을 함께 적용한다."""
    _require_label(label)
    target = store.slot_dir(settings, label)
    if not target.is_dir() or target.is_symlink():
        raise SlotRefusal("missing_source", label)
    return target


def describe(settings: Settings, label: str) -> SlotDescription:
    """실제 디렉토리인 슬롯만 삭제·이름 변경 대상으로 설명한다.

    디스크에서 발견됐다는 사실만으로 슬롯으로 믿으면 숨김 이름이나 심링크가 명령별
    문법 관문을 우회한다. 그래서 문자열과 파일 종류를 한 경계에서 함께 확인한다.
    """
    _require_slot(settings, label)
    return SlotDescription(
        label=label,
        email=identity.email_of(store.slot_auth(settings, label)),
        active=store.active_label(settings) == label,
    )


def rename(settings: Settings, old: str, new: str) -> None:
    """슬롯 디렉토리를 락 안에서 새 라벨로 옮기고 캐시를 비운다.

    전환도 같은 디렉토리를 읽고 쓰므로 실제 이동에 같은 락을 쓴다. 목적지의 빈
    디렉토리도 기존 이름이다. 덮으면 사용자가 둔 상태가 사라지므로 명시적인 삭제
    전에는 건드리지 않는다.
    """
    _require_label(old)
    _require_label(new)
    source = _require_slot(settings, old)
    destination = store.slot_dir(settings, new)
    if destination.exists():
        raise SlotRefusal("destination_exists", new)
    with store.switch_lock(settings):
        os.rename(source, destination)
    # 캐시는 라벨로만 색인된다. 옛 이름의 숫자가 남으면 그 이름을 재사용할 때 삭제와
    # 같은 오염이 생긴다. 한 키만 옮기지 않고 파일째 버리는 것은 전환과도 같다.
    cache.clear(settings)


def remove(settings: Settings, label: str) -> SlotDescription:
    """실제 슬롯 디렉토리 하나를 지우고 라벨 기반 캐시를 비운다."""
    description = describe(settings, label)
    shutil.rmtree(store.slot_dir(settings, label))
    # 어느 계정의 값인지 없는 라벨 캐시를 남기면, 같은 이름을 재사용한 새 계정의
    # 사용량과 정책 입력이 지운 계정의 숫자를 최대 한 TTL 동안 뒤집어쓴다.
    cache.clear(settings)
    return description
