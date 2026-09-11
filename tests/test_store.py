"""락 소유권과 라벨 경계가 무너지면 다른 슬롯의 자격증명까지 바뀔 수 있다."""

import contextlib
import os
from pathlib import Path

import pytest

from codex_swap.core import config, paths, store


@pytest.fixture
def settings(tmp_path: Path) -> config.Settings:
    return config.load(
        {
            "HOME": str(tmp_path),
            "CODEX_ACCOUNT_DEFAULT_HOME": str(tmp_path / "home"),
            "CODEX_ACCOUNTS_DIR": str(tmp_path / "accounts"),
            "CODEX_ROTATE_STATE_ROOT": str(tmp_path / "state"),
        }
    )


@pytest.mark.parametrize(
    "label",
    ["", ".", "..", ".hidden", "-option", "a/b", "a\\b", "a b", "a\t", "a\x00", "한글", "a" * 65],
)
def test_invalid_label_never_becomes_slot(settings: config.Settings, label: str) -> None:
    assert not store.label_syntax_ok(label)
    assert not store.slot_is_admissible(settings, label)
    with pytest.raises(store.StoreError, match="not a usable label"):
        store.slot_dir(settings, label)
    with pytest.raises(store.StoreError, match="not a usable label"):
        store.switch(settings, label)
    assert not settings.accounts_dir.exists()


def test_trailing_newline_is_not_a_label(settings: config.Settings) -> None:
    """Python 의 `$` 는 문자열 끝뿐 아니라 **마지막 개행 앞**에서도 일치한다.

    그래서 `LABEL_RE` 가 `$` 를 쓰던 동안 `"valid\n"` 이 유효한 라벨이었다. 라벨은 그대로
    디렉토리 이름이 되므로 개행이 든 슬롯이 만들어지고, 목록에서 한 줄이 둘로 갈린다 —
    bash 판에서 이미 한 번 물린 결함이다(핸드오버 §2).
    """
    assert not store.label_syntax_ok("valid\n")
    assert not store.slot_is_admissible(settings, "valid\n")
    with pytest.raises(store.StoreError, match="not a usable label"):
        store.slot_dir(settings, "valid\n")


@pytest.mark.parametrize("label", ["a", "_private", "A9._-", "a" * 64])
def test_label_boundaries_accept_valid_names(settings: config.Settings, label: str) -> None:
    assert store.label_syntax_ok(label)
    assert store.slot_dir(settings, label) == settings.accounts_dir / label


@pytest.mark.parametrize("age", [0, 1800])
def test_busy_lock_is_preserved(
    settings: config.Settings, monkeypatch: pytest.MonkeyPatch, age: int
) -> None:
    lock = paths.ensure_root(settings) / ".lock"
    lock.mkdir()
    os.utime(lock, (10_000 - age, 10_000 - age))
    monkeypatch.setattr(store.time, "time", lambda: 10_000)
    with pytest.raises(store.LockBusy), store.switch_lock(settings):
        pytest.fail("사용 중인 락에 진입했다")
    assert lock.is_dir()
    assert lock.stat().st_mtime == 10_000 - age


@pytest.mark.parametrize("kind", ["file", "broken-symlink"])
def test_unusable_lock_is_preserved(settings: config.Settings, kind: str) -> None:
    lock = paths.ensure_root(settings) / ".lock"
    if kind == "file":
        lock.write_text("owner")
    else:
        lock.symlink_to(settings.accounts_dir / "missing")
    with (
        pytest.raises(store.LockUnusable, match="lock path is not a directory"),
        store.switch_lock(settings),
    ):
        pytest.fail("쓸 수 없는 락에 진입했다")
    if kind == "file":
        assert lock.read_text() == "owner"
    else:
        assert lock.is_symlink()


