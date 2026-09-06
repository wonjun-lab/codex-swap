"""TUI 테스트.

화면 그리기와 상태 변경을 갈라 둔 덕에 curses 없이 전부 검증된다 — 터미널을 띄우는
테스트는 CI 에서 돌지 않으므로, 돌릴 수 있는 형태로 짜는 것이 설계의 일부다.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
from pathlib import Path

import pytest

from codex_swap import cli, tui
from codex_swap.core import config, identity, store


def _write_auth(path: Path, email: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    path.write_text(json.dumps({"tokens": {"id_token": f"h.{payload}.s"}}))
    os.chmod(path, 0o600)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    (home / ".codex/accounts").mkdir(parents=True)
    (home / ".claude").mkdir(parents=True)
    for k in ("CODEX_ROTATE_LADDER", "CODEX_ROTATE_MARGIN", "CODEX_ROTATE_COOLDOWN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_ACCOUNT_DEFAULT_HOME", str(home / ".codex"))
    monkeypatch.setenv("CODEX_ACCOUNTS_DIR", str(home / ".codex/accounts"))
    s = config.load()
    _write_auth(store.active_auth(s), "a@example.com")
    _write_auth(store.slot_auth(s, "master"), "a@example.com")
    _write_auth(store.slot_auth(s, "shared"), "b@example.com")
    return s


def text(view: tui.View) -> str:
    return "\n".join(tui.render_lines(view))


# ── 계정 화면 ────────────────────────────────────────────────────────────────


def test_the_account_list_is_shown_immediately(env) -> None:
    """들어가자마자 어떤 계정이 물려 있는지 보여야 한다 — 그게 이 화면의 목적이다."""
    body = text(tui.build_view(env))
    assert "master" in body and "shared" in body
    assert "a@example.com" in body and "b@example.com" in body


def test_the_active_account_is_marked(env) -> None:
    lines = tui.render_lines(tui.build_view(env))
    marked = [ln for ln in lines if ln.startswith((" >*", "  *"))]
    assert len(marked) == 1 and "master" in marked[0]


def test_the_cursor_moves_and_is_visible(env) -> None:
    view = tui.build_view(env)
    assert " >" in tui.render_lines(view)[3]
    moved = tui.replace(view, cursor=1)
    assert " >" in tui.render_lines(moved)[4]


def test_the_cursor_cannot_run_past_the_list(env) -> None:
    view = tui.build_view(env, cursor=99)
    assert view.cursor == len(view.rows) - 1


def test_an_empty_store_says_what_to_do(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    (home / ".codex/accounts").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_ACCOUNT_DEFAULT_HOME", str(home / ".codex"))
    monkeypatch.setenv("CODEX_ACCOUNTS_DIR", str(home / ".codex/accounts"))
    assert "등록된 계정이 없다" in text(tui.build_view(config.load()))


# ── 전환 ─────────────────────────────────────────────────────────────────────


def test_enter_switches_to_the_selected_account(env) -> None:
    view = tui.replace(tui.build_view(env), cursor=1)  # shared
    after = tui.do_switch(view)
    assert identity.email_of(store.active_auth(env)) == "b@example.com"
    assert "전환했다: shared" in after.message
    # 화면이 새 상태를 반영해야 한다 — 옛 목록을 들고 있으면 활성 표시가 어긋난다.
    assert [r.label for r in after.rows if r.active] == ["shared"]


def test_switching_to_the_active_account_is_refused_gently(env) -> None:
    after = tui.do_switch(tui.build_view(env))  # cursor 0 = master = 활성
    assert "이미 활성" in after.message
    assert identity.email_of(store.active_auth(env)) == "a@example.com"


# ── 자동 전환 스위치 ─────────────────────────────────────────────────────────


def test_toggling_auto_rotation_uses_the_same_file_as_bash(env) -> None:
    """off-switch 는 파일 하나다. bash 와 같은 경로여야 둘이 같은 스위치를 본다."""
    view = tui.build_view(env)
    assert "자동 전환: 켜짐" in text(view)

    off = tui.do_toggle_auto(view)
    assert env.off_switch.exists()
    assert "자동 전환: 꺼짐" in text(off)

    on = tui.do_toggle_auto(off)
    assert not env.off_switch.exists()
    assert "자동 전환: 켜짐" in text(on)


# ── 정책 화면 ────────────────────────────────────────────────────────────────


def test_policy_screen_shows_the_current_values(env) -> None:
    view = tui.replace(tui.build_view(env), mode="policy")
    body = text(view)
    assert "50,70,85,95" in body and "사다리" in body
    assert str(env.margin) in body


def test_arrows_adjust_a_value(env) -> None:
    view = tui.replace(tui.build_view(env), mode="policy", policy_cursor=1)  # margin
    up = tui.adjust_policy(view, +1)
    assert up.settings.margin == env.margin + 1
    down = tui.adjust_policy(up, -1)
    assert down.settings.margin == env.margin


def test_a_value_never_goes_negative(env) -> None:
    view = tui.replace(tui.build_view(env), mode="policy", policy_cursor=1)
    for _ in range(env.margin + 5):
        view = tui.adjust_policy(view, -1)
    assert view.settings.margin == 0


def test_the_ladder_cycles_through_presets(env) -> None:
    view = tui.replace(tui.build_view(env), mode="policy", policy_cursor=0)
    first = tui.adjust_policy(view, +1).settings.ladder
    assert first != env.ladder and first in tui.LADDER_PRESETS


def test_saving_writes_a_file_that_load_reads_back(env) -> None:
    """저장이 실제로 다음 실행에 반영되어야 한다 — 아니면 정책 화면이 장식이다."""
    view = tui.replace(tui.build_view(env), mode="policy", policy_cursor=1)
    view = tui.adjust_policy(view, +3)
    saved = tui.save_policy(view)
    assert "저장했다" in saved.message

    reloaded = config.load()
    assert reloaded.margin == env.margin + 3


def test_the_environment_still_wins_over_the_saved_file(env, monkeypatch) -> None:
    """환경변수가 파일을 이겨야 한다.

    반대로 두면 한 번의 `CODEX_ROTATE_MARGIN=…` 실험이 저장된 값에 막혀 조용히 무시된다.
    """
    config.save_policy(env.accounts_dir, margin=99)
    monkeypatch.setenv("CODEX_ROTATE_MARGIN", "7")
    assert config.load().margin == 7


def test_a_corrupt_config_file_is_ignored(env) -> None:
    """rotate 는 매 codex 호출에 실린다. 깨진 설정 파일로 죽으면 안 된다."""
    (env.accounts_dir / config.CONFIG_NAME).write_text("{ this is not json")
    assert config.load().margin == config.DEFAULT_MARGIN


def test_saved_config_is_not_world_readable(env) -> None:
    path = config.save_policy(env.accounts_dir, margin=5)
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"


# ── 진입점 ───────────────────────────────────────────────────────────────────


def test_a_bare_call_without_a_tty_prints_help(env, capsys, monkeypatch) -> None:
    """파이프·스크립트에서 부르면 대화형 화면이 걸려 영영 안 끝난다."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert cli.main([]) == 0
    assert "usage: codex-swap" in capsys.readouterr().out


