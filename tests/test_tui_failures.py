"""전환·조회가 **실패했을 때** 화면이 무엇을 말하는가.

행복한 경로는 `test_tui.py` 가 촘촘히 잰다. 여기는 그 반대편이다 — 락이 잡혀 있고,
디스크를 못 읽고, codex 를 못 찾는 경우.

이 자리가 조용하면 사용자는 **아무 일도 안 일어난 줄 안다.** 그러고 다시 누른다. 전환은
자격증명을 바꾸는 동작이라 "눌렀는데 반응이 없다" 의 대가가 크다. 그런데 이 경로들은
커버리지에 잡히지 않고 있었다.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from codex_swap import tui
from codex_swap.core import config, store


def _auth(path: Path, email: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    claims = base64.urlsafe_b64encode(json.dumps({"email": email}).encode())
    path.write_text(json.dumps({"tokens": {"id_token": f"h.{claims.decode().rstrip('=')}.s"}}))


@pytest.fixture
def env(_isolated_home: Path) -> config.Settings:
    s = config.load()
    _auth(s.accounts_dir / "master/auth.json", "a@example.com")
    _auth(s.accounts_dir / "shared/auth.json", "b@example.com")
    _auth(s.default_home / "auth.json", "a@example.com")
    return s


def _on_shared(env: config.Settings) -> tui.View:
    """커서를 비활성 계정에 둔다 — 전환이 실제로 일어나려는 상태."""
    view = tui.build_view(env)
    at = next(i for i, row in enumerate(view.rows) if row.label == "shared")
    return tui.replace(view, cursor=at)


# ── 락 ──────────────────────────────────────────────────────────────────────


def test_a_busy_lock_says_to_try_again_rather_than_nothing(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """다른 전환이 도는 중이다. 이것은 오류가 아니라 **기다리라는 안내**여야 한다."""

    def busy(_: config.Settings):
        raise store.LockBusy("held")

    monkeypatch.setattr(store, "switch_lock", busy)
    after = tui.do_switch(_on_shared(env))
    assert "in progress" in after.message, after.message
    assert "Try again" in after.message, after.message


def test_an_unusable_lock_says_why(env: config.Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """락 자리를 아예 못 쓰는 경우(권한·읽기전용). 사유가 화면에 남아야 손을 쓸 수 있다."""

    def unusable(_: config.Settings):
        raise store.LockUnusable("cannot use /nope/lock: Permission denied")

    monkeypatch.setattr(store, "switch_lock", unusable)
    after = tui.do_switch(_on_shared(env))
    assert "Permission denied" in after.message, after.message


def test_a_store_error_reaches_the_screen_verbatim(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`store` 가 지어낸 문구가 더 구체적이다. 여기서 덮어쓰면 그 구체성을 잃는다."""

    def boom(*_: object, **__: object) -> None:
        raise store.StoreError("slot 'shared' has no auth.json")

    monkeypatch.setattr(store, "switch", boom)
    after = tui.do_switch(_on_shared(env))
    assert after.message == "slot 'shared' has no auth.json", after.message


def test_an_unexpected_failure_is_labelled_as_one(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """예상 못 한 예외. 트레이스백이 화면을 덮는 것보다 한 줄이 낫다.

    `store` 가 지어낸 문구와 달리 이쪽은 접두사가 필요하다 — 무슨 말인지 모를 문장이
    그냥 떠 있으면 사용자는 그것이 전환에 대한 말인지조차 모른다.
    """

    def boom(*_: object, **__: object) -> None:
        raise RuntimeError("disk went away")

    monkeypatch.setattr(store, "switch", boom)
    after = tui.do_switch(_on_shared(env))
    assert after.message == "Switch failed: disk went away", after.message


def test_a_failed_switch_keeps_the_cursor_where_it_was(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """실패했는데 커서가 튀면 사용자는 다음 `s` 를 **다른 계정**에 누른다."""

    def busy(_: config.Settings):
        raise store.LockBusy("held")

    monkeypatch.setattr(store, "switch_lock", busy)
    before = _on_shared(env)
    after = tui.do_switch(before)
    assert tui.selected_row(after) is not None
    assert tui.selected_row(after).label == "shared", tui.selected_row(after)


# ── 디스크를 못 읽을 때 ─────────────────────────────────────────────────────


def test_being_unable_to_read_the_active_slot_is_said_out_loud(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """디스크가 화면을 만든 **뒤에** 나빠진 경우.

    뷰를 먼저 만드는 것이 이 테스트의 전부다 — `build_view` 도 같은 함수를 지나므로
    먼저 막으면 계정이 하나도 없는 화면이 되고, 그러면 `s` 가 "No accounts yet" 으로
    빠져 재려던 분기에 닿지 못한다.
    """
    view = _on_shared(env)

    def boom(_: config.Settings) -> str:
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(store, "active_label", boom)
    after = tui.do_switch(view)
    assert "Could not read state" in after.message, after.message


def test_switching_with_no_accounts_says_so(_isolated_home: Path) -> None:
    view = tui.build_view(config.load())
    assert not view.rows
    assert tui.do_switch(view).message == "No accounts yet"


# ── 조회 ────────────────────────────────────────────────────────────────────


def test_a_missing_codex_binary_names_itself(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """조회는 codex 를 부른다. 못 찾은 것과 조회가 실패한 것은 다른 조치를 부른다."""

    def boom() -> str:
        raise RuntimeError("no codex on PATH")

    monkeypatch.setattr(tui, "resolve_codex_bin", boom)
    after = tui.do_refresh(tui.build_view(env))
    assert "Could not find codex" in after.message, after.message
    assert "no codex on PATH" in after.message, after.message


def test_being_unable_to_list_slots_during_a_refresh_is_said_out_loud(
    env: config.Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """codex 는 찾았는데 슬롯을 못 읽는 경우.

    바이너리를 먼저 세워 두지 않으면 그 앞 단계(`Could not find codex`)에서 끝나
    재려던 분기에 닿지 못한다. 두 문구가 다른 조치를 부르므로 갈려야 한다.
    """
    view = tui.build_view(env)
    fake = tmp_path / "codex"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o700)
    monkeypatch.setattr(tui, "resolve_codex_bin", lambda: fake)

    def boom(_: config.Settings) -> str:
        raise OSError(5, "I/O error")

    monkeypatch.setattr(store, "active_label", boom)
    after = tui.do_refresh(view)
    assert "Could not read slots" in after.message, after.message
    assert "Could not find codex" not in after.message, after.message
