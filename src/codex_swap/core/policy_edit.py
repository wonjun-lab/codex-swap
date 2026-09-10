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


class CreditsFileError(Exception):
    """설정 파일을 읽지 못해 저장을 멈췄다. **파일은 그대로다.**"""


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

    # **사다리를 여기서 정규화한다.** TUI 는 입력을 받을 때 정렬·중복 제거를 했는데 CLI 는
    # 안 했다. 그래서 `--ladder 90,50,70` 은 그대로 저장됐고, 정책은 **첫 상회 항목**을
    # 관문으로 고르므로 같은 사다리가 두 표면에서 다른 관문을 냈다. 판단이 갈리는 종류의
    # 차이라 표시 문제가 아니다.
    if "ladder" in values:
        rungs = values["ladder"]
        values = {**values, "ladder": sorted(dict.fromkeys(rungs))}  # type: ignore[arg-type]

    # **읽지 못하는 파일 위에 덮어쓰지 않는다.** `_file_config` 는 깨진 파일을 `{}` 로
    # 접는데(rotate 핫패스를 위한 옳은 침묵이다), 저장은 그 `{}` 에 병합하므로 **파일에
    # 있던 다른 키가 통째로 사라진다.** 그러고 나면 정상 JSON 이라 경고도 없다.
    broken = config.file_config_error(settings.accounts_dir)
    if broken:
        raise CreditsFileError(
            f"the policy file is there but cannot be read, so saving would erase it: {broken}"
        )

    path = config.save_policy(settings.accounts_dir, **values)

    # **저장한 뒤 다시 읽어서 견준다.** 환경변수가 이기는지는 이름 목록으로 짐작하지 않고
    # 실제 결과로 판정한다 — 우선순위 규칙이 나중에 바뀌어도 이 검사는 따라간다.
    # **여기서 아는 것은 "요청한 값이 실효값과 다르다" 까지다.** 원인은 대개 환경변수지만,
    # 파일을 다른 프로세스가 방금 깨뜨렸을 수도 있다. 그래서 이름은 `shadowed` 로 두고
    # 원인을 단정하는 것은 표면의 문구에 맡긴다 — 실제로 첫 판에서는 이 값을 보고
    # "environment variables win" 이라고 **단정**했다.
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


def auto_blocked_by_env(settings: config.Settings) -> bool:
    """스위치는 켜져 있는데 **환경변수가 회전을 막고 있나.**

    `CODEX_ROTATE_SKIP` 은 비어 있지 않기만 하면 회전을 끈다(bash 의 `[[ -n ]]` 의미).
    파일 스위치만 보고 "on" 이라고 말하면, 실제로는 한 번도 안 도는데 화면은 정상이라고
    한다 — codex 교차 검토가 잡은 자리다.
    """
    return settings.skip
