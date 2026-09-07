"""슬롯 저장소 — 라벨 검증 · 열거 · 원자적 전환 · 락.

라벨 검증과 경로 조립이 **같은 모듈에** 있다. 라벨은 그대로 디렉토리 이름이 되므로,
검증을 통과하지 않은 값으로 경로를 만들 수 있는 자리가 하나라도 있으면 그것이 곧
구멍이다 — bash 판에서 두 번에 걸쳐 닫은 것이 정확히 그 자리였고, 한 번에 못 닫은 이유는
검사가 명령마다 흩어져 있어 새 명령에서 빠졌기 때문이다.
"""

from __future__ import annotations

import contextlib
import math
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from codex_swap.core import identity, log, paths
from codex_swap.core.config import Settings

# ── 라벨 계약 (bash 판에서 확정, 설계문 §6.6) ────────────────────────────────
#
# 단일 경로 요소만 통과시킨다. `.` 로 시작하는 이름을 막는 것은 보안이 아니라 규약이다 —
# 내부 상태 파일(.usage-cache.json · .last-rotate · .lock · .last-check)이 같은 디렉토리에
# 살고 열거도 그것들을 라벨로 세지 않으므로, 규약이 어긋나면 등록은 되는데 목록에 안 뜨고
# 지울 수도 없는 유령 슬롯이 생긴다.
#
# 하이픈으로 시작하는 이름도 막는다. 경로가 앞에 붙으므로 옵션으로 오인될 일은 없지만,
# 사용자가 손으로 다루는 값이라 셸에서 옵션처럼 보이는 형태를 애초에 만들지 않는다.
# `$` 가 아니라 `\Z` 다. Python 의 `$` 는 문자열 끝뿐 아니라 **마지막 개행 앞**에서도
# 일치해서 `"valid\n"` 이 통과했다. 라벨은 그대로 디렉토리 이름이 되므로, 개행이 든
# 슬롯이 만들어지고 목록 한 줄이 둘로 갈린다 — bash 판에서 이미 한 번 물린 결함이다.
LABEL_RE = re.compile(r"\A[A-Za-z0-9_][A-Za-z0-9._-]*\Z")
MAX_LABEL_LEN = 64


class StoreError(Exception):
    """슬롯을 다루다 실패했다. rotate 경계에서 `Failed` 로 접힌다."""


def label_syntax_ok(label: str) -> bool:
    """순수 술어. 파일시스템을 만지지 않으므로 단독으로 테스트할 수 있다.

    bash 는 이 검사와 심링크 검사를 한 함수에 묶어 두어, 문자열 규칙만 보고 싶을 때도
    `codex_account_root` 를 부르고 lstat 을 돌려야 했다.

    주의: bash 의 `[[ =~ ]]` 는 glibc 로케일 정렬을 쓰므로 UTF-8 로케일에서 `A-Za-z` 가
    ASCII 밖까지 포함할 수 있다. Python `re` 는 이 클래스를 항상 ASCII 로만 본다. 즉
    여기는 bash 의 `LC_ALL=C` 가지를 영구화한다 — 이미 비-ASCII 라벨 슬롯이 있으면
    목록에서 사라지므로, 설치 전 `doctor` 가 그것을 먼저 보고한다 (설계문 §6.7).
    """
    return bool(label) and len(label) <= MAX_LABEL_LEN and LABEL_RE.match(label) is not None


def slot_dir(settings: Settings, label: str) -> Path:
    """검증을 통과한 라벨로만 부른다. 통과하지 못한 값이면 올린다.

    조립 함수 자체가 관문이라, 검증을 빠뜨린 호출 경로가 생길 수 없다.
    """
    if not label_syntax_ok(label):
        raise StoreError(f"쓸 수 없는 라벨: {label!r}")
    return paths.root(settings) / label


def slot_auth(settings: Settings, label: str) -> Path:
    return slot_dir(settings, label) / "auth.json"


def active_auth(settings: Settings) -> Path:
    """지금 쓰이는 자격증명. 활성 계정이 무엇인지는 언제나 이 파일이 답한다."""
    return settings.default_home / "auth.json"


def slot_is_admissible(settings: Settings, label: str) -> bool:
    """문자열 규칙 + 파일시스템 봉쇄.

    두 번째 검사가 필요한 이유는 라벨이 단일 요소여도 슬롯이 바깥을 가리킬 수 있기
    때문이다. 실측(2026-09-05): 심링크 슬롯을 `use` 는 거부하는데 `rotate` 는 골랐고,
    루트 밖 자격증명으로 실제 전환했다. rotate 는 wrapper 가 매 codex 호출마다 부르므로
    사람의 확인 없이 자동으로 일어난다.

    세 번째 검사(`auth.json` 자체가 심링크)는 bash 에 아직 없다 — 슬롯 디렉토리는 진짜인데
    그 안의 자격증명만 바깥을 가리키면 현재 가드를 통과한다 (설계문 §7.5 D4).
    """
    if not label_syntax_ok(label):
        return False
    root = paths.root(settings)
    d = root / label
    if d.is_symlink():
        return False
    auth = d / "auth.json"
    if auth.exists() or auth.is_symlink():
        try:
            real_root = os.path.realpath(root)
            if os.path.commonpath([real_root, os.path.realpath(auth)]) != real_root:
                return False
        except (OSError, ValueError):
            return False
    return True


