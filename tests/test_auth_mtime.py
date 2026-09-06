"""활성 auth.json 의 mtime 은 "언제 바뀌었나" 를 뜻해야 한다.

Claude 훅이 낡은 broker 를 그 값으로 판정한다 — `broker 시작 < auth.json mtime` 이면
그 broker 는 옛 토큰을 들고 있으므로 죽인다.

bash 는 `cp -p` 로 원본 mtime 을 가져왔고 우리도 처음엔 `copy2` 로 따라갔다. 원본은 슬롯에
보관된 며칠 전 사본이므로, 방금 전환했는데도 mtime 이 과거로 찍혀 그 조건이 **항상 거짓**이
됐다. 실측(2026-09-06): 그날 다섯 번 전환했는데 mtime 은 이틀 전이었고 떠 있던 broker 넷이
전부 "안 낡음" 으로 판정됐다.

증상이 없는 결함이다. 아무것도 실패하지 않고 로그도 안 남는다 — 그래서 테스트로 고정한다.
"""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path

import pytest

from codex_swap.core import config, store


def _write_auth(path: Path, email: str, *, mtime: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    path.write_text(json.dumps({"tokens": {"id_token": f"h.{payload}.s"}}))
    os.chmod(path, 0o600)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    (home / ".codex/accounts").mkdir(parents=True)
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_ACCOUNT_DEFAULT_HOME", str(home / ".codex"))
    monkeypatch.setenv("CODEX_ACCOUNTS_DIR", str(home / ".codex/accounts"))
    return config.load()


WEEK = 7 * 86400


def test_a_switch_stamps_the_active_credential_with_now(env) -> None:
    """슬롯 사본이 아무리 오래됐어도 활성 자리의 mtime 은 전환 시각이어야 한다."""
    old = time.time() - WEEK
    _write_auth(store.active_auth(env), "a@example.com", mtime=old)
    _write_auth(store.slot_auth(env, "a"), "a@example.com", mtime=old)
    _write_auth(store.slot_auth(env, "b"), "b@example.com", mtime=old)

    before = time.time()
    with store.switch_lock(env):
        store.switch(env, "b", "test")

    stamped = store.active_auth(env).stat().st_mtime
    assert stamped >= before - 1, (
        f"활성 auth.json 의 mtime 이 과거로 찍혔다 "
        f"({time.time() - stamped:.0f}초 전) — 훅이 낡은 broker 를 못 잡는다"
    )


def test_the_stale_broker_condition_now_fires(env) -> None:
    """훅의 판정을 그대로 재현한다: `broker 시작 < auth mtime` 이면 낡음.

    broker 가 **전환보다 먼저** 떴으면 옛 토큰을 들고 있는 것이므로 반드시 잡혀야 한다.
    """
    old = time.time() - WEEK
    _write_auth(store.active_auth(env), "a@example.com", mtime=old)
    _write_auth(store.slot_auth(env, "a"), "a@example.com", mtime=old)
    _write_auth(store.slot_auth(env, "b"), "b@example.com", mtime=old)

    broker_started = time.time() - 600  # 10분 전에 뜬 broker

    with store.switch_lock(env):
        store.switch(env, "b", "test")

    auth_mtime = store.active_auth(env).stat().st_mtime
    assert broker_started < auth_mtime, "훅 조건이 거짓 — 낡은 broker 가 살아남는다"


def test_a_broker_started_after_the_switch_is_not_stale(env) -> None:
    """반례. 전환 뒤에 뜬 broker 는 새 토큰을 들고 있으므로 죽이면 안 된다."""
    _write_auth(store.active_auth(env), "a@example.com")
    _write_auth(store.slot_auth(env, "a"), "a@example.com")
    _write_auth(store.slot_auth(env, "b"), "b@example.com")

    with store.switch_lock(env):
        store.switch(env, "b", "test")
    auth_mtime = store.active_auth(env).stat().st_mtime

    broker_started = time.time() + 1  # 전환 뒤에 뜬 broker
    assert not (broker_started < auth_mtime), "새 broker 를 낡은 것으로 잡았다"


def test_the_hook_comparison_is_integer_seconds(env) -> None:
    """훅은 float 이 아니라 **정수 초**를 견준다.

    `stat %Y` 와 `date +%s` 는 둘 다 초를 자른다. 그래서 같은 초 안에서 broker 가 먼저
    뜨고 전환이 뒤따르면 — broker 1000.1, 전환 1000.9 — 정수로는 `1000 < 1000` 이라
    거짓이 되어 그 broker 를 놓친다. float 로 비교하는 테스트는 이 구멍을 못 본다.

    올림 덕에 auth mtime 이 다음 초로 넘어가 잡힌다.
    """
    _write_auth(store.active_auth(env), "a@example.com")
    _write_auth(store.slot_auth(env, "a"), "a@example.com")
    _write_auth(store.slot_auth(env, "b"), "b@example.com")

    broker_started = time.time()  # 전환과 같은 초에 뜬 broker
    with store.switch_lock(env):
        store.switch(env, "b", "test")
    auth_mtime = store.active_auth(env).stat().st_mtime

    assert int(broker_started) < int(auth_mtime), (
        "훅의 정수 비교에서 같은 초의 선행 broker 를 놓친다 "
        f"(broker={int(broker_started)}, auth={int(auth_mtime)})"
    )


def test_the_departing_slot_keeps_its_mtime(env) -> None:
    """sync-back 은 보존한다.

    슬롯 사본의 mtime 은 아무도 판정에 쓰지 않고, "이 계정의 자격증명이 마지막으로
    갱신된 시각" 이라는 뜻을 유지하는 편이 자연스럽다. 활성 자리와 의미가 다르다는
    것이 이 테스트의 요지다.
    """
    old = time.time() - WEEK
    _write_auth(store.active_auth(env), "a@example.com", mtime=old)
    _write_auth(store.slot_auth(env, "a"), "a@example.com", mtime=old - 1)
    _write_auth(store.slot_auth(env, "b"), "b@example.com", mtime=old)

    with store.switch_lock(env):
        store.switch(env, "b", "test")

    # 떠난 슬롯에는 활성 자리에 있던 파일이 그 mtime 그대로 들어간다.
    assert abs(store.slot_auth(env, "a").stat().st_mtime - old) < 2


def test_credentials_are_unchanged_by_the_mtime_fix(env) -> None:
    """mtime 만 달라지고 내용은 그대로여야 한다."""
    _write_auth(store.active_auth(env), "a@example.com")
    _write_auth(store.slot_auth(env, "a"), "a@example.com")
    _write_auth(store.slot_auth(env, "b"), "b@example.com")
    expected = store.slot_auth(env, "b").read_bytes()

    with store.switch_lock(env):
        store.switch(env, "b", "test")

    assert store.active_auth(env).read_bytes() == expected
    assert oct(store.active_auth(env).stat().st_mode & 0o777) == "0o600"
