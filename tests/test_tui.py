"""TUI 테스트.

화면 그리기와 상태 변경을 갈라 둔 덕에 curses 없이 전부 검증된다 — 터미널을 띄우는
테스트는 CI 에서 돌지 않으므로, 돌릴 수 있는 형태로 짜는 것이 설계의 일부다.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import threading
import time
import unicodedata
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
    """계정 다음에 메뉴가 이어지므로 클램프는 그 끝이다."""
    view = tui.build_view(env, cursor=99)
    assert view.cursor == tui.cursor_limit(view)
    assert tui.selected_menu(view) == tui.MENU[-1][0]


def test_an_empty_store_says_what_to_do(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    (home / ".codex/accounts").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_ACCOUNT_DEFAULT_HOME", str(home / ".codex"))
    monkeypatch.setenv("CODEX_ACCOUNTS_DIR", str(home / ".codex/accounts"))
    assert "No accounts yet" in text(tui.build_view(config.load()))


# ── 전환 ─────────────────────────────────────────────────────────────────────


def test_enter_switches_to_the_selected_account(env) -> None:
    view = tui.replace(tui.build_view(env), cursor=1)  # shared
    after = tui.do_switch(view)
    assert identity.email_of(store.active_auth(env)) == "b@example.com"
    assert "Switched to shared" in after.message
    # 화면이 새 상태를 반영해야 한다 — 옛 목록을 들고 있으면 활성 표시가 어긋난다.
    assert [r.label for r in after.rows if r.active] == ["shared"]


def test_switching_to_the_active_account_is_refused_gently(env) -> None:
    after = tui.do_switch(tui.build_view(env))  # cursor 0 = master = 활성
    assert "is already active" in after.message
    assert identity.email_of(store.active_auth(env)) == "a@example.com"


# ── 자동 전환 스위치 ─────────────────────────────────────────────────────────


def test_toggling_auto_rotation_uses_the_same_file_as_bash(env) -> None:
    """off-switch 는 파일 하나다. bash 와 같은 경로여야 둘이 같은 스위치를 본다."""
    view = tui.build_view(env)
    assert "Auto switch: on" in text(view)

    off = tui.do_toggle_auto(view)
    assert env.off_switch.exists()
    assert "Auto switch: off" in text(off)

    on = tui.do_toggle_auto(off)
    assert not env.off_switch.exists()
    assert "Auto switch: on" in text(on)


# ── 정책 화면 ────────────────────────────────────────────────────────────────


def test_policy_screen_shows_the_current_values(env) -> None:
    view = tui.replace(tui.build_view(env), mode="policy")
    body = text(view)
    assert "50,70,85,95" in body and "Ladder" in body
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
    assert "Saved:" in saved.message

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
    assert "Not overwriting" in after.message
    assert identity.email_of(store.slot_auth(env, "shared")) == "b@example.com"


def test_adopt_allows_refreshing_the_same_account(env) -> None:
    after = tui.do_adopt(tui.build_view(env), "master")  # master 도 a@, 활성도 a@
    assert "Adopted master" in after.message


def test_switch_reads_the_active_account_from_disk(env) -> None:
    """배경에서 rotate 가 돌면 화면의 `*` 가 낡는다. 그걸 믿으면 멀쩡한 전환을 거부한다."""
    view = tui.build_view(env)  # 이 시점 활성 = master
    with store.switch_lock(env):
        store.switch(env, "shared", "external")  # 화면 몰래 바뀜
    moved = tui.replace(view, cursor=0)  # 커서는 여전히 master
    after = tui.do_switch(moved)
    assert "Switched to master" in after.message


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
    assert "is not in any slot" in body and "will not keep it" in body


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


# ── 사용량은 항상 보인다 ─────────────────────────────────────────────────────
#
# `?` 하나가 세 가지 다른 사정을 덮고 있었다 — TTL(기본 300 초) 만료, 전환 직후의 캐시
# 삭제, 그리고 활성이 사다리 첫 칸 아래라 rotate 가 후보를 아예 조회하지 않은 경우.
# 사용자에게는 셋이 구별되지 않아 "토큰이 끊겼나" 로 읽힌다. 아래가 그 세 경로다.


def _cache_usage(s: config.Settings, label: str, pct: int, *, age: int = 0) -> None:
    """캐시에 사용량 하나를 심는다. `age` 초만큼 과거로 찍는다."""
    from codex_swap.core import cache

    cache.write(s, label, {"usedPercent": pct, "resetsAt": None}, now=_now() - age)


def _now() -> int:
    import time

    return int(time.time())


def test_a_fresh_cached_usage_is_shown_plain(env) -> None:
    _cache_usage(env, "master", 38)
    row = next(r for r in tui.build_view(env).rows if r.label == "master")
    assert (row.used, row.known, row.stale) == ("38%", True, False)


def test_an_expired_cache_still_shows_the_number_marked_stale(env) -> None:
    """TTL 이 지났다고 화면이 물음표가 되면 안 된다.

    지난 값은 "읽지 못했다" 가 아니라 "5 분 지났다" 다. 낡았다고 표시하며 보여 주는
    편이 언제나 낫다 — 물음표는 사용자에게 계정이 끊긴 것으로 보인다.
    """
    _cache_usage(env, "master", 38, age=env.cache_ttl + 10)
    row = next(r for r in tui.build_view(env).rows if r.label == "master")
    assert (row.used, row.known, row.stale) == ("~38%", True, True)


def test_a_switch_wipes_the_cache_but_the_screen_keeps_the_numbers(env) -> None:
    """전환은 캐시를 파일째 지운다(정책 쪽의 옳은 동작). 화면은 직전 값을 이어받는다."""
    _cache_usage(env, "master", 38)
    _cache_usage(env, "shared", 70)
    before = tui.build_view(env)
    assert [r.used for r in before.rows] == ["38%", "70%"]

    after = tui.do_switch(tui.replace(before, cursor=1))  # shared 로 전환
    from codex_swap.core import paths

    assert not paths.cache_path(env).exists(), "전환이 캐시를 지우지 않았다 — 전제가 깨졌다"
    assert [r.used for r in after.rows] == ["~38%", "~70%"]
    assert all(r.known for r in after.rows)


def test_carry_never_overrides_a_value_that_is_on_disk(env) -> None:
    """이어받기는 **빈 자리만** 메운다. 디스크에 새 값이 있으면 그것이 이긴다."""
    stale_screen = {
        "master": tui.Row("master", "a@example.com", "~1%", "-", False),
        "shared": tui.Row("shared", "b@example.com", "~2%", "-", True),
    }
    _cache_usage(env, "master", 38)
    rows = {r.label: r for r in tui.build_view(env, carry=stale_screen).rows}
    assert rows["master"].used == "38%" and rows["master"].stale is False
    # shared 는 디스크에 없으므로 이어받는다.
    assert rows["shared"].used == "~2%" and rows["shared"].known is True


def test_an_unknown_row_is_not_carried_forward_as_if_known(env) -> None:
    """물음표를 이어받아 "안다" 고 표시하면 이번에는 반대로 거짓말이 된다."""
    unknown_screen = {"master": tui.Row("master", "a@example.com", "?", "-", False, known=False)}
    row = next(r for r in tui.build_view(env, carry=unknown_screen).rows if r.label == "master")
    assert (row.used, row.known) == ("?", False)


def test_auto_probe_skips_rows_that_are_merely_stale(env) -> None:
    """낡은 값은 **아는** 값이다. 자동 조회 대상에 들어가면 화면을 열 때마다 슬롯
    수만큼 프로브가 돈다 — 아끼려던 비용을 그대로 되돌린다."""
    _cache_usage(env, "master", 38, age=env.cache_ttl + 10)
    view = tui.build_view(env)
    assert tui.auto_probe_targets(view) == ("shared",)


def test_nothing_known_anywhere_still_says_so(env) -> None:
    """한 번도 못 읽은 슬롯은 정직하게 물음표다. 지어내지 않는다."""
    view = tui.build_view(env)
    assert [r.used for r in view.rows] == ["?", "?"]
    assert tui.auto_probe_targets(view) == ("master", "shared")


def test_a_slot_that_failed_once_is_not_probed_again_automatically(env) -> None:
    """실패한 슬롯이 화면을 인질로 잡으면 안 된다.

    프로브가 실패해도 `known` 은 False 로 남는다. 억제가 없으면 화면을 열 때마다·
    enter 를 누를 때마다 그 슬롯을 다시 조회하고, 그 동안 키 입력이 처리되지 않아
    `q` 로 나가지도 못한다. 다시 읽는 것은 `r` 이 있고 그건 사용자가 시킨 일이다.
    """
    view = tui.build_view(env)
    assert tui.auto_probe_targets(view, ()) == ("master", "shared")
    assert tui.auto_probe_targets(view, {"master"}) == ("shared",)
    assert tui.auto_probe_targets(view, {"master", "shared"}) == ()


def test_carry_is_dropped_when_the_slot_now_holds_a_different_account(env) -> None:
    """같은 라벨에 다른 계정이 들어오면 옛 사용량을 이어받지 않는다.

    라벨은 슬롯 이름일 뿐이라 지웠다가 다른 계정으로 다시 등록할 수 있다. 그때 이어받으면
    **새 이메일 옆에 옛 계정의 숫자**가 붙는다. `~` 는 값이 낡았다는 뜻이지 다른 사람
    것이라는 뜻이 아니라, 그 화면은 낡은 것보다 나쁘다 — 틀린 것을 맞다고 말한다.
    """
    before = {
        "master": tui.Row("master", "a@example.com", "38%", "-", True),
        "shared": tui.Row("shared", "b@example.com", "70%", "-", False),
    }
    # master 슬롯을 다른 계정으로 갈아 끼운다.
    _write_auth(store.slot_auth(env, "master"), "someone-else@example.com")
    rows = {r.label: r for r in tui.build_view(env, carry=before).rows}
    assert rows["master"].email == "someone-else@example.com"
    assert (rows["master"].used, rows["master"].known) == ("?", False)
    # 그대로인 슬롯은 계속 이어받는다.
    assert rows["shared"].used == "~70%"


def test_pressing_enter_on_the_active_row_keeps_the_numbers(env) -> None:
    """전환 직후 같은 행에서 enter 를 한 번 더 누르는 것은 흔한 조작이다.

    아무것도 하지 않는 분기인데 화면이 방금 지켜 낸 숫자를 물음표로 되돌리면, 사용자는
    자기가 무언가 망가뜨렸다고 읽는다.
    """
    _cache_usage(env, "master", 38)
    _cache_usage(env, "shared", 70)
    after = tui.do_switch(tui.replace(tui.build_view(env), cursor=1))  # shared 로 전환
    assert [r.used for r in after.rows] == ["~38%", "~70%"]
    again = tui.do_switch(after)  # 커서가 shared 에 있고 shared 가 활성이다
    assert "is already active" in again.message
    assert [r.used for r in again.rows] == ["~38%", "~70%"]


def test_auto_refresh_probes_only_the_requested_slots(env, monkeypatch) -> None:
    """자동 조회는 **모르는 슬롯만** 읽는다. 전체 조회(`r`)와 갈리는 지점이다."""
    from codex_swap.core.types import ProbeResult, Usage

    seen: list[str] = []

    def fake_probe(codex_bin: str, home: str | None = None, **kw):
        seen.append(Path(home).name)
        return ProbeResult.of(Usage(used_percent=11))

    monkeypatch.setattr(tui.probe, "probe", fake_probe)
    monkeypatch.setattr(tui, "resolve_codex_bin", lambda: "/bin/true")

    _cache_usage(env, "master", 38)
    view = tui.build_view(env)
    assert tui.auto_probe_targets(view) == ("shared",)

    after = tui.do_refresh(view, ("shared",))
    assert seen == ["shared"], "모르는 슬롯만 읽어야 한다"
    rows = {r.label: r for r in after.rows}
    assert (rows["master"].used, rows["shared"].used) == ("38%", "11%")
    assert tui.auto_probe_targets(after) == ()


def test_a_failed_auto_refresh_says_what_is_still_empty(env, monkeypatch) -> None:
    """조용히 실패하면 사용자는 아무 일도 없었다고 읽는다. 무엇이 비었는지 말한다."""
    monkeypatch.setattr(tui.probe, "probe", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    monkeypatch.setattr(tui, "resolve_codex_bin", lambda: "/bin/true")
    view = tui.build_view(env)
    after = tui.do_refresh(view, ("master", "shared"))
    assert "master" in after.message and "shared" in after.message
    assert "r to retry" in after.message
    assert [r.used for r in after.rows] == ["?", "?"]


def test_the_stale_marker_is_explained_only_when_something_is_stale(env) -> None:
    """늘 떠 있는 안내는 곧 안 읽힌다. 낡은 행이 있을 때만 범례를 낸다."""
    _cache_usage(env, "master", 38)
    _cache_usage(env, "shared", 70)
    assert "stale" not in text(tui.build_view(env))

    _cache_usage(env, "master", 38, age=env.cache_ttl + 10)
    body = text(tui.build_view(env))
    assert "~38%" in body and "~ marks a stale cached value" in body


# ── 배경 조회 ────────────────────────────────────────────────────────────────
#
# 동기로 돌렸더니 화면을 여는 데 5.3 초가 걸렸고(실측, 슬롯 2 개) 그 동안 `q` 조차 먹지
# 않았다. 끝난 뒤 `flushinp()` 가 그 사이 눌린 키까지 버려서 두 번 눌러야 나갈 수 있었다.
# 프로브 타임아웃이 **요청당** 20 초라 최악은 슬롯당 1 분에 가깝다.


def test_the_prober_hands_back_a_message_and_then_goes_idle(env, monkeypatch) -> None:
    monkeypatch.setattr(tui, "_refresh_message", lambda s, labels: f"읽었다: {','.join(labels)}")
    prober = tui._Prober()
    assert prober.labels == ()
    assert prober.start(env, ["master", "shared"]) is True
    for _ in range(200):  # 스레드가 끝날 때까지
        message = prober.take()
        if message is not None:
            break
        time.sleep(0.01)
    assert message == "읽었다: master,shared"
    # 끝났으면 다시 놀아야 한다. 안 그러면 다음 조회가 영영 안 뜬다.
    assert prober.labels == ()
    assert prober.take() is None
    assert prober.start(env, ["master"]) is True


def test_the_prober_does_not_stack_two_runs(env, monkeypatch) -> None:
    """같은 슬롯을 두 번 읽지 않는다. 놓친 대상은 다음 틱에 다시 집힌다."""
    release = threading.Event()
    monkeypatch.setattr(tui, "_refresh_message", lambda s, labels: release.wait(5) and "done")
    prober = tui._Prober()
    assert prober.start(env, ["master"]) is True
    assert prober.start(env, ["shared"]) is False
    assert prober.labels == ("master",)
    release.set()


def test_the_prober_never_leaves_the_screen_stuck_on_probing(env, monkeypatch) -> None:
    """스레드에서 나가는 예외는 아무도 못 본다. 큐가 비면 화면이 '조회 중' 에 굳는다."""

    def boom(settings, labels):
        raise RuntimeError("터졌다")

    monkeypatch.setattr(tui, "_refresh_message", boom)
    prober = tui._Prober()
    prober.start(env, ["master"])
    for _ in range(200):
        message = prober.take()
        if message is not None:
            break
        time.sleep(0.01)
    assert message is not None and "터졌다" in message
    assert prober.labels == ()


def test_nothing_to_probe_is_not_a_run(env) -> None:
    assert tui._Prober().start(env, []) is False


def test_the_probing_note_does_not_bury_an_existing_message(env) -> None:
    """실패 통지 위에 '조회 중' 을 덮으면 사용자가 그것을 못 본다."""
    view = tui.build_view(env, message="전환 실패: 무언가")
    noted = tui.probing_note(view, ("master",))
    assert "전환 실패: 무언가" in noted.message and "master" in noted.message
    assert tui.probing_note(view, ()) is view


def test_refresh_message_names_what_it_could_not_read(env, monkeypatch) -> None:
    monkeypatch.setattr(tui, "resolve_codex_bin", lambda: "/bin/true")
    monkeypatch.setattr(tui.probe, "probe", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    msg = tui._refresh_message(env, ("master", "shared"))
    assert "master" in msg and "shared" in msg and "r to retry" in msg


def test_refresh_message_reports_a_missing_codex_instead_of_raising(env, monkeypatch) -> None:
    """이 함수는 스레드 안에서 돈다. 예외를 올리면 화면이 아무 말도 못 듣는다."""

    def missing():
        raise RuntimeError("없다")

    monkeypatch.setattr(tui, "resolve_codex_bin", missing)
    assert "Could not find codex" in tui._refresh_message(env, ("master",))


# ── 사용량 바와 사다리 축 ────────────────────────────────────────────────────


def test_render_lines_is_exactly_the_text_of_render_screen(env) -> None:
    """둘을 따로 만들면 줄 수가 어긋난다. 껍질이라는 계약을 못박는다."""
    view = tui.build_view(env)
    assert tui.render_lines(view, width=120) == [t for t, _ in tui.render_screen(view, width=120)]


def test_every_ladder_rung_lands_in_its_own_bar_cell(env) -> None:
    """바를 좁히면 85 와 95 가 같은 칸으로 뭉쳐 관문 표시가 뜻을 잃는다."""
    cells = {rung: tui._bar_cell(rung) for rung in env.ladder}
    assert len(set(cells.values())) == len(env.ladder), cells


@pytest.mark.parametrize(
    ("percent", "filled"), [(0, 0), (50, 12), (70, 17), (100, tui.BAR_COLS), (96, 23)]
)
def test_the_bar_fills_proportionally(percent: int, filled: int) -> None:
    bar = tui.usage_bar(percent, None)
    assert len(bar) == tui.BAR_COLS
    assert bar.count("█") == filled


def test_an_unknown_usage_draws_no_bar() -> None:
    """모르는 값을 0% 로 그리면 '가장 덜 쓴 계정' 으로 보인다 — 정반대의 오해다."""
    bar = tui.usage_bar(None, 70)
    assert tui.BAR_FILL not in bar and tui.BAR_EMPTY not in bar
    assert bar == tui.BAR_UNKNOWN * tui.BAR_COLS


def test_an_unknown_bar_keeps_the_width_class_of_every_other_bar() -> None:
    """공백으로 두면 이 행만 뒤쪽 열이 통째로 어긋난다.

    공백은 EAW `Na`, 바 글자는 전부 `A` 다. Ambiguous 를 두 칸으로 그리는 터미널에서
    바 자리가 이 행만 24 칸이고 다른 행은 48 칸이 되어, 뒤따르는 쿠폰·리셋 열이 24 칸
    왼쪽으로 밀린다. 모듈이 바로 그 불변식을 문서로 적어 두었는데 미지 행만 빠져 있었다.
    """
    known = {unicodedata.east_asian_width(ch) for ch in tui.usage_bar(50, 70)}
    unknown = {unicodedata.east_asian_width(ch) for ch in tui.usage_bar(None, 70)}
    assert len(unknown) == 1 and unknown == known, (unknown, known)


def test_the_bar_marks_only_the_current_rung() -> None:
    """관문 넷을 다 찍으면 `░┆░░┃░░┆░░┆` 처럼 잡음이 된다. 전체는 아래 축이 맡는다."""
    bar = tui.usage_bar(30, 70)
    assert bar.count("┆") == 1 and "╪" not in bar
    # 채워진 자리를 지나면 눈금이 채움 위에 얹힌다 — 칸을 잃지 않는다.
    passed = tui.usage_bar(90, 70)
    assert passed.count("╪") == 1 and "┆" not in passed
    assert len(passed) == tui.BAR_COLS


def test_the_axis_tick_sits_in_the_same_cell_as_the_bar_tick(env) -> None:
    """어긋나면 사용자가 관문을 실제와 다른 위치로 읽는다."""
    for rung in env.ladder:
        bar = tui.usage_bar(rung, rung)
        axis, _ = tui.ladder_axis(env.ladder, rung)
        assert bar.index("╪") == axis.index("┻"), rung


def test_the_axis_separates_the_current_rung_from_the_rest(env) -> None:
    axis, labels = tui.ladder_axis(env.ladder, 70)
    assert axis.count("┻") == 1 and axis.count("┴") == len(env.ladder) - 1
    assert "50" in labels and "70" in labels and "95" in labels


@pytest.mark.parametrize(
    ("percent", "rung", "tone"),
    [
        (10, 70, "ok"),
        (49, 70, "ok"),
        # 첫 칸(50)은 넘었지만 **현재 관문**(70)은 아직이다. 여기가 비어 있으면 관문을
        # `ladder[0]` 로 고정하는 실수가 테스트를 통과한다 — 실제로 통과했다.
        (50, 70, "ok"),
        (60, 70, "ok"),
        (69, 70, "ok"),
        (70, 70, "warn"),
        (94, 70, "warn"),
        (95, 70, "danger"),  # 사다리 끝 — 더 올라갈 칸이 없다
        (None, 70, "dim"),
    ],
)
def test_the_row_colour_follows_the_ladder_not_arbitrary_bands(
    env, percent: int | None, rung: int, tone: str
) -> None:
    """50/80/90 같은 관습 구간을 쓰면 색이 이 도구의 판단과 무관한 말을 한다."""
    row = tui.Row("a", "a@x", "-", "-", False, percent=percent)
    view = tui.View(rows=(row,), cursor=0, settings=env, current_rung=rung)
    assert tui._row_tone(row, view) == tone


def test_the_active_row_bolds_only_its_label_never_the_bar(env) -> None:
    """행 전체에 bold 를 걸면 **같은 문자열인 바가 활성 행에서만 길어 보인다.**

    이 터미널의 bold 글자가 더 굵고 넓게 그려져서다. 두 행의 문자열·표시 폭·모든 열
    위치가 완전히 동일한데도 "아래 바가 더 짧다" 로 읽혔다 — 실측으로 확인했다.
    강조는 라벨 구간에만 얹는다. 거기는 글자라 굵어져도 뜻이 왜곡되지 않는다.
    """
    rows = (
        tui.Row("master", "a@x", "70%", "-", True, percent=70),
        tui.Row("shared", "b@x", "70%", "-", False, percent=70),
    )
    view = tui.View(rows=rows, cursor=0, settings=env, current_rung=70)
    screen = tui.render_screen(view, width=140)
    active = next(st for text, st in screen if text.startswith(" >*"))
    idle = next(st for text, st in screen if text.lstrip().startswith("shared"))

    assert active.bold is False, "행 전체에 bold 를 걸면 바가 밀린다"
    assert active.spans and all(sp[2].bold for sp in active.spans)
    assert not idle.spans

    # 강조 구간이 라벨을 벗어나 바까지 덮지 않는다.
    text = next(t for t, _ in screen if t.startswith(" >*"))
    bar_at = text.index(tui.BAR_FILL)
    assert all(end <= bar_at for _, end, _ in active.spans), (active.spans, bar_at)
    covered = "".join(text[start:end] for start, end, _ in active.spans)
    assert covered.strip() == ">*master", covered


def test_the_active_label_keeps_the_row_colour(env) -> None:
    """소진된 계정이 활성일 때 하필 라벨에서만 경고색이 빠지면 안 된다.

    구간 덧칠은 그 자리를 **다시 그린다.** 구간의 tone 을 비워 두면 행의 색이 거기서만
    기본색으로 되돌아간다 — 눈이 가장 먼저 가는 라벨이 그 행에서 유일하게 색이 없는
    글자가 된다.
    """
    rows = (tui.Row("master", "a@x", "99%", "-", True, percent=99),)
    view = tui.View(rows=rows, cursor=0, settings=env, current_rung=95)
    style = next(st for text, st in tui.render_screen(view, width=140) if text.startswith(" >*"))
    assert style.tone == "danger"
    label_span = next(sp for sp in style.spans if sp[2].bold and sp[2].tone != "accent")
    assert label_span[2].tone == "danger", style.spans


def test_the_cursor_glyph_is_the_only_thing_accented_on_a_row(env) -> None:
    """enter 는 **커서 행**의 자격증명을 바꾼다. 그 표시가 안 보이면 확신할 수 없다."""
    rows = (
        tui.Row("master", "a@x", "70%", "-", True, percent=70),
        tui.Row("shared", "b@x", "40%", "-", False, percent=40),
    )
    view = tui.View(rows=rows, cursor=1, settings=env, current_rung=70)
    screen = tui.render_screen(view, width=140)

    text, style = next((t, s) for t, s in screen if "shared" in t)
    accents = [sp for sp in style.spans if sp[2].tone == "accent"]
    assert len(accents) == 1, style.spans
    assert text[accents[0][0] : accents[0][1]] == ">"

    # 커서가 없는 행에는 강조가 붙지 않는다 — 붙으면 커서가 둘로 보인다.
    other = next(s for t, s in screen if "master" in t)
    assert not any(sp[2].tone == "accent" for sp in other.spans), other.spans


def test_the_chrome_is_dim(env) -> None:
    rows = (tui.Row("a", "a@x", "70%", "-", True, percent=70),)
    view = tui.View(rows=rows, cursor=0, settings=env, current_rung=70)
    assert any(st.tone == "dim" for _, st in tui.render_screen(view, width=120))


# ── 폭 적응 ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("width", [None, 20, 40, 60, 80, 90, 95, 100, 120, 200])
def test_no_line_ever_exceeds_the_terminal_width(env, width: int) -> None:
    """표와 크롬은 어느 폭에서도 넘치지 않는다.

    넘치는 줄은 그리기 단계가 잘라내는데, 잘린 열은 화면이 고장 난 것처럼 보인다.
    **메시지·경고는 예외다** — 임의의 문장(오류 문구·이메일 주소)이라 어떤 폭에서도
    들어간다고 약속할 수 없다. 그건 자르는 편이 아예 안 보이는 것보다 낫다.
    """
    rows = tuple(
        tui.Row(
            f"label{i}",
            f"someone.long{i}@example.com",
            "~100%",
            "09-13 02:00 (6일 뒤)",
            i == 0,
            percent=100,
        )
        for i in range(3)
    )
    view = tui.View(rows=rows, cursor=0, settings=env, current_rung=95)
    limit = 200 if width is None else width
    assert limit >= tui.MIN_FIT_WIDTH, "계약이 성립하는 범위 밖을 테스트하고 있다"
    for text, _ in tui.render_screen(view, width=width):
        if text.lstrip().startswith("Warning:") or (view.message and view.message in text):
            continue
        assert tui._width(text) <= limit, (width, text)


def test_a_narrow_terminal_drops_the_bar_but_keeps_the_ladder(env) -> None:
    """축이 빠지면 사다리가 화면 어디에도 안 남는다. 그때는 머리말이 대신 든다.

    **관문도 함께 든다.** 사다리만 적던 때는 넓은 화면이 두 번(머리말·축) 알려 주던
    "지금 넘어야 하는 칸" 이 좁은 화면에서 0 번이 됐다.
    """
    rows = (tui.Row("a", "a@x", "70%", "-", True, percent=70),)
    view = tui.View(rows=rows, cursor=0, settings=env, current_rung=70)

    wide = tui.render_lines(view, width=tui._BAR_MIN_WIDTH)
    narrow = tui.render_lines(view, width=tui._BAR_MIN_WIDTH - 1)
    assert any("█" in line for line in wide) and any("┻" in line for line in wide)
    assert not any("█" in line for line in narrow)
    assert "gate 70%" in wide[0]
    assert "50,70,85,95" in narrow[0], "좁아지면 사다리가 화면에서 사라진다"
    assert "gate 70%" in narrow[0], "좁아지면 어느 눈금이 관문인지 사라진다"


def test_a_truncated_email_says_that_it_is_truncated(env) -> None:
    """`account.name@gmail.co` 가 실제 주소인지 잘린 것인지 구별되어야 한다."""
    rows = (tui.Row("a", "a-very-long-address@example.com", "70%", "-", True, percent=70),)
    view = tui.View(rows=rows, cursor=0, settings=env, current_rung=70)
    body = next(line for line in tui.render_lines(view, width=tui._BAR_MIN_WIDTH) if "…" in line)
    assert "…" in body


# ── 머리말이 "왜 안 바뀌는가" 에 답한다 ──────────────────────────────────────


def test_the_headline_shows_the_gate_that_actually_blocks(env) -> None:
    rows = (tui.Row("a", "a@x", "70%", "-", True, percent=70),)
    view = tui.View(rows=rows, cursor=0, settings=env, current_rung=85, cooldown_left=735)
    head = tui.render_lines(view, width=120)[0]
    assert "gate 85%" in head
    assert "cooldown 12m left" in head


def test_the_headline_omits_a_cooldown_that_is_not_running(env) -> None:
    rows = (tui.Row("a", "a@x", "70%", "-", True, percent=70),)
    view = tui.View(rows=rows, cursor=0, settings=env, current_rung=85)
    assert "cooldown" not in tui.render_lines(view, width=120)[0]


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(45, "45s"), (60, "1m"), (735, "12m"), (3600, "1h 0m"), (5430, "1h 30m")],
)
def test_durations_are_read_by_people_not_stopwatches(seconds: int, text: str) -> None:
    assert tui._duration(seconds) == text


def test_the_current_rung_is_measured_from_the_lightest_account(env) -> None:
    """활성 기준으로 잡으면 앞선 쪽만 계속 올라가 번갈아 밟기가 성립하지 않는다."""
    rows = [
        tui.Row("heavy", "h@x", "90%", "-", True, percent=90),
        tui.Row("light", "l@x", "40%", "-", False, percent=40),
    ]
    assert tui.current_rung(env, rows) == (50, False)  # 가장 덜 쓴 40 바로 위 칸
    assert tui.current_rung(env, []) == (None, False)
    unknown = [tui.Row("x", "x@x", "?", "-", False, known=False)]
    assert tui.current_rung(env, unknown) == (None, False)


def test_cooldown_left_counts_down_and_then_disappears(env, monkeypatch) -> None:
    from codex_swap.core import paths

    stamp = paths.rotate_stamp_path(env)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.touch()
    os.utime(stamp, (1_000_000, 1_000_000))
    monkeypatch.setattr(tui.time, "time", lambda: 1_000_000 + 100)
    assert tui.cooldown_left(env) == env.cooldown - 100
    monkeypatch.setattr(tui.time, "time", lambda: 1_000_000 + env.cooldown + 1)
    assert tui.cooldown_left(env) is None


def test_cooldown_left_is_none_without_a_stamp(env) -> None:
    assert tui.cooldown_left(env) is None


@pytest.mark.parametrize(("percent", "glyph"), [(69, "┆"), (70, "╪"), (71, "╪")])
def test_the_tick_crosses_exactly_where_the_policy_switches(percent: int, glyph: str) -> None:
    """정책은 `active_pct >= rung` 에서 전환한다. 화면이 다른 말을 하면 안 된다.

    셀 인덱스로 판정하면 정확히 관문 위(70% · 관문 70)에서 `filled == tick` 이라
    "아직 안 넘음" 으로 그려진다 — 정작 그 순간이 전환이 일어나는 지점이다.
    """
    assert glyph in tui.usage_bar(percent, 70)


def test_columns_are_dropped_in_priority_order(env) -> None:
    """버리는 순서가 우선순위다 — 바 → 리셋 시각 → 이메일 폭 → 라벨 폭.

    임계는 **상수에서 파생**시킨다. 숫자를 박아 두면 간격 한 칸을 바꿀 때마다 테스트가
    같이 틀어지고, 그러면 테스트가 규칙이 아니라 그때의 숫자를 지키게 된다.
    """
    # 임계는 **최소 폭 기준**으로 판다 — `_BAR_MIN_WIDTH` 도 그렇게 파생된다.
    reset_min = tui._overhead(with_bar=False, with_reset=True) + tui._LABEL_MIN + tui._EMAIL_MIN
    cases = [
        (tui._BAR_MIN_WIDTH + 100, True, True),
        (tui._BAR_MIN_WIDTH, True, True),
        (tui._BAR_MIN_WIDTH - 1, False, True),
        (reset_min, False, True),
        (reset_min - 1, False, False),
    ]
    for width, bar, reset in cases:
        with_bar, with_reset, label_cols, email_cols = tui._layout(
            width, tui._LABEL_MIN, tui._EMAIL_MIN
        )
        assert (with_bar, with_reset) == (bar, reset), width
        assert label_cols > 0 and email_cols > 0, width


def test_a_very_narrow_screen_drops_columns_instead_of_half_clipping_them(env) -> None:
    """넘치는 줄을 그리기 단계의 클립에 맡기면 마지막 열이 반쯤 잘려 고장 나 보인다."""
    rows = (tui.Row("a", "someone@example.com", "70%", "09-13 02:00 (6일 뒤)", True, percent=70),)
    view = tui.View(rows=rows, cursor=0, settings=env, current_rung=70)
    body = next(line for line in tui.render_lines(view, width=64) if line.startswith(" >"))
    assert "09-13" not in body, "리셋 열이 남아 넘쳤다"
    assert "someone" in body, "계정을 알아볼 수 없게 잘렸다"


# ── 리뷰가 잡은 것들 ─────────────────────────────────────────────────────────


def test_the_gate_ignores_stale_numbers(env) -> None:
    """화면과 정책이 다른 관문을 말하면 사용자는 화면을 믿는다.

    정책이 보는 것은 TTL 안의 값 아니면 방금 돌린 프로브뿐이다. 낡은 숫자를 섞으면
    갈린다 — 활성 90% · 인증 실패 후보의 낡은 10% · 정상 후보 80% 에서 화면은 50%,
    정책은 85% 를 관문으로 잡는다.
    """
    rows = [
        tui.Row("active", "a@x", "90%", "-", True, percent=90),
        tui.Row("dead", "d@x", "~10%", "-", False, percent=10, stale=True),
        tui.Row("ok", "o@x", "80%", "-", False, percent=80),
    ]
    assert tui.current_rung(env, rows) == (85, False)  # min(90, 80) 바로 위 칸
    # 전부 낡았으면(전환 직후가 그렇다) 추정하되 잠정이라고 말한다. 감추면 대부분의
    # 시간에 화면에서 관문이 사라진다.
    assert tui.current_rung(env, [rows[1]]) == (50, True)
    assert tui.current_rung(env, []) == (None, False)


def test_the_cooldown_countdown_actually_counts_down(env, monkeypatch) -> None:
    """입력 없이 기다리는 동안 루프는 같은 View 를 다시 그린다. 그냥 두면 숫자가 언다."""
    from codex_swap.core import paths

    stamp = paths.rotate_stamp_path(env)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.touch()
    os.utime(stamp, (1_000_000, 1_000_000))
    monkeypatch.setattr(tui.time, "time", lambda: 1_000_000 + 10)
    view = tui.build_view(env)
    assert view.cooldown_left == env.cooldown - 10

    monkeypatch.setattr(tui.time, "time", lambda: 1_000_000 + 500)
    assert tui.refresh_clock(view).cooldown_left == env.cooldown - 500
    monkeypatch.setattr(tui.time, "time", lambda: 1_000_000 + env.cooldown + 1)
    assert tui.refresh_clock(view).cooldown_left is None


def test_a_future_stamp_cannot_report_more_than_the_configured_cooldown(env, monkeypatch) -> None:
    """설정은 15 분인데 화면이 "3시간 남음" 이면 사용자는 설정 쪽을 의심한다."""
    from codex_swap.core import paths

    stamp = paths.rotate_stamp_path(env)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.touch()
    os.utime(stamp, (2_000_000, 2_000_000))  # 미래
    monkeypatch.setattr(tui.time, "time", lambda: 1_000_000)
    assert tui.cooldown_left(env) == env.cooldown


@pytest.mark.parametrize("rung", [0, 1, 50, 97, 98, 99, 100])
def test_the_tick_survives_every_rung_including_the_ends(rung: int) -> None:
    """98% 이상은 채움이 24(전부)인데 칸 번호로는 24 가 없다. 눈금이 통째로 사라졌었다."""
    bar = tui.usage_bar(50, rung)
    assert ("╪" in bar) or ("┆" in bar), rung
    axis, _ = tui.ladder_axis([rung], rung)
    marker = "╪" if "╪" in bar else "┆"
    assert bar.index(marker) == axis.index("┻"), rung


def test_a_crowded_ladder_keeps_the_current_gate_visible() -> None:
    """두 칸이 같은 자리에 떨어지면 하필 지금 필요한 표시가 사라졌다."""
    axis, _ = tui.ladder_axis([1, 2, 3, 4], 1)
    assert "┻" in axis


def test_a_crowded_ladder_does_not_garble_its_numbers() -> None:
    """겹쳐 쓰면 어느 쪽도 못 읽는 문자열이 된다. 못 놓을 숫자는 아예 놓지 않는다."""
    _, labels = tui.ladder_axis([98, 99], 98)
    assert "98" in labels or "99" in labels
    # 끝 칸의 숫자는 왼쪽으로 밀어 넣는다 — 그냥 자르면 `100` 이 `10` 으로 보인다.
    _, edge = tui.ladder_axis([100], 100)
    assert "100" in edge


def test_an_empty_ladder_draws_an_empty_axis() -> None:
    axis, labels = tui.ladder_axis([], None)
    assert axis.strip() == "" and labels.strip() == ""


def test_a_provisional_gate_says_so_instead_of_disappearing(env) -> None:
    """전환 직후에는 캐시가 비어 모든 값이 낡는다. 그때마다 관문이 사라지면 안 된다.

    감추는 편이 정직해 보이지만, 그건 **대부분의 시간에** 화면에서 관문이 없다는 뜻이다.
    틀릴 수 있다고 표시하며 보여 주는 편이 낫다 — 사용량과 같은 `~` 를 쓴다.
    """
    stale_only = (tui.Row("a", "a@x", "~40%", "-", True, percent=40, stale=True),)
    view = tui.View(rows=stale_only, cursor=0, settings=env, current_rung=50, rung_provisional=True)
    assert "gate ~50%" in tui.render_lines(view, width=120)[0]

    fresh = (tui.Row("a", "a@x", "40%", "-", True, percent=40),)
    solid = tui.View(rows=fresh, cursor=0, settings=env, current_rung=50)
    assert "gate 50%" in tui.render_lines(solid, width=120)[0]


# ── 바 글자의 폭 클래스 (눈으로 안 보이는 불변식) ───────────────────────────


@pytest.mark.parametrize("percent", [0, 1, 30, 50, 58, 70, 96, 100])
def test_a_bar_never_mixes_east_asian_width_classes(percent: int) -> None:
    """섞이면 **채움 비율에 따라 바의 실제 폭이 달라진다.**

    원래 채움이 `█`(Ambiguous)이고 빈 칸이 `░`(Neutral)였다. Ambiguous 를 두 칸으로 그리는
    터미널에서는 채움 개수가 곧 두 칸 글자의 개수라, 같은 24 글자 바가 58% 에서 39 칸,
    70% 에서 42 칸으로 그려졌다 — 사용자 눈에는 "아래 행의 바가 더 짧다" 로 보인다.

    이 파일은 이미 같은 이유로 조작법을 ASCII 로 적어 두었는데(`↑↓` 는 Ambiguous), 바를
    만들 때 그 교훈을 적용하지 않았다. 눈으로는 확인할 수 없으므로 테스트가 유일한 방어선이다.
    """
    for rung in (None, 0, 50, 70, 100):
        bar = tui.usage_bar(percent, rung)
        classes = {unicodedata.east_asian_width(ch) for ch in bar}
        assert len(classes) == 1, (percent, rung, classes, bar)


def test_the_axis_shares_that_width_class_too(env) -> None:
    """축이 바와 다른 클래스면 눈금과 숫자가 터미널에서 어긋난다."""
    axis, _ = tui.ladder_axis(env.ladder, 70)
    bar_classes = {unicodedata.east_asian_width(ch) for ch in tui.usage_bar(50, 70)}
    axis_classes = {unicodedata.east_asian_width(ch) for ch in axis if ch != " "}
    assert axis_classes == bar_classes, (axis_classes, bar_classes)


def test_the_glyph_constants_are_one_class() -> None:
    glyphs = (
        tui.BAR_FILL,
        tui.BAR_EMPTY,
        tui.BAR_TICK,
        tui.BAR_TICK_PASSED,
        tui.AXIS_TICK,
        tui.AXIS_TICK_CURRENT,
    )
    assert len({unicodedata.east_asian_width(g) for g in glyphs}) == 1


# ── 관문 숫자 정렬 ──────────────────────────────────────────────────────────


def test_the_gate_number_sits_under_its_tick_not_left_of_it(env) -> None:
    """`i - len//2` 로 두면 두 글자 숫자가 눈금보다 한 칸 왼쪽으로 치우쳐 보인다."""
    axis, labels = tui.ladder_axis(env.ladder, 70)
    for step in env.ladder:
        tick = axis.index(tui.AXIS_TICK_CURRENT) if step == 70 else None
        cell = tui._tick_cell(step)
        assert labels[cell] == str(step)[0], (step, cell, labels)
        if tick is not None:
            assert tick == cell


def test_the_last_gate_number_still_fits(env) -> None:
    """마지막 칸은 바 끝이라, 숫자 줄을 넓히지 않으면 `85`·`95` 가 `8595` 로 붙는다."""
    _, labels = tui.ladder_axis(env.ladder, 70)
    assert "8595" not in labels
    for step in env.ladder:
        assert str(step) in labels, (step, labels)


def test_columns_are_separated_by_the_same_gutter(env) -> None:
    """한 칸이면 `USED`·바·`RESET` 이 서로 붙어 읽힌다."""
    rows = (tui.Row("a", "a@x", "70%", "09-13 02:00", True, percent=70, credits=1),)
    view = tui.View(rows=rows, cursor=0, settings=env, current_rung=70)
    body = next(line for line in tui.render_lines(view, width=140) if line.startswith(" >"))
    bar = tui.usage_bar(70, 70)
    assert f"{tui._GUTTER}{bar}{tui._GUTTER}" in body, body
    # 사용량 칸은 `~100%` 상한에 맞춘 5 칸이라, `70%` 뒤의 남는 자리는 두 칸이다.
    # 그 뒤에 간격이 붙는다. 칸을 넓게 잡으면 이 열만 멀어 보인다.
    assert f"70%  {tui._GUTTER}{bar}" in body, f"사용량 열과 다음 열 사이 간격이 다르다: {body!r}"


def test_the_used_column_does_not_leave_dead_space(env) -> None:
    """자릿수 정렬(오른쪽 붙임)을 **되돌린 자리**다.

    한때 이 열만 오른쪽으로 붙였다. `58%` 와 `~70%` 의 `%` 가 세로로 맞는 이점이 있었지만,
    표에서 한 열만 반대 방향이라 어긋나 보였다. 계정이 두셋뿐인 화면에서 자릿수 정렬의
    이득은 작고 그 인상은 매번 치른다.

    대신 칸을 값의 상한(`~100%` = 5)에 맞춰 좁혔다. 왼쪽 정렬로도 죽은 공백이 남지
    않으므로, 오른쪽 붙임이 풀려던 문제가 애초에 생기지 않는다.
    """
    rows = (
        tui.Row("a", "a@x", "58%", "-", True, percent=58),
        tui.Row("b", "b@x", "~100%", "-", False, percent=100, stale=True),
    )
    view = tui.View(rows=rows, cursor=0, settings=env, current_rung=70)
    body = [ln for ln in tui.render_lines(view, width=140) if "@x" in ln]
    # 가장 긴 값이 칸을 꽉 채운다 = 칸이 그보다 넓지 않다.
    assert "~100%" + tui._GUTTER in body[1], body[1]


def test_the_coupon_count_is_shown_and_carried(env) -> None:
    """소진된 계정에 쿠폰이 남아 있으면 전환하는 대신 그것을 쓰는 선택지가 있다."""
    _cache_usage(env, "master", 95)
    from codex_swap.core import cache

    cache.write(env, "shared", {"usedPercent": 40, "resetsAt": None, "resetCredits": 2})
    rows = {r.label: r for r in tui.build_view(env).rows}
    assert rows["shared"].credits == 2
    assert rows["master"].credits is None  # 이 항목엔 쿠폰 값이 없다
    body = [line for line in tui.render_lines(tui.build_view(env), width=140)]
    assert any("  2  " in line for line in body), body

    # 전환으로 캐시가 비어도 직전 값을 이어받는다.
    before = tui.build_view(env)
    after = tui.build_view(env, carry={r.label: r for r in before.rows})
    assert {r.label: r.credits for r in after.rows} == {"master": None, "shared": 2}


def test_an_unknown_coupon_count_shows_a_dash_not_a_zero(env) -> None:
    """0 으로 쓰면 "쿠폰이 없다" 는 없는 사실이 화면에 뜬다."""
    rows = (tui.Row("a", "a@x", "70%", "-", True, percent=70, credits=None),)
    view = tui.View(rows=rows, cursor=0, settings=env, current_rung=70)
    body = next(line for line in tui.render_lines(view, width=140) if line.startswith(" >"))
    assert f"{tui._GUTTER}-  " in body, body


def test_the_bar_is_dropped_before_the_coupon_count(env) -> None:
    """바는 `%` 숫자의 재표현이지만 쿠폰은 화면 어디에도 없는 정보다."""
    narrow = tui._BAR_MIN_WIDTH - 1
    with_bar, with_reset, _, _ = tui._layout(narrow, tui._LABEL_MIN, tui._EMAIL_MIN)
    assert (with_bar, with_reset) == (False, True)


# ── 단축키 강조 ─────────────────────────────────────────────────────────────


def test_only_the_key_glyphs_are_highlighted() -> None:
    """설명까지 강조하면 눈이 어디를 눌러야 하는지 못 찾고 줄 전체를 읽게 된다."""
    text, spans = tui.keys_line(tui.ACCOUNT_KEYS, width=200)
    assert [text[a:b] for a, b, _ in spans] == [key for key, _ in tui.ACCOUNT_KEYS]
    assert all(style == tui._KEY_STYLE for _, _, style in spans)
    # 설명은 구간 밖이다.
    covered = {i for a, b, _ in spans for i in range(a, b)}
    for label in ("move", "switch", "usage"):
        at = text.index(label)
        assert not (covered & set(range(at, at + len(label)))), label


def test_the_keys_survive_every_width_even_when_labels_do_not() -> None:
    """설명은 한 번 익히면 안 보지만 키는 계속 필요하다."""
    wide, _ = tui.keys_line(tui.ACCOUNT_KEYS, width=200)
    assert "move" in wide
    narrow, spans = tui.keys_line(tui.ACCOUNT_KEYS, width=30)
    assert "move" not in narrow, narrow
    assert [narrow[a:b] for a, b, _ in spans] == [key for key, _ in tui.ACCOUNT_KEYS]
    # 마지막 한 칸에 남는 것은 **나가는 키**다. 자리가 아니라 이름으로 고른다 —
    # 순서를 바꿔도 엉뚱한 키가 남지 않아야 한다.
    tiny, tiny_spans = tui.keys_line(tui.ACCOUNT_KEYS, width=4)
    assert tiny.strip() == "q" and [tiny[a:b] for a, b, _ in tiny_spans] == ["q"]


def test_the_span_offsets_are_character_indices_not_columns(env) -> None:
    """한글이 섞인 줄에서 둘은 다르다. 칸 계산은 그리는 곳 한 군데에만 있어야 한다."""
    # `ACCOUNT_KEYS` 의 설명은 이제 전부 ASCII 라 문자 인덱스와 칸이 **우연히** 같다.
    # 그 우연에 기대면 이 계약을 검사할 수 없으므로, 넓은 글자를 가진 설명을 직접
    # 넣어 둘을 갈라 놓는다. 검사 대상은 `keys_line` 의 구간 계산이지 라벨 자체가 아니다.
    wide_keys = (("^v", "이동"), ("enter", "전환"), ("q", "종료"))
    text, spans = tui.keys_line(wide_keys, width=200)
    enter = next((a, b) for a, b, _ in spans if text[a:b] == "enter")
    assert text[enter[0] : enter[1]] == "enter"
    # 앞에 한글 설명이 있으므로 문자 인덱스와 칸이 갈린다 — 그게 이 테스트의 요점이다.
    assert tui._width(text[: enter[0]]) > enter[0]


def test_the_account_screen_carries_the_key_spans(env) -> None:
    rows = (tui.Row("a", "a@x", "70%", "-", True, percent=70),)
    view = tui.View(rows=rows, cursor=0, settings=env, current_rung=70)
    keys = next(st for text, st in tui.render_screen(view, width=140) if "enter open" in text)
    assert keys.spans and keys.tone == "dim"


def test_the_policy_screen_carries_them_too(env) -> None:
    view = tui.replace(tui.build_view(env), mode="policy")
    keys = next(st for text, st in tui.render_screen(view, width=140) if "e type" in text)
    assert keys.spans
    assert "←→ adjust" in next(
        text for text, _ in tui.render_screen(view, width=140) if "e type" in text
    )


# ── 정책 직접 입력 ──────────────────────────────────────────────────────────


def _policy(env, field: str) -> tui.View:
    idx = next(i for i, (key, *_) in enumerate(tui.POLICY_FIELDS) if key == field)
    return tui.replace(tui.build_view(env), mode="policy", policy_cursor=idx)


def test_a_ladder_can_be_typed_in_directly(env) -> None:
    """프리셋 순환만으로는 임의의 사다리에 닿을 수 없었다."""
    after = tui.edit_policy(_policy(env, "ladder"), "33, 66 ,88")
    assert after.settings.ladder == (33, 66, 88)


def test_a_typed_ladder_is_sorted_and_deduplicated(env) -> None:
    """정책은 앞에서부터 훑어 첫 상회 칸을 관문으로 잡는다(`rung_for`).

    순서가 뒤엉킨 사다리는 오류가 아니라 **조용히 엉뚱한 칸**을 고른다.
    """
    after = tui.edit_policy(_policy(env, "ladder"), "88,33,66,33")
    assert after.settings.ladder == (33, 66, 88)


def test_a_typed_value_goes_through_the_same_parser_as_the_env_var(env) -> None:
    """화면이 자기만의 규칙을 만들면 터미널에서는 되는데 화면에서는 거부된다."""
    assert tui.edit_policy(_policy(env, "margin"), " 7 ").settings.margin == 7
    assert config.parse_int(" 7 ") == 7


@pytest.mark.parametrize("bad", ["abc", "5.5", "", "   ", "5%"])
def test_a_bad_value_is_refused_without_changing_anything(env, bad: str) -> None:
    before = _policy(env, "cooldown")
    after = tui.edit_policy(before, bad)
    assert after.settings.cooldown == before.settings.cooldown
    if bad.strip():
        assert "not an integer" in after.message
    else:
        assert after.message == ""  # 빈 입력은 취소다


def test_a_negative_value_is_refused_even_though_the_parser_accepts_it(env) -> None:
    """`config` 는 bash 패리티로 음수를 받지만 이 값들에는 뜻이 없다.

    저장하면 쿨다운이 영원히 안 걸리는 식으로 조용히 이상해진다.
    """
    assert config.parse_int("-5") == -5
    after = tui.edit_policy(_policy(env, "cooldown"), "-5")
    assert after.settings.cooldown == env.cooldown
    assert "cannot be negative" in after.message


@pytest.mark.parametrize("bad", ["abc", "a,b", "-1,-2"])
def test_an_unreadable_ladder_is_refused(env, bad: str) -> None:
    before = _policy(env, "ladder")
    after = tui.edit_policy(before, bad)
    assert after.settings.ladder == before.settings.ladder
    assert "could not read numbers" in after.message


def test_a_typed_ladder_survives_the_preset_ring(env) -> None:
    """직접 넣은 값이 고리에 없으면 화살표 한 번에 영영 돌아올 수 없다."""
    typed = tui.edit_policy(_policy(env, "ladder"), "33,66")
    saved = tui.replace(typed, saved_settings=typed.settings)
    moved = tui.adjust_policy(saved, +1)
    assert moved.settings.ladder != (33, 66)
    back = tui.adjust_policy(moved, -1)
    assert back.settings.ladder == (33, 66)


def test_a_finished_probe_does_not_throw_you_out_of_the_policy_screen(env) -> None:
    """`build_view` 는 `mode` 를 기본값(계정)으로 되돌리고 미저장 편집을 버린다.

    정책 화면에서 값을 고치는 동안 배경 조회가 끝나면 화면이 통째로 튀어나가고 편집이
    날아갔다 — 실제로 그래서 `e` 로 넣은 사다리가 반영되지 않았다.
    """
    editing = tui.edit_policy(
        tui.replace(tui.build_view(env), mode="policy", policy_cursor=0), "33,66,88"
    )
    after = tui.apply_probe_result(editing, "Usage refreshed")
    assert after.mode == "policy", "정책 화면에서 튀어나갔다"
    assert after.settings.ladder == (33, 66, 88), "미저장 편집이 날아갔다"
    assert after.message == "Usage refreshed"


def test_a_finished_probe_does_refresh_the_account_screen(env) -> None:
    """계정 화면에서는 반대다 — 디스크에서 새로 읽어야 숫자가 갱신된다."""
    _cache_usage(env, "master", 38)
    before = tui.build_view(env)
    assert next(r for r in before.rows if r.label == "master").used == "38%"
    _cache_usage(env, "master", 77)
    after = tui.apply_probe_result(tui.replace(before, cursor=1), "읽었다")
    assert next(r for r in after.rows if r.label == "master").used == "77%"
    # 커서는 지킨다. 조회가 끝날 때마다 커서가 튀면 enter 가 엉뚱한 계정을 고른다.
    assert after.rows[after.cursor].label == before.rows[1].label


# ── 축은 바에 붙는다 ────────────────────────────────────────────────────────


def test_the_axis_sits_directly_under_the_bars(env) -> None:
    """빈 줄로 떼어 놓으면 표와 축이 별개의 것처럼 읽힌다.

    앞서는 반대로 판단했다 — 축이 마지막 계정의 한 줄로 오해될까 봐 한 줄 띄웠다.
    실제로 놓고 보니 떨어진 쪽이 더 어색했다. 축은 바로 위 바들의 눈금이고, 붙어 있어야
    그 관계가 보인다.
    """
    rows = (
        tui.Row("a", "a@x", "58%", "-", True, percent=58),
        tui.Row("b", "b@x", "70%", "-", False, percent=70),
    )
    view = tui.View(rows=rows, cursor=0, settings=env, current_rung=70)
    lines = tui.render_lines(view, width=140)
    axis_at = next(i for i, ln in enumerate(lines) if tui.AXIS_TICK_CURRENT in ln)
    assert "b@x" in lines[axis_at - 1], lines[axis_at - 2 : axis_at + 1]


# ── 열 정렬은 한 방향이다 ──────────────────────────────────────────────────


def test_every_column_is_left_aligned(env) -> None:
    """숫자만 오른쪽으로 붙이면 한 열만 반대로 보인다.

    자릿수 정렬이라는 이점이 있지만, 계정이 두셋뿐인 화면에서 그 이득은 작고 어긋나
    보이는 대가는 매번 치른다. 칸을 값에 맞춰 좁히면 죽은 공백도 같이 사라진다.
    """
    rows = (
        tui.Row("a", "a@x", "58%", "-", True, percent=58),
        tui.Row("b", "b@x", "~100%", "-", False, percent=100, stale=True, credits=2),
    )
    view = tui.View(rows=rows, cursor=0, settings=env, current_rung=70)
    lines = tui.render_lines(view, width=140)
    header = next(ln for ln in lines if "USED" in ln)
    body = [ln for ln in lines if ln.startswith((" >", "  ")) and "@x" in ln]
    # 값이 각 칸의 **왼쪽 끝**에서 시작한다 = 머리말과 같은 열에서 시작한다.
    used_at = header.index("USED")
    for ln in body:
        assert ln[used_at] not in " ", f"USED 값이 칸 왼쪽에서 시작하지 않는다: {ln!r}"
    cred_at = header.index("CRED")
    assert body[1][cred_at] == "2", body[1]


# ── 방향키로 들어가는 길 ────────────────────────────────────────────────────
#
# 단축키만으로 화면을 옮기면 그 키를 외운 사람만 쓸 수 있다. 커서를 계정 아래 **메뉴**
# 까지 내려 `enter` 로 들어가는 길을 함께 둔다. 그러면 `enter` 가 하던 "전환" 과 겹치므로
# 전환은 `s` 로 옮긴다.


def _view(env, cursor: int = 0) -> tui.View:
    rows = (
        tui.Row("a", "a@x", "58%", "-", True, percent=58),
        tui.Row("b", "b@x", "70%", "-", False, percent=70),
    )
    return tui.View(rows=rows, cursor=cursor, settings=env, current_rung=70)


def test_the_cursor_runs_past_the_accounts_into_a_menu(env) -> None:
    view = _view(env)
    assert tui.cursor_limit(view) == len(view.rows) + len(tui.MENU) - 1
    # 계정 구간
    assert tui.selected_row(_view(env, 0)).label == "a"
    assert tui.selected_menu(_view(env, 0)) is None
    # 메뉴 구간
    assert tui.selected_row(_view(env, len(view.rows))) is None
    assert tui.selected_menu(_view(env, len(view.rows))) == tui.MENU[0][0]


def test_the_menu_is_drawn_and_the_cursor_shows_where_it_is(env) -> None:
    lines = tui.render_lines(_view(env, cursor=2), width=140)
    menu = [ln for ln in lines if any(t in ln for _, t in tui.MENU)]
    assert len(menu) == len(tui.MENU), lines
    marked = [ln for ln in menu if ln.startswith(" >")]
    assert len(marked) == 1 and tui.MENU[0][1] in marked[0], menu
    # 계정에 커서가 있을 때는 메뉴에 표시가 없다.
    on_account = tui.render_lines(_view(env, cursor=0), width=140)
    assert not [ln for ln in on_account if ln.startswith(" >") and "Policy" in ln]


def test_enter_on_a_menu_item_goes_in(env) -> None:
    after = tui.activate(_view(env, cursor=2))
    assert after.mode == "policy"


def test_enter_on_an_account_does_not_switch_but_says_what_does(env) -> None:
    """전환은 `s` 다. 조용히 아무 일도 안 하면 사용자는 키가 죽은 줄 안다."""
    before = _view(env, cursor=1)
    after = tui.activate(before)
    assert after.mode == "accounts"
    assert "s" in after.message and "switch" in after.message.lower(), after.message


def test_s_still_switches(env) -> None:
    view = _view(env, cursor=1)
    assert tui.selected_row(view) is not None
    # `do_switch` 는 커서 행을 쓴다 — 그 계약이 살아 있어야 `s` 가 붙는다.
    assert tui.selected_row(view).label == "b"


def test_s_on_a_menu_row_is_refused_gently(env) -> None:
    view = _view(env, cursor=2)
    after = tui.do_switch(view)
    assert after.mode == "accounts"
    assert after.message, "아무 말 없이 무시하면 키가 죽은 줄 안다"


def test_the_key_line_teaches_the_new_layout(env) -> None:
    text, _ = tui.keys_line(tui.ACCOUNT_KEYS, width=140)
    assert "s switch" in text, text
    assert "enter open" in text, text


# ── 방향키는 방향키로 보여야 한다 ──────────────────────────────────────────


def test_the_move_and_adjust_keys_are_drawn_as_arrows(env) -> None:
    """`^v` 와 `<>` 는 방향키를 뜻하는데 그렇게 안 읽힌다 — 산술 기호로 읽힌다."""
    account, _ = tui.keys_line(tui.ACCOUNT_KEYS, width=200)
    policy, _ = tui.keys_line(tui.POLICY_KEYS, width=200)
    assert "↑↓ move" in account, account
    assert "↑↓ move" in policy, policy
    assert "←→ adjust" in policy, policy
    assert "^v" not in account and "<>" not in policy


def test_the_arrow_entry_is_last_so_nothing_after_it_can_shift(env) -> None:
    """화살표 글자는 East Asian **Ambiguous** 라 터미널마다 한 칸도 두 칸도 된다.

    구간 강조는 `_paint` 가 `_width(line[:start])` 로 칸을 계산해 덧칠한다. 그 앞에
    Ambiguous 글자가 있으면 계산과 실제가 갈려 강조가 옆으로 밀린다.

    글자를 바꿀 수는 없다 — 방향키를 뜻하는 글자는 전부 Ambiguous 다. 대신 **맨 뒤에**
    두면 그 뒤에 계산할 구간이 없어서 문제가 성립하지 않는다. 앞의 구간들은 전부
    ASCII 만 지나므로 정확하다.
    """
    for pairs in (tui.ACCOUNT_KEYS, tui.POLICY_KEYS):
        assert pairs[-1][0] == "↑↓" or pairs[-1][0] == "←→", pairs[-1]
        arrows = [i for i, (k, _) in enumerate(pairs) if not k.isascii()]
        assert arrows == list(range(len(pairs) - len(arrows), len(pairs))), pairs

    text, spans = tui.keys_line(tui.ACCOUNT_KEYS, width=200)
    first_arrow = min(i for i, ch in enumerate(text) if not ch.isascii())
    for start, _, _ in spans[:-1]:
        assert start < first_arrow, "화살표 뒤에 강조 구간이 있다 — 밀릴 수 있다"


# ── esc 가 편집을 조용히 버리던 것 ─────────────────────────────────────────


def _edited(env) -> tui.View:
    view = tui.View(rows=(), cursor=0, settings=env, mode="policy", saved_settings=env)
    return tui.edit_policy(replace_view(view), "33,66,88")


def replace_view(view: tui.View) -> tui.View:
    from dataclasses import replace

    return replace(view, policy_cursor=0)


def test_esc_with_unsaved_edits_asks_before_throwing_them_away(env) -> None:
    """편집이 사라지는 것이 조용하면, 사용자는 저장이 됐다고 믿는다.

    화면에 `*` 로 미저장 표시는 있었지만 `esc` 는 아무 말 없이 버렸다. 되돌릴 방법이
    없는 동작이라 한 번은 물어야 한다.
    """
    view = _edited(env)
    assert view.settings.ladder == (33, 66, 88)
    after = tui.leave_policy(view)
    assert after.mode == "policy", "물어보지도 않고 나갔다"
    assert after.discard_armed is True
    assert "unsaved" in after.message.lower(), after.message
    assert after.settings.ladder == (33, 66, 88), "편집이 사라졌다"


def test_a_second_esc_discards(env) -> None:
    after = tui.leave_policy(tui.leave_policy(_edited(env)))
    assert after.mode == "accounts"


def test_esc_leaves_at_once_when_nothing_was_edited(env) -> None:
    """묻는 것은 잃을 것이 있을 때만이다. 늘 물으면 곧 반사적으로 넘긴다."""
    view = tui.View(rows=(), cursor=0, settings=env, mode="policy", saved_settings=env)
    after = tui.leave_policy(view)
    assert after.mode == "accounts"
    assert after.discard_armed is False


def test_moving_the_cursor_disarms_the_pending_discard(env) -> None:
    """물어본 뒤 사용자가 다른 일을 했으면, 그 다음 esc 는 다시 물어야 한다."""
    from dataclasses import replace

    armed = tui.leave_policy(_edited(env))
    moved = replace(armed, policy_cursor=1, discard_armed=False, message="")
    after = tui.leave_policy(moved)
    assert after.mode == "policy", "다시 묻지 않고 버렸다"
