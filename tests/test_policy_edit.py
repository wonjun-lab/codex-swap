"""정책 편집과 자동 전환 스위치 — **CLI 에 없던 것들.**

TUI 에만 있는 동안, CLI 사용자는 사다리를 바꾸려면 `config.json` 을 손으로 고쳐야 했고
자동 전환을 끄려면 README 가 시키는 대로 `~/.claude/.codex-rotate-off` 를 직접 만들어야
했다 — 도구가 자기 내부 파일을 사용자에게 떠넘기는 셈이다.

여기서 재는 것 중 가장 중요한 것은 **"저장했다" 가 "반영됐다" 는 뜻이 아니라는 사실을 말해
주는가** 다. 환경변수가 파일을 이기므로(설계상), 조용히 성공을 보고하면 사용자는 반영된 줄
알고 같은 값을 다시 넣는다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_swap import cli, tui
from codex_swap.core import config, policy_edit


@pytest.fixture
def env(_isolated_home: Path) -> config.Settings:
    return config.load()


# ── 저장이 곧 반영은 아니다 ─────────────────────────────────────────────────


def test_a_saved_knob_that_an_env_var_overrides_is_reported_not_swallowed(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """조용히 "저장했다" 만 하면 사용자는 반영된 줄 안다."""
    monkeypatch.setenv("CODEX_ROTATE_LADDER", "11,22")
    saved = policy_edit.save(env, ladder=[50, 70, 85, 95])
    assert saved.shadowed == ("Ladder",), saved
    assert json.loads(saved.path.read_text())["ladder"] == [50, 70, 85, 95], (
        "파일에는 들어가야 한다"
    )


def test_nothing_is_reported_as_shadowed_when_it_actually_took(env: config.Settings) -> None:
    """늘 경고하면 그 경고가 곧 안 읽힌다."""
    assert policy_edit.save(env, ladder=[60, 80], margin=7).shadowed == ()
    assert config.load().ladder == (60, 80)


def test_only_the_knobs_we_touched_are_checked(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """건드리지 않은 노브까지 "안 먹는다" 고 말하면 무엇을 고쳐야 하는지 흐려진다."""
    monkeypatch.setenv("CODEX_ROTATE_MARGIN", "9")
    assert policy_edit.save(env, ladder=[60, 80]).shadowed == ()


def test_the_cli_says_it_out_loud_and_fails(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """rc 0 으로 끝내면 스크립트가 성공으로 읽는다."""
    monkeypatch.setenv("CODEX_ROTATE_LADDER", "11,22")
    with pytest.raises(cli.CliError, match="not in effect"):
        cli.cmd_policy(env, {"ladder": [50, 70]})
    assert "saved:" in capsys.readouterr().out, "파일에 들어간 사실은 말해야 한다"


def test_an_unknown_knob_is_refused(env: config.Settings) -> None:
    """오타가 조용히 파일에 새 키를 만들면 아무도 못 알아챈다."""
    with pytest.raises(ValueError, match="not a policy knob"):
        policy_edit.save(env, laddder=[50])


# ── 값이 하나라도 틀리면 아무것도 저장하지 않는다 ──────────────────────────


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["policy", "--margin", "abc"], "not an integer"),
        (["policy", "--margin", "-1"], "cannot be negative"),
        (["policy", "--ladder", "oops"], "not a ladder"),
        (["policy", "--cooldown", ""], "not an integer"),
    ],
)
def test_a_bad_value_stops_before_the_file_is_touched(
    env: config.Settings, argv: list[str], message: str
) -> None:
    """값 하나가 틀렸는데 나머지를 저장하면, 무엇이 들어갔는지 모른 채 파일만 바뀐다."""
    before = (env.accounts_dir / config.CONFIG_NAME).exists()
    assert cli.main(argv) == 1
    assert (env.accounts_dir / config.CONFIG_NAME).exists() == before


def test_a_bad_value_alongside_good_ones_saves_none_of_them(env: config.Settings) -> None:
    assert cli.main(["policy", "--ladder", "60,80", "--margin", "abc"]) == 1
    assert config.load().ladder == config.DEFAULT_LADDER


# ── 자동 전환 스위치 ────────────────────────────────────────────────────────


def test_the_switch_is_the_file_the_readme_told_people_to_make(env: config.Settings) -> None:
    """같은 파일이어야 한다. 다른 자리를 쓰면 예전 안내를 따른 사람의 설정이 무시된다."""
    policy_edit.set_auto(env, False)
    assert env.off_switch.exists()
    policy_edit.set_auto(env, True)
    assert not env.off_switch.exists()


def test_setting_it_to_what_it_already_is_leaves_the_file_alone(env: config.Settings) -> None:
    """`touch` 는 mtime 을 바꾼다. 그 나이를 보는 코드가 나중에 생길 수 있다.

    **시각을 직접 심는다.** 처음에는 두 번 호출 사이의 mtime 을 견줬는데, 리눅스는 파일
    시각을 성긴 시계로 찍어서 두 호출이 같은 눈금에 들어가면 값이 같다 — 검사가 통과했지만
    아무것도 재지 않았고, 뮤테이션이 살아남아서야 알았다.
    """
    import os

    policy_edit.set_auto(env, False)
    long_ago = 1_000_000_000
    os.utime(env.off_switch, (long_ago, long_ago))

    policy_edit.set_auto(env, False)
    assert env.off_switch.stat().st_mtime == long_ago, "이미 꺼져 있는데 파일을 다시 건드렸다"


def test_turning_it_on_when_it_is_already_on_does_not_fail(env: config.Settings) -> None:
    assert policy_edit.auto_on(env)
    assert policy_edit.set_auto(env, True) is True
    assert not env.off_switch.exists()


def test_the_cli_shows_the_state_without_changing_it(env: config.Settings, capsys) -> None:
    assert cli.main(["auto"]) == 0
    assert "on" in capsys.readouterr().out
    assert not env.off_switch.exists(), "보여 주기만 해야 하는데 바꿨다"


def test_the_cli_names_the_switch_when_it_is_off(env: config.Settings, capsys) -> None:
    """끈 사실만 알려 주고 어디에 있는지 안 말하면, 손으로 되돌릴 방법이 없다."""
    cli.main(["auto", "off"])
    capsys.readouterr()
    cli.main(["auto"])
    out = capsys.readouterr().out
    assert "off" in out
    assert str(env.off_switch) in out, out


@pytest.mark.parametrize("state", ["on", "off"])
def test_the_cli_round_trips(env: config.Settings, state: str) -> None:
    assert cli.main(["auto", state]) == 0
    assert policy_edit.auto_on(env) is (state == "on")


# ── 두 표면이 같은 노브를 갖는가 ────────────────────────────────────────────


def test_both_surfaces_edit_the_same_knobs() -> None:
    """한쪽에만 있는 노브는 그 표면에서만 고칠 수 있다.

    사용자는 다른 표면에서 그것을 찾다가 못 찾고, 없는 줄 안다.
    """
    assert [key for key, _ in policy_edit.FIELDS] == [key for key, _, _ in tui.POLICY_FIELDS]


def test_the_cli_has_a_flag_for_every_knob() -> None:
    """core 에 노브를 더하고 CLI 플래그를 잊으면 CLI 사용자만 못 고친다."""
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if getattr(a, "choices", None))
    dests = {act.dest for act in sub.choices["policy"]._actions}
    missing = {key for key, _ in policy_edit.FIELDS} - dests - {"check_interval"}
    assert not missing, f"플래그가 없는 노브: {sorted(missing)}"
    assert "throttle" in dests, "check_interval 의 플래그 이름이 바뀌었다"


def test_the_tui_and_the_cli_write_through_the_same_function(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """따로 구현하면 갈리고, 갈리면 한쪽만 경고를 잃는다.

    실제로 환경변수 경고가 TUI 에만 있었다.
    """
    calls: list[dict[str, object]] = []
    real = policy_edit.save

    def spy(settings: config.Settings, **values: object):
        calls.append(values)
        return real(settings, **values)

    monkeypatch.setattr(policy_edit, "save", spy)
    cli.cmd_policy(env, {"margin": 7})
    tui.save_policy(tui.build_view(config.load()))
    assert len(calls) == 2, calls


# ── codex 교차 검토가 잡은 것들 ─────────────────────────────────────────────


def test_both_surfaces_normalise_the_ladder_the_same_way(env: config.Settings) -> None:
    """정렬을 한쪽만 하면 **같은 사다리가 다른 관문**을 낸다.

    정책은 첫 상회 항목을 관문으로 고른다. `90,50,70` 을 그대로 두면 후보 40% 에서 관문이
    90 인데, 정렬하면 50 이다 — 표시가 아니라 **판단**이 갈린다. codex 가 잡았다.
    """
    policy_edit.save(env, ladder=[90, 50, 70])
    assert config.load().ladder == (50, 70, 90)

    view = tui.replace(tui.build_view(config.load()), mode="policy", policy_cursor=0)
    typed = tui.edit_policy(view, "90,50,70")
    assert typed.settings.ladder == (50, 70, 90), "두 표면이 다르게 정규화한다"


def test_a_repeated_rung_is_stored_once(env: config.Settings) -> None:
    policy_edit.save(env, ladder=[70, 50, 70, 50])
    assert config.load().ladder == (50, 70)


def test_saving_onto_a_file_we_cannot_read_is_refused(env: config.Settings) -> None:
    """깨진 파일을 `{}` 로 접고 그 위에 병합하면 **거기 있던 키가 통째로 사라진다.**

    그러고 나면 정상 JSON 이라 경고할 기회도 없다. 읽기 경로의 침묵(rotate 핫패스를 위한
    옳은 선택)을 편집 경로까지 가져온 결과였다 — codex 가 잡았다.
    """
    path = env.accounts_dir / config.CONFIG_NAME
    path.write_text('{"margin": 9, "busy_window": 77,}')  # 마지막 쉼표 하나
    with pytest.raises(policy_edit.CreditsFileError, match="cannot be read"):
        policy_edit.save(env, cooldown=1200)
    assert path.read_text() == '{"margin": 9, "busy_window": 77,}', "파일이 바뀌었다"


def test_the_cli_turns_that_into_a_message_not_a_traceback(env: config.Settings) -> None:
    (env.accounts_dir / config.CONFIG_NAME).write_text("{ broken")
    assert cli.main(["policy", "--cooldown", "1200"]) == 1


def test_the_tui_only_saves_the_knobs_it_changed(env: config.Settings) -> None:
    """화면을 열어 둔 사이에 CLI 가 고친 값을 이 저장이 되돌리면 안 된다.

    다섯을 통째로 넘기던 때는, margin 만 바꿔 저장해도 cooldown 이 화면을 열 때의 값으로
    돌아갔다 — 그쪽을 건드린 적도 없는데. codex 가 잡았다.
    """
    view = tui.replace(tui.build_view(env), mode="policy", saved_settings=env)
    # 화면을 열어 둔 사이에 다른 곳에서 cooldown 을 바꿨다.
    policy_edit.save(env, cooldown=1200)
    # 화면에서는 margin 만 고쳤다.
    edited = tui.replace(view, settings=config.load().__class__(**{**vars(env), "margin": 7}))
    tui.save_policy(tui.replace(edited, saved_settings=env))
    fresh = config.load()
    assert fresh.margin == 7
    assert fresh.cooldown == 1200, "건드리지도 않은 값이 되돌아갔다"


def test_saving_with_nothing_changed_says_so(env: config.Settings) -> None:
    view = tui.replace(tui.build_view(env), mode="policy", saved_settings=env)
    after = tui.save_policy(view)
    assert "Nothing to save" in after.message
    assert not (env.accounts_dir / config.CONFIG_NAME).exists()


def test_the_wording_does_not_blame_the_environment_without_evidence(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """아는 것은 "요청한 값이 실효값과 다르다" 까지다. 원인 단정은 틀릴 수 있다."""
    monkeypatch.setenv("CODEX_ROTATE_MARGIN", "9")
    with pytest.raises(cli.CliError) as exc:
        cli.cmd_policy(env, {"margin": 7})
    assert "not in effect" in str(exc.value)
    assert "usually" in str(exc.value), "원인을 단정했다"


def test_auto_says_when_an_env_var_makes_the_switch_meaningless(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """`CODEX_ROTATE_SKIP` 이 걸리면 스위치가 켜져 있어도 **한 번도 안 돈다.**

    파일만 보고 "on" 이라고 말하면 화면은 정상이라는데 실제로는 아무 일도 안 일어난다.
    codex 가 잡았다.
    """
    monkeypatch.setenv("CODEX_ROTATE_SKIP", "1")
    assert cli.main(["auto"]) == 0
    out = capsys.readouterr().out
    assert "on" in out
    assert "CODEX_ROTATE_SKIP" in out, out


def test_auto_stays_quiet_about_the_env_when_it_is_not_set(env: config.Settings, capsys) -> None:
    assert cli.main(["auto"]) == 0
    assert "CODEX_ROTATE_SKIP" not in capsys.readouterr().out


@pytest.mark.parametrize("bad", ["--1", "²", "1_0", "+5"])
def test_a_number_that_int_would_choke_on_is_a_message_not_a_traceback(bad: str) -> None:
    """`parse_int` 는 **환경변수도** 지난다. 여기서 새면 `rotate` 가 매 codex 호출마다 죽는다.

    `lstrip("-")` 이 부호를 전부 벗겨 `"--1"` 이 `"1"` 로 보였고, `str.isdigit()` 이 참인
    `"²"` 도 통과해 `int()` 에서 `ValueError` 가 됐다 — codex 가 잡았다.
    """
    with pytest.raises(config.ConfigError):
        config.parse_int(bad)


def test_the_throttle_flag_actually_reaches_the_knob(env: config.Settings) -> None:
    """이름 검사만으로는 `--throttle` 의 매핑이 끊겨도 통과한다 — codex 가 지목한 구멍."""
    assert cli.main(["policy", "--throttle", "45"]) == 0
    assert config.load().check_interval == 45