def labels(settings: Settings) -> list[str]:
    """등록된 라벨. 사용자 인자와 **같은 관문**을 지난다.

    디스크에서 읽었다고 안전한 값이 아니다. 정렬해서 돌려주는 것은 동률 후보의 승자가
    열거 순서로 정해지기 때문이다 — bash glob 순서와 Python `iterdir()` 순서는 같지
    않으므로, 정렬해 두지 않으면 같은 상태에서 두 구현이 다른 계정을 고른다 (§6.5).
    """
    root = paths.root(settings)
    if not root.is_dir():
        return []
    out = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        name = entry.name
        if not slot_is_admissible(settings, name):
            continue
        if not (root / name / "auth.json").is_file():
            continue
        out.append(name)
    return out


def active_label(settings: Settings) -> str | None:
    """활성 email 과 일치하는 슬롯. 없으면 None.

    사용자가 슬롯에 없는 계정으로 직접 로그인한 상태가 None 이고, 그때는 아무것도
    건드리지 않는다.
    """
    email = identity.email_of(active_auth(settings))
    if email is None:
        return None
    for label in labels(settings):
        if identity.email_of(slot_auth(settings, label)) == email:
            return label
    return None


# ── 락 ───────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LockHeld:
    path: Path


class LockBusy(Exception):
    """다른 프로세스가 전환 중이다. 정상적인 무동작이다."""


class LockUnusable(Exception):
    """락 경로가 디렉토리가 아니다. 사람이 지워야 한다 (설계문 §7.5 D2)."""


STALE_LOCK_SECONDS = 1800


@contextlib.contextmanager
def switch_lock(settings: Settings):
    """`mkdir` 락.

    병행 기간에는 bash 와 **같은 프로토콜**이어야 했다 — `fcntl` 로 바꾸면 bash 가
    무시하고, owner 파일을 넣으면 bash 의 `rmdir` 기반 stale 회수가 영영 실패한다.
    그 제약은 bash 와 함께 사라졌지만 교체하려면 배포된 기기에 남은 락과의 전이를
    설계해야 한다 (`paths.lock_path`).

    아래 둘은 bash 와 갈리던 곳이고 둘 다 의도적이었다.

    D1 — bash 의 `trap … RETURN` 은 함수 스코프가 아니라서 호출자가 반환할 때 **다시**
    발화한다. 두 번째 발화는 그 사이 다른 프로세스가 잡은 락을 지운다. 여기서는
    컨텍스트 매니저가 정확히 한 번 해제한다.

    D2 — 락 경로가 디렉토리가 아니면(0바이트 파일·깨진 심링크) bash 는 `-d` 가 거짓이라
    stale 판정을 아예 돌리지 않고 영구히 실패한다. 복구 경로가 코드에 없다. 여기서는
    세 번째 갈래로 잡아 보고한다.
    """
    lock = paths.lock_path(settings)
    paths.ensure_root(settings)
    try:
        os.mkdir(lock, 0o700)
    except FileExistsError:
        st = os.lstat(lock)
        if not os.path.isdir(lock):
            raise LockUnusable(f"락 경로가 디렉토리가 아니다: {lock}") from None
        if time.time() - st.st_mtime <= STALE_LOCK_SECONDS:
            raise LockBusy(str(lock)) from None
        # 30분 넘게 남아 있으면 죽은 프로세스의 잔해다.
        with contextlib.suppress(OSError):
            os.rmdir(lock)
        try:
            os.mkdir(lock, 0o700)
        except OSError as exc:
            raise LockBusy(str(lock)) from exc
    except OSError as exc:
        raise StoreError(f"락을 잡지 못했다: {exc}") from exc

    try:
        yield LockHeld(lock)
    finally:
        with contextlib.suppress(OSError):
            os.rmdir(lock)


# ── 전환 ─────────────────────────────────────────────────────────────────────


