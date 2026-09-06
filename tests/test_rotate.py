"""rotate 조립 테스트 — 관문 순서·부수효과·fail-open 경계.

`policy` 는 순수 함수라 따로 시험한다(test_policy.py). 여기서 보는 것은 그 판단을
**둘러싼** 것들이다: 어떤 순서로 관문을 지나는지, 무엇을 언제 디스크에 쓰는지, 그리고
실패가 밖으로 새지 않는지.

프로브는 주입한다. 실제 codex 바이너리도 네트워크도 없이 전 경로가 돈다.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

from codex_swap.core import config, identity, paths, rotate, store
from codex_swap.core.types import Failed, Indeterminate, NoOp, ProbeResult, Switched, Usage


def _write_auth(path: Path, email: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    path.write_text(json.dumps({"tokens": {"id_token": f"h.{payload}.s"}}))
    os.chmod(path, 0o600)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """격리된 HOME. 실제 계정은 어떤 경로로도 닿지 않는다."""
    home = tmp_path / "home"
    (home / ".codex/accounts").mkdir(parents=True)
    (home / ".claude").mkdir(parents=True)
    for k in (
        "CODEX_ROTATE_SKIP",
        "CODEX_HOME",
        "CODEX_ROTATE_LADDER",
        "CODEX_ROTATE_CHECK_INTERVAL",
        "CODEX_ROTATE_COOLDOWN",
        "CODEX_ROTATE_BUSY_WINDOW",
        "CODEX_ROTATE_CACHE_TTL",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_ACCOUNT_DEFAULT_HOME", str(home / ".codex"))
    monkeypatch.setenv("CODEX_ACCOUNTS_DIR", str(home / ".codex/accounts"))
    monkeypatch.setenv("CODEX_ROTATE_STATE_ROOT", str(tmp_path / "state"))
    # 프로브를 주입하므로 값 자체는 쓰이지 않지만, 탐색이 돌지 않게 막아 둔다.
    monkeypatch.setenv("CODEX_ACCOUNT_BIN", "/bin/true")
    return home


def settings() -> config.Settings:
    return config.load()


def two_accounts(home: Path, active_pct: int, other_pct: int) -> config.Settings:
    s = settings()
    _write_auth(store.active_auth(s), "a@example.com")
    _write_auth(store.slot_auth(s, "a"), "a@example.com")
    _write_auth(store.slot_auth(s, "b"), "b@example.com")
    return s


def probe_map(mapping: dict[str, ProbeResult]):
    """home 경로의 마지막 요소로 어느 계정인지 가른다."""

    def fn(codex_bin: str, home: str | None) -> ProbeResult:
        key = Path(home).name if home else ""
        return mapping.get(key, ProbeResult.unknown())

    return fn


def ok(pct: int, *, reached: bool = False) -> ProbeResult:
    return ProbeResult.of(Usage(used_percent=pct, reached=reached))


# ── 관문 사다리 ──────────────────────────────────────────────────────────────


def test_skip_flag_short_circuits_before_anything_else(env, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_ROTATE_SKIP", "1")
    s = two_accounts(env, 95, 1)
    d = rotate.rotate(s, probe_fn=probe_map({}))
    assert isinstance(d, NoOp) and d.reason == "CODEX_ROTATE_SKIP"
    # 스탬프도 찍히지 않아야 한다 — 관문이 그보다 앞이다.
    assert not paths.check_stamp_path(s).exists()


def test_skip_is_non_emptiness_not_a_boolean(env, monkeypatch) -> None:
    """`CODEX_ROTATE_SKIP=0` 은 bash 에서 회전을 **끈다**.

    일반적인 불리언 파서는 이를 false 로 읽어 정반대로 동작한다.
    """
    monkeypatch.setenv("CODEX_ROTATE_SKIP", "0")
    s = two_accounts(env, 95, 1)
    assert isinstance(rotate.rotate(s, probe_fn=probe_map({})), NoOp)


def test_off_switch_file_stops_rotation(env) -> None:
    s = two_accounts(env, 95, 1)
    s.off_switch.parent.mkdir(parents=True, exist_ok=True)
    s.off_switch.touch()
    d = rotate.rotate(s, probe_fn=probe_map({}))
    assert isinstance(d, NoOp) and "off switch" in d.reason


def test_codex_home_guard_compares_physical_paths(env, monkeypatch) -> None:
    """계약 13. 문자열 비교면 후행 슬래시 하나로 가드가 뚫린다."""
    s = two_accounts(env, 95, 1)
    monkeypatch.setenv("CODEX_HOME", str(s.default_home) + "/")
    d = rotate.rotate(s, probe_fn=probe_map({".codex": ok(95), "b": ok(1)}))
    # 같은 홈이므로 가드에 걸리지 않고 정상 판단으로 간다.
    assert isinstance(d, Switched)


def test_codex_home_elsewhere_is_left_alone(env, monkeypatch, tmp_path) -> None:
    s = two_accounts(env, 95, 1)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "elsewhere"))
    d = rotate.rotate(s, probe_fn=probe_map({}))
    assert isinstance(d, NoOp) and "CODEX_HOME" in d.reason


# ── 스탬프 ───────────────────────────────────────────────────────────────────


def test_check_stamp_is_written_before_the_decision(env) -> None:
    """계약 11. 성공 경로가 아니라 진입 직후에 찍힌다.

    아래는 아무것도 전환하지 않는 상황인데도 스탬프가 남아야 한다 — 그렇지 않으면 실패가
    반복될 때 스로틀이 걸리지 않아 매 codex 호출이 프로브를 돈다.
    """
    s = two_accounts(env, 10, 10)
    d = rotate.rotate(s, probe_fn=probe_map({".codex": ok(10), "b": ok(10)}))
    assert isinstance(d, NoOp)
    assert paths.check_stamp_path(s).exists()


def test_dry_run_skips_both_the_throttle_read_and_the_write(env) -> None:
    s = two_accounts(env, 95, 1)
    d = rotate.rotate(s, dry_run=True, probe_fn=probe_map({".codex": ok(95), "b": ok(1)}))
    assert isinstance(d, Switched)
    assert not paths.check_stamp_path(s).exists()
    # 판단만 하고 실제로 바꾸지는 않는다.
    assert identity.email_of(store.active_auth(s)) == "a@example.com"


def test_throttle_blocks_a_second_call(env) -> None:
    s = two_accounts(env, 95, 1)
    first = rotate.rotate(s, probe_fn=probe_map({".codex": ok(95), "b": ok(1)}), now=1000.0)
    assert isinstance(first, Switched)
    second = rotate.rotate(s, probe_fn=probe_map({}), now=1010.0)
    assert isinstance(second, NoOp) and second.reason == "throttled"


# ── 지름길 ───────────────────────────────────────────────────────────────────


def test_below_first_rung_does_not_probe_candidates(env) -> None:
    """주중 대부분의 호출이 여기서 끝난다. 후보를 프로브하면 비용이 헛나간다."""
    probed: list[str] = []

    def fn(codex_bin: str, home: str | None) -> ProbeResult:
        probed.append(Path(home).name if home else "")
        return ok(10)

    s = two_accounts(env, 10, 0)
    d = rotate.rotate(s, probe_fn=fn)
    assert isinstance(d, NoOp) and "first rung" in d.reason
    assert probed == [".codex"], f"후보까지 프로브했다: {probed}"


# ── 로그아웃 복구 ────────────────────────────────────────────────────────────


def test_logged_out_recovery_works_with_a_single_slot(env) -> None:
    s = settings()
    _write_auth(store.slot_auth(s, "only"), "only@example.com")
    d = rotate.rotate(s, probe_fn=probe_map({"only": ok(42)}))
    assert isinstance(d, Switched) and d.to_label == "only"
    assert identity.email_of(store.active_auth(s)) == "only@example.com"


def test_logged_out_with_no_slots_is_indeterminate(env) -> None:
    d = rotate.rotate(settings(), probe_fn=probe_map({}))
    assert isinstance(d, Indeterminate)


# ── fail-open 경계 ───────────────────────────────────────────────────────────


def test_a_raising_probe_never_escapes(env) -> None:
    """계약 2. 여기서 새면 트레이스백이 사용자 터미널에 실린다."""

    def boom(codex_bin: str, home: str | None) -> ProbeResult:
        raise RuntimeError("token=sk-secret-should-never-surface")

    s = two_accounts(env, 95, 1)
    d = rotate.rotate(s, probe_fn=boom)
    assert isinstance(d, Failed)


def test_an_unusable_lock_is_reported_not_deadlocked(env) -> None:
    """§7.5 D2. bash 는 이 경우 stale 판정을 아예 돌리지 않아 영구 교착이다."""
    s = two_accounts(env, 95, 1)
    paths.lock_path(s).write_text("")  # 디렉토리가 아니라 파일
    d = rotate.rotate(s, probe_fn=probe_map({".codex": ok(95), "b": ok(1)}))
    assert isinstance(d, Failed) and "락" in d.reason


def test_a_held_lock_is_an_ordinary_no_op(env) -> None:
    s = two_accounts(env, 95, 1)
    paths.lock_path(s).mkdir()
    d = rotate.rotate(s, probe_fn=probe_map({".codex": ok(95), "b": ok(1)}))
    assert isinstance(d, NoOp) and "in progress" in d.reason


# ── 전환 ─────────────────────────────────────────────────────────────────────


def test_a_switch_syncs_back_before_installing(env) -> None:
    """계약 5. 떠나기 전 현재 자격증명을 자기 슬롯에 되쓴다.

    슬롯 사본이 뒤처져 있으면 돌아올 때 만료 토큰을 집는다. 여기서는 활성 홈의 내용을
    슬롯과 다르게 만들어 두고, 전환 후 슬롯이 갱신됐는지 본다.
    """
    s = two_accounts(env, 95, 1)
    # id_token 은 그대로 둔다. 여기를 망가뜨리면 활성 판정이 먼저 실패해서(활성 email 을
    # 못 읽으니 어느 슬롯과도 안 맞는다) sync-back 에 닿기도 전에 무동작으로 끝난다.
    live = json.loads(store.active_auth(s).read_text())
    live["mark"] = "live"
    store.active_auth(s).write_text(json.dumps(live))

    d = rotate.rotate(s, probe_fn=probe_map({".codex": ok(95), "b": ok(1)}))
    assert isinstance(d, Switched)
    assert json.loads(store.slot_auth(s, "a").read_text()).get("mark") == "live"


def test_a_switch_leaves_a_ledger_line_without_credentials(env) -> None:
    s = two_accounts(env, 95, 1)
    d = rotate.rotate(s, probe_fn=probe_map({".codex": ok(95), "b": ok(1)}))
    assert isinstance(d, Switched)
    ledger = paths.log_path(s).read_text()
    assert "a -> b" in ledger
    assert "id_token" not in ledger and "eyJ" not in ledger


def test_a_switch_invalidates_the_cache(env) -> None:
    s = two_accounts(env, 95, 1)
    d = rotate.rotate(s, probe_fn=probe_map({".codex": ok(95), "b": ok(1)}))
    assert isinstance(d, Switched)
    assert not paths.cache_path(s).exists()


@pytest.mark.parametrize(
    ("window", "age", "expected"),
    [
        # 창 안이면 busy. 경계(나이 == 창)는 열려 있다 — 비교가 strict `<` 다.
        (100, 50, True),
        (100, 99, True),
        (100, 100, False),
        # 여기가 bash 를 걷어내며 좁아진 자리다. 예전에는 `ceil(100/60)*60 = 120` 이라
        # 나이 110 초가 busy 였다. 지금은 적은 값 그대로 100 초라 not-busy 다.
        (100, 110, False),
        # 기본값 180 은 분의 배수라 두 계산이 같다 — 실사용 동작이 바뀌지 않는 이유다.
        (180, 170, True),
        (180, 190, False),
        # 0 이면 가드 자체를 끈다.
        (0, 0, False),
    ],
)
def test_busy_window_is_seconds_not_rounded_up_to_minutes(
    env, monkeypatch, tmp_path: Path, window: int, age: int, expected: bool
) -> None:
    """busy 창은 **적은 초 그대로**다.

    bash 가 있던 동안에는 그쪽 `find -mmin` 폴백에 맞추려고 `ceil(window/60)*60` 으로
    올렸다. 그 패리티가 사라졌으므로 60 의 배수가 아닌 값은 이제 적은 대로 동작한다.
    """
    monkeypatch.setenv("CODEX_ROTATE_BUSY_WINDOW", str(window))
    s = settings()
    assert s.busy_window == window
    root = s.rotate_state_root
    root.mkdir(parents=True, exist_ok=True)
    log = root / "job" / "run.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("")
    now = 1_000_000.0
    os.utime(log, (now - age, now - age))
    assert rotate.busy(s, now) is expected


def test_busy_ignores_non_log_files_and_a_missing_root(env) -> None:
    """`*.log` 만 센다. 루트가 없으면 busy 가 아니라 **판단을 막지 않는다**."""
    s = settings()
    assert rotate.busy(s, 1_000_000.0) is False
    root = s.rotate_state_root
    root.mkdir(parents=True, exist_ok=True)
    other = root / "run.jsonl"
    other.write_text("")
    os.utime(other, (999_999.0, 999_999.0))
    assert rotate.busy(s, 1_000_000.0) is False