# ── 감사에서 나온 결함들 (2026-09-06) ────────────────────────────────────────


def test_adopt_refuses_to_overwrite_another_accounts_slot(env) -> None:
    """critical. `a` 는 이름을 자유 입력받는데, 기존 이름을 치면 그 슬롯의 자격증명이
    덮어써지고 되돌릴 방법이 없다. 같은 계정이면 갱신이므로 허용한다."""
    view = tui.build_view(env)
    after = tui.do_adopt(view, "shared")  # shared 는 b@, 활성은 a@
    assert "덮어쓰지 않는다" in after.message
    assert identity.email_of(store.slot_auth(env, "shared")) == "b@example.com"


def test_adopt_allows_refreshing_the_same_account(env) -> None:
    after = tui.do_adopt(tui.build_view(env), "master")  # master 도 a@, 활성도 a@
    assert "등록했다" in after.message


def test_switch_reads_the_active_account_from_disk(env) -> None:
    """배경에서 rotate 가 돌면 화면의 `*` 가 낡는다. 그걸 믿으면 멀쩡한 전환을 거부한다."""
    view = tui.build_view(env)  # 이 시점 활성 = master
    with store.switch_lock(env):
        store.switch(env, "shared", "external")  # 화면 몰래 바뀜
    moved = tui.replace(view, cursor=0)  # 커서는 여전히 master
    after = tui.do_switch(moved)
    assert "전환했다: master" in after.message


def test_render_lines_touches_no_files(env, monkeypatch) -> None:
    """설계 제약. 순수해야 터미널 없이 테스트할 수 있다."""
    view = tui.build_view(env)
    monkeypatch.setattr(
        Path, "exists", lambda self: (_ for _ in ()).throw(AssertionError("파일 접근"))
    )
    tui.render_lines(view, height=24, width=100)


def test_long_values_cannot_break_the_columns(env) -> None:
    view = tui.build_view(env)
    huge = tui.Row(label="x" * 90, email="y" * 90, used="100%", reset="09-11 07:57", active=False)
    lines = tui.render_lines(tui.replace(view, rows=(huge,)), width=100)
    row = next(ln for ln in lines if "xxx" in ln)
    assert tui._width(row) < 120, f"열이 밀려났다: {len(row)}"


def test_an_unregistered_active_account_is_warned_about(env) -> None:
    """전환하면 sync-back 이 건너뛰어져 지금 자격증명이 사라진다."""
    _write_auth(store.active_auth(env), "stranger@example.com")
    body = text(tui.build_view(env))
    assert "슬롯에 없다" in body and "보관되지 않는다" in body


def test_the_empty_screen_still_shows_messages(env, tmp_path, monkeypatch) -> None:
    """등록 실패가 나오는 화면이다. 메시지가 안 보이면 왜 안 됐는지 알 수 없다."""
    for label in ("master", "shared"):
        shutil.rmtree(store.slot_dir(env, label))
    view = tui.build_view(env, message="등록 실패: 무언가")
    assert "등록 실패" in text(view)


def test_escaping_the_policy_screen_discards_edits(env) -> None:
    """esc 뒤에도 편집값이 남으면 화면이 저장된 것처럼 보인다."""
    view = tui.replace(tui.build_view(env), mode="policy", policy_cursor=1, saved_settings=env)
    edited = tui.adjust_policy(view, +5)
    assert edited.settings.margin != env.margin
    assert "*" in "".join(tui.render_lines(edited))  # 편집 표시


def test_the_ladder_can_return_to_a_non_preset_value(env, monkeypatch) -> None:
    """프리셋에 없는 사다리는 한 번 움직이면 돌아올 수 없었다."""
    odd = tui.replace(env, ladder=(11, 22, 33))
    view = tui.replace(
        tui.build_view(env), settings=odd, saved_settings=odd, mode="policy", policy_cursor=0
    )
    ring = [view.settings.ladder]
    for _ in range(len(tui.LADDER_PRESETS) + 1):
        view = tui.adjust_policy(view, +1)
        ring.append(view.settings.ladder)
    assert (11, 22, 33) in ring[1:], "원래 값으로 못 돌아온다"