def _install(src: Path, dst: Path, *, keep_mtime: bool) -> None:
    """같은 파일시스템 안의 temp + rename. 반쪽 쓰인 auth.json 이 생기지 않는다.

    `keep_mtime` 이 이 함수의 전부다. 설명은 `switch` 에 있다.
    """
    tmp = dst.with_name(f"{dst.name}.tmp.{os.getpid()}")
    try:
        # copy2 는 mtime 까지 가져오고, copy 는 내용과 권한 비트만 가져온다. 둘 다 권한은
        # 옮기며, 어느 쪽이든 아래 chmod 가 0600 을 확정한다 (계약 8).
        (shutil.copy2 if keep_mtime else shutil.copy)(src, tmp)
        os.chmod(tmp, 0o600)
        if not keep_mtime:
            # **초 단위로 올림한다.** 훅은 `stat %Y` 와 `date +%s` 로 **정수 초**를 견준다
            # (`broker 시작 < auth mtime`). 그래서 같은 초 안에서 broker 가 먼저 뜨고
            # 전환이 뒤따르면 — broker 1000.1, 전환 1000.9 — 정수로는 1000 < 1000 이라
            # 거짓이 되어 그 broker 를 놓친다.
            #
            # 올림하면 1001 이 되어 잡힌다. 대가는 그 1 초 안에 **뒤에** 뜬 broker 를
            # 불필요하게 죽일 수 있다는 것인데, 그 비용은 재시작 한 번이고 놓치는 비용은
            # 옛 계정으로 계속 요청하는 것이다. 기울기가 명확하다.
            stamp = float(math.ceil(time.time()))
            os.utime(tmp, (stamp, stamp))
        os.replace(tmp, dst)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def switch(settings: Settings, target: str, reason: str = "manual") -> None:
    """활성 계정을 `target` 으로 바꾼다. 락은 호출자가 이미 잡고 있어야 한다.

    **실패해도 롤백하지 않는다.** sync-back 이 install 보다 먼저 돌고 되돌려지지 않으므로,
    install 이 실패해도 떠나려던 슬롯은 이미 갱신돼 있다. 이건 결함이 아니라 올바른
    동작이다 — 그 바이트가 그 계정의 **최신 토큰**이고, prepare/commit 으로 감싸 롤백하면
    오히려 그것을 잃는다. 두 효과는 독립적으로 커밋된다 (설계문 §7.5).

    ── mtime 을 두 효과가 다르게 다룬다 ──

    활성 자리(B)의 mtime 은 **"활성 자격증명이 마지막으로 바뀐 시각"** 을 뜻해야 한다.
    Claude 훅이 낡은 broker 를 그 값으로 판정하기 때문이다 —
    `broker 시작 < auth.json mtime` 이면 그 broker 는 옛 토큰을 들고 있는 것이므로 죽인다.

    bash 는 `cp -p` 로 원본 mtime 을 가져왔고 우리도 `copy2` 로 그대로 옮겼다. 그런데
    원본은 **슬롯에 보관된 며칠 전 사본**이다. 그래서 방금 전환했는데도 mtime 이 과거로
    찍히고, 위 조건이 **항상 거짓**이 되어 훅이 낡은 broker 를 하나도 죽이지 못한다.

    실측(2026-09-06, 이 기기): 원장에 그날 다섯 번의 전환이 남아 있는데
    `~/.codex/auth.json` mtime 은 이틀 전(09-04 06:34)이었고, 떠 있던 broker 넷은 전부
    그보다 **뒤에** 시작해 하나도 낡은 것으로 잡히지 않았다. 훅 주석(hook:79-80)은 이
    비교가 "옛 토큰을 든 broker 를 같은 실행에서 내린다" 고 적고 있지만 작동한 적이 없다.

    그래서 (B)는 mtime 을 새로 찍는다. bash 와 갈리는 의도된 divergence 이고, 설계문의
    계약 12("mtime 을 보존한다")는 이 발견으로 폐기됐다 — 그 계약은 bash 충실성만 보고
    보존이 **옳은지**를 묻지 않은 것이었다.

    (A) sync-back 은 계속 보존한다. 저장소 전체를 훑어 슬롯 mtime 을 **판정에 쓰는 코드가
    없음**을 확인했다 — 시각을 읽는 곳은 락 디렉토리·`.last-check`·`.last-rotate`·job 로그
    뿐이다. 그러니 여기서 새로 찍을 이유가 없고, 보존이 더 적은 변경이다.

    다만 그 값을 "이 계정의 자격증명이 마지막으로 갱신된 시각" 이라고 부르면 부정확하다.
    (B)가 활성 자리에 활성화 시각을 찍고 다음 (A)가 그것을 그대로 복사해 오므로, 토큰
    바이트가 그대로여도 슬롯에는 **마지막 활성화 시각**이 남는다.
    """
    if not slot_is_admissible(settings, target):
        raise StoreError(f"쓸 수 없는 라벨: {target!r}")
    target_auth = slot_auth(settings, target)
    if not target_auth.is_file():
        raise StoreError(f"등록되지 않은 라벨: {target}")

    active = active_label(settings)
    live = active_auth(settings)

    # 효과 (A) — 떠나기 전에 지금 쓰던 자격증명을 자기 슬롯에 되쓴다. 그동안 갱신된
    # 토큰이 슬롯 사본에는 없어서, 이걸 빼먹으면 돌아올 때 만료된 토큰을 집는다.
    if active is not None and live.is_file():
        with contextlib.suppress(OSError):
            _install(live, slot_auth(settings, active), keep_mtime=True)

    # 효과 (B) — 대상 자격증명을 활성 자리에 건다. mtime 은 **지금**으로 찍는다.
    _install(target_auth, live, keep_mtime=False)

    log.append(settings, from_label=active, to_label=target, reason=reason)
    with contextlib.suppress(OSError):
        paths.rotate_stamp_path(settings).touch()
    # 활성이 바뀌었으니 캐시의 활성 항목은 무효다.
    with contextlib.suppress(OSError):
        paths.cache_path(settings).unlink()