def test_stale_lock_is_reclaimed_then_released(
    settings: config.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = paths.ensure_root(settings) / ".lock"
    lock.mkdir()
    os.utime(lock, (8199, 8199))
    monkeypatch.setattr(store.time, "time", lambda: 10_000)
    with store.switch_lock(settings) as held:
        assert held.path == lock
        assert lock.is_dir() and lock.stat().st_mode & 0o777 == 0o700
        with pytest.raises(store.LockBusy), store.switch_lock(settings):
            pytest.fail("회수한 락을 동시에 획득했다")
    assert not lock.exists()


def test_nonempty_stale_lock_remains_busy(
    settings: config.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = paths.ensure_root(settings) / ".lock"
    lock.mkdir()
    (lock / "owner").write_text("preserve")
    os.utime(lock, (0, 0))
    monkeypatch.setattr(store.time, "time", lambda: 10_000)
    with pytest.raises(store.LockBusy), store.switch_lock(settings):
        pytest.fail("회수할 수 없는 락에 진입했다")
    assert (lock / "owner").read_text() == "preserve"


def test_exception_releases_lock(settings: config.Settings) -> None:
    with pytest.raises(RuntimeError, match="작업 실패"), store.switch_lock(settings):
        raise RuntimeError("작업 실패")
    assert not paths.lock_path(settings).exists()
    with store.switch_lock(settings) as held:
        assert held.path.is_dir()


def test_lock_creation_error_is_store_error(
    settings: config.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = paths.ensure_root(settings) / ".lock"
    original = os.mkdir

    def denied(path: str | Path, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        if Path(path) == lock:
            raise PermissionError("락 생성 거부")
        original(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(store.os, "mkdir", denied)
    with (
        pytest.raises(store.StoreError, match="could not take the lock"),
        store.switch_lock(settings),
    ):
        pytest.fail("생성 실패 뒤 락에 진입했다")
    assert not lock.exists()


@pytest.mark.parametrize("error", [OSError, ValueError])
def test_unresolvable_auth_is_not_admissible(
    settings: config.Settings, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    auth = store.slot_auth(settings, "a")
    auth.parent.mkdir(parents=True)
    auth.write_text("{}")

    def failed(*args: object) -> str:
        raise error("경로 해석 실패")

    monkeypatch.setattr(store.os.path, "commonpath", failed)
    assert not store.slot_is_admissible(settings, "a")


def test_failed_install_keeps_destination_and_removes_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dst = tmp_path / "auth.json"
    dst.write_bytes(b"old")
    src = tmp_path / "incoming"
    src.write_bytes(b"new")

    def failed_replace(source: Path, destination: Path) -> None:
        assert source.read_bytes() == b"new"
        assert destination == dst
        raise PermissionError("교체 거부")

    monkeypatch.setattr(store.os, "replace", failed_replace)
    with pytest.raises(PermissionError, match="교체 거부"):
        store._install(src, dst, keep_mtime=False)
    assert dst.read_bytes() == b"old"
    assert set(tmp_path.iterdir()) == {src, dst}


@pytest.mark.parametrize("kind", ["slot", "auth"])
def test_symlink_cannot_escape_accounts_root(
    settings: config.Settings, tmp_path: Path, kind: str
) -> None:
    # 문자열 접두사가 같은 형제도 루트 밖이다. commonprefix 로 바꾸면 봉쇄가 뚫린다.
    outside = tmp_path / "accounts-other"
    outside.mkdir()
    (outside / "auth.json").write_text("outside")
    slot = paths.ensure_root(settings) / "a"
    if kind == "slot":
        slot.symlink_to(outside, target_is_directory=True)
    else:
        slot.mkdir()
        (slot / "auth.json").symlink_to(outside / "auth.json")
    assert not store.slot_is_admissible(settings, "a")
    assert store.labels(settings) == []
    assert (outside / "auth.json").read_text() == "outside"


def test_missing_root_has_no_labels(settings: config.Settings) -> None:
    assert store.labels(settings) == []
    assert not settings.accounts_dir.exists()


def test_unregistered_target_cannot_replace_live_auth(settings: config.Settings) -> None:
    live = store.active_auth(settings)
    live.parent.mkdir(parents=True, exist_ok=True)  # 설정 로드가 활성 홈을 이미 만든다
    live.write_bytes(b"preserve")
    with pytest.raises(store.StoreError, match="label is not registered"):
        store.switch(settings, "missing")
    assert live.read_bytes() == b"preserve"


def test_the_temp_credential_file_is_never_wider_than_0600(tmp_path, monkeypatch) -> None:
    """전이 중에도 토큰이 남에게 보이면 안 된다.

    `shutil.copy` 계열은 dst 를 만든 **뒤** 권한을 옮긴다. 그 사이 파일은 umask 모드로
    존재하고(흔한 개발 기기에서 0664), temp 는 `~/.codex` 안에 생기는데 그 디렉토리는
    codex 소유라 0775 인 기기가 있다 — 매 전환마다 로컬 타 계정이 OAuth 토큰을 읽을 수
    있는 창이 열린다. 실측으로 0664 를 잡았다.

    같은 저장소가 이미 옳은 패턴을 갖고 있다: `cache._opener` 가 email 만 담긴 캐시를
    처음부터 0600 으로 만든다. 정작 토큰 파일에 적용되지 않았다.

    관측은 `os.chmod` 를 가로채서 한다 — 실제 경쟁으로 창을 잡으려면 큰 파일과 스레드가
    필요하고 그건 재현이 흔들린다. 여기서 보는 것은 "chmod 이 오기 전에 이미 0600 인가" 다.
    """
    src = tmp_path / "src.json"
    src.write_text('{"tokens": {}}')
    os.chmod(src, 0o600)
    dst = tmp_path / "dst.json"

    seen: list[str] = []
    real_chmod = os.chmod

    def spy(path, mode, *a, **k):
        # chmod 이 불리는 시점의 **현재** 모드를 기록한다. 이미 0600 이면 창이 없다.
        with contextlib.suppress(OSError):
            seen.append(oct(os.stat(path).st_mode & 0o777))
        return real_chmod(path, mode, *a, **k)

    monkeypatch.setattr(os, "chmod", spy)
    monkeypatch.setattr(os, "umask", lambda mask: 0o022)
    store._install(src, dst, keep_mtime=False)

    assert seen, "temp 에 chmod 이 걸리지 않았다"
    assert all(m == "0o600" for m in seen), f"0600 보다 넓은 창이 있었다: {seen}"
    assert oct(dst.stat().st_mode & 0o777) == "0o600"
