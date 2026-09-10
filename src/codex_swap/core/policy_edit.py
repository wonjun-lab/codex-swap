"""정책 저장과 자동 전환 스위치 — **두 표면이 공유하는 부분.**

`tui` 에만 있던 것을 내렸다. CLI 사용자는 사다리를 바꾸려면 `config.json` 을 손으로
고쳐야 했고, 자동 전환을 끄려면 README 가 시키는 대로 `~/.claude/.codex-rotate-off` 를
직접 만들어야 했다 — 도구가 자기 내부 파일을 사용자에게 떠넘기는 셈이다.

`core/credits.py` 와 같은 이유로 여기 있다. 각 표면이 따로 구현하면 갈리고, 갈리면 약한
쪽이 이 도구의 실제 수준이 된다.

**저장이 곧 반영은 아니다.** 환경변수가 파일을 이기므로(설계상), 저장한 값이 실제로 쓰이는지
확인해서 알려 주는 것까지가 이 모듈의 일이다. 아무 말 없이 "저장했다" 만 하면 사용자는
반영된 줄 알고 같은 값을 다시 넣는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from codex_swap.core import config

FIELDS: tuple[tuple[str, str], ...] = (
    ("ladder", "Ladder"),
    ("margin", "Margin (%p)"),
    ("cooldown", "Cooldown (s)"),
    ("cache_ttl", "Cache TTL (s)"),
    ("check_interval", "Throttle (s)"),
)
"""저장되는 노브. **`tui.POLICY_FIELDS` 와 같은 집합이어야 한다** — 한쪽에만 있는 노브는
그 표면에서만 고칠 수 있고, 사용자는 다른 표면에서 그것을 찾다가 못 찾는다.

`busy_window` 가 없는 것은 `tui` 도 저장하지 않기 때문이다. 여기서 늘리려면 양쪽을 함께
늘려야 하고, 그 대응은 `tests/test_parity.py` 가 잰다.
"""


@dataclass(frozen=True)
class Saved:
    """저장 결과. `shadowed` 는 **저장했지만 안 먹는** 노브의 이름."""

    path: Path
    shadowed: tuple[str, ...] = ()


def save(settings: config.Settings, **values: object) -> Saved:
    """정책을 파일에 얹고, 그중 실제로 반영되지 않는 것을 함께 돌려준다.

    `values` 는 `FIELDS` 의 키다. 주지 않은 노브는 파일에 있던 값이 그대로 남는다 —
    `config.save_policy` 가 병합이라서다.
    """
    unknown = set(values) - {key for key, _ in FIELDS}
    if unknown:
        raise ValueError(f"not a policy knob: {', '.join(sorted(unknown))}")

    path = config.save_policy(settings.accounts_dir, **values)

    # **저장한 뒤 다시 읽어서 견준다.** 환경변수가 이기는지는 이름 목록으로 짐작하지 않고
    # 실제 결과로 판정한다 — 우선순위 규칙이 나중에 바뀌어도 이 검사는 따라간다.
    fresh = config.load()
    shadowed = []
    for key, title in FIELDS:
        if key not in values:
            continue
        want = tuple(values[key]) if key == "ladder" else values[key]  # type: ignore[arg-type]
        got = tuple(fresh.ladder) if key == "ladder" else getattr(fresh, key)
        if got != want:
            shadowed.append(title)
    return Saved(path=path, shadowed=tuple(shadowed))


def auto_on(settings: config.Settings) -> bool:
    """자동 전환이 켜져 있나. 스위치는 **파일 하나의 존재**다 (bash 와 같은 파일)."""
    return not settings.off_switch.exists()


def set_auto(settings: config.Settings, on: bool) -> bool:
    """자동 전환을 켜거나 끈다. 바뀐 결과를 돌려준다.

    이미 그 상태면 아무것도 하지 않는다 — `touch` 가 mtime 을 바꾸는데, 그 파일의 나이를
    보고 무언가를 판단하는 코드가 나중에 생길 수 있다.
    """
    switch = settings.off_switch
    if on == auto_on(settings):
        return on
    if on:
        switch.unlink(missing_ok=True)
    else:
        switch.parent.mkdir(parents=True, exist_ok=True)
        switch.touch()
    return on
