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
    assert "이미 활성" in again.message
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
    assert "r 로 다시 시도" in after.message
    assert [r.used for r in after.rows] == ["?", "?"]


def test_the_stale_marker_is_explained_only_when_something_is_stale(env) -> None:
    """늘 떠 있는 안내는 곧 안 읽힌다. 낡은 행이 있을 때만 범례를 낸다."""
    _cache_usage(env, "master", 38)
    _cache_usage(env, "shared", 70)
    assert "~ 는" not in text(tui.build_view(env))

    _cache_usage(env, "master", 38, age=env.cache_ttl + 10)
    body = text(tui.build_view(env))
    assert "~38%" in body and "~ 는 캐시가 낡았다는 표시다" in body


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
    assert "master" in msg and "shared" in msg and "r 로 다시 시도" in msg


def test_refresh_message_reports_a_missing_codex_instead_of_raising(env, monkeypatch) -> None:
    """이 함수는 스레드 안에서 돈다. 예외를 올리면 화면이 아무 말도 못 듣는다."""

    def missing():
        raise RuntimeError("없다")

    monkeypatch.setattr(tui, "resolve_codex_bin", missing)
    assert "codex 를 찾지 못했다" in tui._refresh_message(env, ("master",))
