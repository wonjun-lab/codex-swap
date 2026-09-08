"""CLI 어댑터 테스트 — 표면·exit code·출력 규율.

특히 `rotate` 의 출력 규율(계약 2)을 여기서 지킨다. wrapper 가 매 codex 호출마다 이
명령을 부르고 stdout 만 버리므로, stderr 로 새는 것은 전부 사용자 화면에 실린다.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

from codex_swap import cli
from codex_swap.core import cache, config, discovery, identity, store
from codex_swap.core.types import ProbeOutcome, ProbeResult, Usage


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
    for k in ("CODEX_ROTATE_SKIP", "CODEX_HOME", "CODEX_ROTATE_LADDER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_ACCOUNT_DEFAULT_HOME", str(home / ".codex"))
    monkeypatch.setenv("CODEX_ACCOUNTS_DIR", str(home / ".codex/accounts"))
    monkeypatch.setenv("CODEX_ROTATE_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("CODEX_ACCOUNT_BIN", "/bin/true")
    s = config.load()
    _write_auth(store.active_auth(s), "a@example.com")
    _write_auth(store.slot_auth(s, "a"), "a@example.com")
    _write_auth(store.slot_auth(s, "b"), "b@example.com")
    return s


# ── 표면 ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [("ls", "list"), ("switch", "use"), ("rm", "remove")],
)
def test_aliases_survive(alias: str, canonical: str) -> None:
    """별칭은 사용자의 손과 스크립트에 이미 박혀 있다."""
    argv = [alias, "x"] if canonical in {"use", "remove"} else [alias]
    assert cli.build_parser().parse_args(argv).command == alias


def test_bare_invocation_prints_help_and_succeeds(capsys) -> None:
    assert cli.main([]) == 0
    assert "codex-swap" in capsys.readouterr().out


# ── 라벨 관문 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["../evil", "a/b", ".hidden", "..", "a b"])
@pytest.mark.parametrize("command", ["adopt", "use", "remove"])
def test_bad_labels_are_refused_by_every_command(env, capsys, command: str, bad: str) -> None:
    """bash 는 이 검사가 adopt·add 에만 있었고 use·remove 에는 없었다."""
    assert cli.main([command, bad]) == 1
    assert "not a usable label" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["adopt", "use", "remove"])
def test_a_hyphen_leading_label_is_refused_earlier_by_argparse(env, command: str) -> None:
    """의도된 divergence: 거부는 같고 exit code 가 다르다.

    `-rf` 는 argparse 가 옵션으로 읽어 우리 검증에 닿기 전에 exit 2 로 끝낸다. bash 는
    같은 입력을 `$1` 로 받아 `ensure_label` 이 exit 1 로 거부한다. 거부된다는 결론은
    양쪽이 같고 Python 쪽이 오히려 더 이르므로 맞추지 않는다 — 다만 이름을 붙여 둔다.
    `--` 뒤에 오면 우리 관문까지 도달해 exit 1 이 된다(아래 반례).
    """
    with pytest.raises(SystemExit) as exc:
        cli.main([command, "-rf"])
    assert exc.value.code == 2


@pytest.mark.parametrize("command", ["adopt", "use", "remove"])
def test_a_hyphen_label_after_a_double_dash_reaches_our_gate(env, capsys, command: str) -> None:
    assert cli.main([command, "--", "-rf"]) == 1
    assert "not a usable label" in capsys.readouterr().err


def test_remove_refuses_a_symlinked_slot(env, capsys, tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, env.accounts_dir / "linked")
    assert cli.main(["remove", "linked"]) == 1
    assert outside.exists(), "루트 밖 디렉토리가 지워졌다"


# ── rotate 출력 규율 ─────────────────────────────────────────────────────────


def test_rotate_is_silent_when_it_does_nothing(env, capsys, monkeypatch) -> None:
    """평상시 매 codex 호출에서 화면에 아무것도 남기지 않아야 한다."""
    monkeypatch.setenv("CODEX_ROTATE_SKIP", "1")
    assert cli.main(["rotate"]) == 1
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_rotate_writes_nothing_to_stdout_even_when_it_switches(env, capsys) -> None:
    """wrapper 는 stdout 만 버린다. 전환 통지는 stderr 한 줄이어야 한다."""
    from codex_swap.core import rotate as rotate_mod
    from codex_swap.core.types import ProbeResult, Usage

    def fake(settings, *, dry_run=False, probe_fn=None, now=None):
        return rotate_mod._rotate(
            settings,
            dry_run=dry_run,
            probe_fn=lambda b, h: ProbeResult.of(
                Usage(used_percent=95 if Path(h).name == ".codex" else 1)
            ),
            now=now,
        )

    import codex_swap.cli as cli_mod

    original = cli_mod.rotate.rotate
    cli_mod.rotate.rotate = fake
    try:
        rc = cli.main(["rotate"])
    finally:
        cli_mod.rotate.rotate = original

    out = capsys.readouterr()
    assert rc == 0
    assert out.out == "", f"stdout 에 무언가 나갔다: {out.out!r}"
    assert out.err.count("\n") == 1 and "->" in out.err
    assert "id_token" not in out.err and "eyJ" not in out.err


# ── adopt / list ─────────────────────────────────────────────────────────────


def test_adopt_registers_the_live_account(env, capsys) -> None:
    assert cli.main(["adopt", "fresh"]) == 0
    assert identity.email_of(store.slot_auth(env, "fresh")) == "a@example.com"
    assert oct(os.stat(store.slot_auth(env, "fresh")).st_mode & 0o777) == "0o600"
    assert oct(os.stat(store.slot_dir(env, "fresh")).st_mode & 0o777) == "0o700"


def test_list_marks_the_active_slot(env, capsys) -> None:
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    marked = [line for line in lines if line.startswith("*")]
    assert len(marked) == 1 and " a " in marked[0]


def test_list_on_an_empty_store_explains_what_to_do(tmp_path, monkeypatch, capsys) -> None:
    home = tmp_path / "home"
    (home / ".codex/accounts").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_ACCOUNT_DEFAULT_HOME", str(home / ".codex"))
    monkeypatch.setenv("CODEX_ACCOUNTS_DIR", str(home / ".codex/accounts"))
    assert cli.main(["list"]) == 0
    assert "adopt" in capsys.readouterr().out


# ── use / clean ──────────────────────────────────────────────────────────────


def test_use_switches_and_says_so(env, capsys) -> None:
    assert cli.main(["use", "b"]) == 0
    assert identity.email_of(store.active_auth(env)) == "b@example.com"
    assert "switched to b" in capsys.readouterr().out


def test_clean_keeps_auth_and_removes_the_rest(env, capsys) -> None:
    slot = store.slot_dir(env, "b")
    (slot / "sqlite.db").write_text("junk")
    (slot / "sub").mkdir()
    assert cli.main(["clean"]) == 0
    assert store.slot_auth(env, "b").is_file()
    assert not (slot / "sqlite.db").exists() and not (slot / "sub").exists()


# ── list 의 사용량 표시 ──────────────────────────────────────────────────────
#
# `?` 하나가 "TTL 이 지났다" 와 "읽지 못했다" 를 겹쳐 쓰고 있었다. 사람은 그 둘을 구별할
# 방법이 없어 토큰이 끊긴 줄 안다. `list` 가 프로브를 돌리지 않는 것은 그대로 두되(매
# 호출이 싸야 한다), 디스크에 있는 값은 낡았더라도 보여 준다.


def _cache_usage(s, label: str, pct: int, *, age: int = 0) -> None:
    import time

    from codex_swap.core import cache

    cache.write(s, label, {"usedPercent": pct, "resetsAt": None}, now=int(time.time()) - age)


def test_list_shows_a_fresh_number_plainly(env, capsys) -> None:
    _cache_usage(env, "a", 38)
    cli.main(["list"])
    out = capsys.readouterr().out
    assert "38%" in out and "~38%" not in out
    assert "stale cached value" not in out


def test_list_still_shows_an_expired_number_marked_stale(env, capsys) -> None:
    """TTL 이 지났다고 물음표를 찍으면 안 된다. 그건 '읽지 못했다' 가 아니다."""
    _cache_usage(env, "a", 38, age=env.cache_ttl + 10)
    cli.main(["list"])
    out = capsys.readouterr().out
    assert "~38%" in out
    assert "stale cached value" in out, "범례가 없으면 `~` 를 오류로 읽는다"


def test_list_keeps_the_question_mark_for_a_slot_never_read(env, capsys) -> None:
    """한 번도 못 읽은 슬롯은 정직하게 물음표다. 지어내지 않는다."""
    cli.main(["list"])
    out = capsys.readouterr().out
    assert "?" in out and "%" not in out.split("ladder")[0]
    assert "stale cached value" not in out


def test_list_does_not_probe(env, capsys, monkeypatch) -> None:
    """`list` 는 네트워크를 타지 않는 조회여야 매 호출이 싸다."""

    def forbidden(*a, **k):
        raise AssertionError("list 가 프로브를 돌렸다")

    monkeypatch.setattr("codex_swap.core.probe.probe", forbidden)
    _cache_usage(env, "a", 38, age=env.cache_ttl + 10)
    assert cli.main(["list"]) == 0


def test_list_says_when_auto_switching_is_off(env, capsys) -> None:
    """꺼 놓은 것을 잊고 "왜 안 바뀌지" 를 디버깅하게 두면 안 된다."""
    env.off_switch.parent.mkdir(parents=True, exist_ok=True)
    env.off_switch.touch()
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "automatic switching: off" in out and str(env.off_switch) in out


# ── remove 의 성공 경로 ──────────────────────────────────────────────────────
#
# 이 도구에서 파일을 **지우는** 명령은 이것 하나다. 거부 경로(나쁜 라벨·심링크·없는
# 라벨)는 이미 고정돼 있었는데 정작 "지울 때 그것만 지우는가" 가 비어 있었다. 거부만
# 검사하면 `rmtree` 의 인자가 한 칸 위를 가리켜도 전부 통과한다.


def test_remove_deletes_only_the_named_slot(env, capsys) -> None:
    assert cli.main(["remove", "a"]) == 0
    assert not store.slot_dir(env, "a").exists()
    assert store.slot_auth(env, "b").is_file(), "옆 슬롯이 함께 지워졌다"
    assert env.accounts_dir.is_dir(), "슬롯 루트까지 지워졌다"
    assert "removed a" in capsys.readouterr().out


def test_remove_does_not_log_you_out(env) -> None:
    """슬롯을 지우는 것과 로그아웃은 다른 일이다.

    활성 자격증명은 기본 홈에 따로 있고 슬롯은 그 사본을 보관할 뿐이다. 지금 쓰는
    계정의 슬롯을 지웠다고 세션까지 끊기면 사용자는 영문도 모른 채 다시 로그인한다.
    """
    assert cli.main(["remove", "a"]) == 0
    assert identity.email_of(store.active_auth(env)) == "a@example.com"


def test_remove_forgets_the_cached_usage(env) -> None:
    """라벨은 재사용된다. 지운 계정의 숫자가 그 이름에 남아 있으면 안 된다.

    캐시는 라벨로만 색인된다 — 어느 계정의 숫자인지는 적혀 있지 않다. `remove work`
    직후 `adopt work` 로 다른 계정을 같은 이름에 넣으면, TTL 이 지나기 전까지 새 계정이
    옛 계정의 사용량을 뒤집어쓴다. 표시만의 문제가 아니다: `rotate` 도 이 캐시를 정책
    입력으로 읽으므로(`rotate.py` `_usage_of`), 방금 등록한 멀쩡한 계정이 92% 로 보여
    후보에서 빠지거나 반대로 소진된 계정이 3% 로 보여 선택된다.
    """
    _cache_usage(env, "a", 38)
    assert cli.main(["remove", "a"]) == 0
    assert cache.read(env, "a") is None
    assert cache.read_stale(env, "a") is None, "지운 라벨의 숫자가 캐시에 남았다"


# ── status ───────────────────────────────────────────────────────────────────
#
# `--fresh` 는 프로브를 타는 **유일한** CLI 경로다. 그 하나가 통째로 미검증이었다.


def _probe_recorder(result: ProbeResult, calls: list[tuple[str, str]]):
    def run(codex_bin: str, home: str) -> ProbeResult:
        calls.append((codex_bin, home))
        return result

    return run


def test_status_answers_from_the_cache_without_probing(env, capsys, monkeypatch) -> None:
    """캐시가 살아 있으면 조회하지 않는다 — 그것이 TTL 을 두는 이유다."""

    def forbidden(*a, **k):
        raise AssertionError("status 가 살아 있는 캐시를 두고 프로브를 돌렸다")

    monkeypatch.setattr("codex_swap.core.probe.probe", forbidden)
    _cache_usage(env, "a", 38)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "active account: a" in out and "usage: 38%" in out


def test_status_fresh_ignores_a_live_cache(env, capsys, monkeypatch) -> None:
    """`--fresh` 가 캐시를 존중하면 그 플래그는 아무 일도 하지 않는 것이다."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "codex_swap.core.probe.probe",
        _probe_recorder(ProbeResult.of(Usage(used_percent=71)), calls),
    )
    _cache_usage(env, "a", 38)
    assert cli.main(["status", "--fresh"]) == 0
    out = capsys.readouterr().out
    assert "usage: 71%" in out and "38%" not in out
    assert len(calls) == 1
    assert calls[0][1] == str(store.slot_dir(env, "a")), "활성 슬롯이 아닌 홈을 조회했다"


def test_status_probes_when_the_cache_is_stale(env, capsys, monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "codex_swap.core.probe.probe",
        _probe_recorder(ProbeResult.of(Usage(used_percent=71)), calls),
    )
    _cache_usage(env, "a", 38, age=env.cache_ttl + 10)
    assert cli.main(["status"]) == 0
    assert "usage: 71%" in capsys.readouterr().out
    assert len(calls) == 1


def test_status_probes_the_default_home_when_no_slot_matches(env, capsys, monkeypatch) -> None:
    """설계문 §7.5 D3 의 회귀 가드.

    bash 는 활성 계정이 어느 슬롯에도 없을 때 가짜 라벨을 만들어
    `CODEX_HOME=<root>/__active__` 로 프로브를 돌렸다. 자격증명은 실제로 기본 홈에
    있으므로 그 조회는 언제나 빈 홈을 보고 "조회 실패" 로 끝난다 — 정작 로그인은
    멀쩡한데도. 여기서는 **어느 홈을 물었는가**를 직접 본다.
    """
    _write_auth(store.active_auth(env), "z@example.com")  # 어느 슬롯과도 안 맞는 계정
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "codex_swap.core.probe.probe",
        _probe_recorder(ProbeResult.of(Usage(used_percent=12)), calls),
    )
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "z@example.com" in out and "not in any slot" in out
    assert calls[0][1] == str(env.default_home)
    assert "__active__" not in calls[0][1]


@pytest.mark.parametrize(
    "outcome",
    [ProbeOutcome.AUTH_FAILED, ProbeOutcome.UNKNOWN],
)
def test_status_folds_every_failure_into_one_line(env, capsys, monkeypatch, outcome) -> None:
    """실패의 종류를 늘어놓지 않는다. 사용자에게 쓸모 있는 것은 "지금 모른다" 다."""
    monkeypatch.setattr(
        "codex_swap.core.probe.probe",
        _probe_recorder(ProbeResult(outcome), []),
    )
    assert cli.main(["status", "--fresh"]) == 1
    assert "usage: probe failed" in capsys.readouterr().out


def test_status_survives_a_throwing_probe(env, capsys, monkeypatch) -> None:
    """프로브가 던져도 역추적이 사용자 화면으로 새면 안 된다."""

    def boom(*a, **k):
        raise RuntimeError("id_token eyJhbGciOi... 유출 금지")

    monkeypatch.setattr("codex_swap.core.probe.probe", boom)
    assert cli.main(["status", "--fresh"]) == 1
    out = capsys.readouterr()
    assert "usage: probe failed" in out.out
    assert "eyJ" not in out.out and "eyJ" not in out.err


# ── rotate --dry-run ─────────────────────────────────────────────────────────


def test_dry_run_says_what_it_would_do_and_changes_nothing(env, capsys) -> None:
    """`--dry-run` 이 자격증명을 건드리면 그건 dry 가 아니다."""
    from codex_swap.core import rotate as rotate_mod

    def fake(settings, *, dry_run=False, probe_fn=None, now=None):
        return rotate_mod._rotate(
            settings,
            dry_run=dry_run,
            probe_fn=lambda b, h: ProbeResult.of(
                Usage(used_percent=95 if Path(h).name == ".codex" else 1)
            ),
            now=now,
        )

    original = cli.rotate.rotate
    cli.rotate.rotate = fake
    try:
        assert cli.main(["rotate", "--dry-run"]) == 0
    finally:
        cli.rotate.rotate = original

    assert "would switch:" in capsys.readouterr().out
    assert identity.email_of(store.active_auth(env)) == "a@example.com", "dry-run 이 전환했다"


def test_dry_run_also_explains_a_non_switch(env, capsys, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_ROTATE_SKIP", "1")
    cli.main(["rotate", "--dry-run"])
    assert "no switch:" in capsys.readouterr().out


# ── 처음 쓰는 사람이 실제로 밟는 자리 ───────────────────────────────────────
#
# 아래 둘은 격리 환경에 방금 설치한 상태를 재현해서 찾은 것이다. 단위 테스트가 전부
# 통과하는 동안 둘 다 살아 있었다 — 기존 테스트가 **이미 로그인된** 픽스처에서만
# 돌기 때문이다. 계정이 하나도 없는 상태가 새 사용자의 첫 화면인데 그 상태를
# 아무도 보지 않았다.


def test_status_does_not_call_a_probe_failure_when_you_are_simply_logged_out(
    tmp_path, monkeypatch, capsys
) -> None:
    """로그인 전에는 "probe failed" 가 아니라 "로그인하라" 여야 한다.

    자격증명이 없으면 프로브는 실패할 수밖에 없다. 그 실패를 그대로 보여 주면 새
    사용자는 도구가 깨진 줄 안다 — 실제로는 아직 아무것도 안 한 상태다.
    """
    home = tmp_path / "home"
    (home / ".codex/accounts").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_ACCOUNT_DEFAULT_HOME", str(home / ".codex"))
    monkeypatch.setenv("CODEX_ACCOUNTS_DIR", str(home / ".codex/accounts"))

    def forbidden(*a, **k):
        raise AssertionError("로그인도 안 된 상태에서 프로브를 돌렸다")

    monkeypatch.setattr("codex_swap.core.probe.probe", forbidden)
    assert cli.main(["status"]) == 1
    out = capsys.readouterr().out
    assert "probe failed" not in out, out
    assert "codex login" in out, out


def test_add_reports_an_unrunnable_codex_instead_of_a_traceback(env, capsys, monkeypatch) -> None:
    """실행 파일이 없으면 `subprocess` 가 `OSError` 를 던진다 — 그게 화면까지 갔다.

    `CODEX_ACCOUNT_BIN` 은 의도적으로 검증하지 않는다(핫패스). 그 대가를 `add` 가
    raw traceback 으로 치르고 있었다. `main` 의 예외 처리는 `CliError` 계열만 잡는다.

    방어가 **두 층**이라 각각 보장하는 것을 따로 본다. `_run_login` 의 핸들러는
    *무엇을 하면 되는지*를 주고, `main` 의 안전망은 *역추적을 막는* 것까지만 한다.
    "codex" 가 나오는지만 보면 `codex-swap:` 접두어에도 걸려서 둘 중 하나가 사라져도
    통과한다 — 실제로 그렇게 뮤테이션 둘을 놓쳤다.
    """
    monkeypatch.setenv("CODEX_ACCOUNT_BIN", str(tmp_missing := env.accounts_dir / "no-such-codex"))
    assert not tmp_missing.exists()
    assert cli.main(["add", "fresh"]) == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err, err
    assert str(tmp_missing) in err, err
    # 처방까지 준다. 이 문구가 없으면 사용자는 무엇을 고쳐야 할지 모른 채 errno 만 본다.
    assert "CODEX_ACCOUNT_BIN" in err, err


def test_no_command_lets_an_os_error_reach_the_screen_as_a_traceback(env, capsys, monkeypatch):
    """`main` 의 안전망 자체. 어느 명령에서든 `OSError` 는 한 줄로 접혀야 한다.

    위 테스트는 `_run_login` 의 처방 문구가 먼저 잡아 주므로 안전망을 지나가지 않는다.
    안전망이 실제로 있는지는 그 층을 직접 건드려야 보인다 — 여기서는 `cmd_list` 가
    쓰는 열거가 `OSError` 를 던지게 만든다.
    """

    def boom(*a, **k):
        raise PermissionError(13, "Permission denied", str(env.accounts_dir))

    monkeypatch.setattr("codex_swap.core.store.labels", boom)
    assert cli.main(["list"]) == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err, err
    assert "Permission denied" in err, err


# ── 자격증명을 되돌릴 수 없게 잃는 두 경로 ─────────────────────────────────
#
# 둘 다 TUI 에는 이미 방어가 있고 CLI 에만 없었다. 같은 위험에 대해 두 표면이 다르게
# 행동하면, 더 약한 쪽이 곧 그 도구의 실제 안전 수준이다.


def test_adopt_refuses_to_overwrite_another_account(env, capsys) -> None:
    """`adopt` 는 활성 자격증명을 그 이름 위에 **그냥 복사**한다.

    기존 이름을 입력하면 그 계정의 보관본이 사라지고 되돌릴 방법이 없다. `tui.do_adopt`
    는 정확히 이것을 막는데(docstring: "되돌릴 방법이 없다") CLI 에는 가드가 없었다.
    """
    assert identity.email_of(store.slot_auth(env, "b")) == "b@example.com"
    assert cli.main(["adopt", "b"]) == 1
    assert identity.email_of(store.slot_auth(env, "b")) == "b@example.com", "덮어썼다"
    err = capsys.readouterr().err
    assert "b@example.com" in err, err
    assert "remove" in err, "무엇을 하면 되는지 말해야 한다"


def test_adopt_still_refreshes_the_same_account(env, capsys) -> None:
    """같은 계정이면 갱신이다 — 썩은 사본을 새로 뜨는 정상 용법이라 막으면 안 된다."""
    assert cli.main(["adopt", "a"]) == 0
    assert identity.email_of(store.slot_auth(env, "a")) == "a@example.com"


def test_adopt_into_a_free_label_is_untouched(env) -> None:
    assert cli.main(["adopt", "brand-new"]) == 0
    assert identity.email_of(store.slot_auth(env, "brand-new")) == "a@example.com"


def test_use_refuses_to_discard_an_unregistered_active_credential(env, capsys) -> None:
    """전환은 활성 자격증명을 슬롯으로 되돌려 놓고(sync-back) 바꾼다.

    그런데 활성이 어느 슬롯과도 안 맞으면 되돌려 놓을 자리가 없어 그냥 사라진다
    (`store.switch` 가 `active_label is None` 이면 sync-back 을 건너뛴다). 사용자가 손으로
    `codex login` 한 계정이 그 경우이고, 잃으면 브라우저 재로그인 말고는 복구가 없다.
    TUI 는 이 상태에 전용 경고줄을 띄운다 — CLI 는 아무 말 없이 실행했다.
    """
    _write_auth(store.active_auth(env), "unregistered@example.com")
    assert cli.main(["use", "a"]) == 1
    assert identity.email_of(store.active_auth(env)) == "unregistered@example.com", "버렸다"
    err = capsys.readouterr().err
    assert "unregistered@example.com" in err, err
    assert "adopt" in err and "--force" in err, err


def test_use_force_still_discards_when_you_mean_it(env, capsys) -> None:
    """버리는 것이 뜻인 경우가 있다(임시 로그인). 다만 말로 밝혀야 한다."""
    _write_auth(store.active_auth(env), "throwaway@example.com")
    assert cli.main(["use", "--force", "a"]) == 0
    assert identity.email_of(store.active_auth(env)) == "a@example.com"


def test_use_is_unaffected_when_the_active_account_is_registered(env) -> None:
    assert cli.main(["use", "b"]) == 0
    assert identity.email_of(store.active_auth(env)) == "b@example.com"


def test_status_fresh_keeps_what_it_just_read(env, capsys, monkeypatch) -> None:
    """`list` 는 "새로 읽으려면 status --fresh" 라고 안내한다. 그 말이 참이어야 한다.

    `--fresh` 는 프로브를 돌리고도 결과를 버렸다. 그래서 안내를 따라도 표는 그대로
    `?` 였다 — 캐시에 쓰는 곳이 `rotate` 뿐이었기 때문이다. 방금 읽은 값을 그 라벨의
    것으로 남긴다. 라벨을 아는 경우(활성이 슬롯에 있음)에만 쓸 수 있다.
    """
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "codex_swap.core.probe.probe",
        _probe_recorder(ProbeResult.of(Usage(used_percent=64)), calls),
    )
    assert cli.main(["status", "--fresh"]) == 0
    assert cache.read(env, "a") is not None, "읽고도 버렸다"
    assert cache.read(env, "a")["usedPercent"] == 64

    capsys.readouterr()
    cli.main(["list"])
    out = capsys.readouterr().out
    assert "64%" in out and "~64%" not in out, out
    assert len(calls) == 1, "list 가 프로브를 돌렸다"


def test_status_does_not_cache_when_the_label_is_unknown(env, capsys, monkeypatch) -> None:
    """활성이 어느 슬롯과도 안 맞으면 그 값을 **어느 라벨의 것으로도** 적을 수 없다."""
    _write_auth(store.active_auth(env), "z@example.com")
    monkeypatch.setattr(
        "codex_swap.core.probe.probe",
        _probe_recorder(ProbeResult.of(Usage(used_percent=64)), []),
    )
    assert cli.main(["status", "--fresh"]) == 0
    assert cache.read_stale(env, "a") is None and cache.read_stale(env, "b") is None


# ── 진단 가능성: 원인을 말해 주는가 ─────────────────────────────────────────
#
# 아래는 전부 "고장 났다는 것은 알겠는데 무엇을 고쳐야 하는지 모른다" 는 자리다.
# 각각 격리 환경에서 직접 재현해 확인했다.


def test_a_missing_codex_is_not_reported_as_a_usage_problem(env, capsys, monkeypatch) -> None:
    """설치 문제를 "사용량 조회 실패" 로 말하면 사용자는 계정을 의심한다.

    `cmd_status` 는 `probe.probe(str(discovery.resolve_codex_bin()), …)` 로 바이너리
    해석을 프로브 호출의 **인자 안**에 두었다. 그래서 해석 실패가 같은 `except` 에
    걸려, codex 가 아예 없는 기기에서도 `usage: probe failed` 만 나왔다. 고쳐야 할 것이
    전혀 다른데 화면은 같은 말을 한다.
    """

    def missing(*a, **k):
        raise discovery.UpstreamNotFound("could not find the codex binary")

    monkeypatch.setattr("codex_swap.core.discovery.resolve_codex_bin", missing)
    monkeypatch.setattr(
        "codex_swap.core.probe.probe",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("바이너리도 없이 프로브했다")),
    )
    assert cli.main(["status", "--fresh"]) == 1
    out = capsys.readouterr().out
    assert "codex" in out and "probe failed" not in out, out


def test_add_does_not_say_the_same_thing_twice(env, capsys, monkeypatch) -> None:
    """`could not find the codex binary: could not find the codex binary` 였다."""

    def missing(*a, **k):
        raise discovery.UpstreamNotFound("could not find the codex binary")

    monkeypatch.setattr("codex_swap.core.discovery.resolve_codex_bin", missing)
    assert cli.main(["add", "fresh"]) == 1
    err = capsys.readouterr().err.strip()
    assert err.count("could not find the codex binary") == 1, err


def test_dry_run_reports_a_broken_configuration_instead_of_going_quiet(
    env, capsys, monkeypatch
) -> None:
    """`--dry-run` 은 "왜 안 바뀌나" 에 답하는 **유일한** 명령이다.

    그런데 원인이 설정일 때만 입을 닫았다 — `main` 의 fail-open 이 `command == "rotate"`
    만 보고 `--dry-run` 여부를 안 봤기 때문이다. 핫패스 침묵(계약 2)은 wrapper 가 매
    codex 호출마다 부르는 그 경로를 위한 것이지, 사람이 진단하려고 직접 친 명령에까지
    걸릴 이유가 없다.
    """
    monkeypatch.setenv("CODEX_ROTATE_MARGIN", "abc")
    assert cli.main(["rotate", "--dry-run"]) == 1
    err = capsys.readouterr().err
    assert "CODEX_ROTATE_MARGIN" in err, err


def test_plain_rotate_stays_silent_on_a_broken_configuration(env, capsys, monkeypatch) -> None:
    """반대쪽은 그대로다. 핫패스는 설정이 깨져도 조용히 무동작으로 끝난다."""
    monkeypatch.setenv("CODEX_ROTATE_MARGIN", "abc")
    assert cli.main(["rotate"]) == 1
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_a_broken_policy_file_is_not_silently_ignored(env, capsys) -> None:
    """TUI 로 저장한 정책이 조용히 무시되면, 사용자는 저장이 안 된 줄 안다.

    `_file_config` 가 `JSONDecodeError` 를 삼킨다. 그 침묵은 rotate 핫패스를 위한
    것이지만, 사람이 직접 부르는 `list` 까지 아무 말도 안 하면 기본값이 자기 설정인 줄
    안다 — 화면의 숫자가 저장한 값과 다른데 이유가 어디에도 없다.
    """
    (env.accounts_dir / "config.json").write_text("{ this is not json")
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "config.json" in out, out


def test_removing_the_active_slot_says_what_it_costs(env, capsys) -> None:
    """활성 라벨을 지우면 자동 전환이 **영구 무동작**이 된다.

    이후 `rotate` 는 `active account is not a registered slot` 으로 끝나는데, 그 사유는
    `--dry-run` 에서만 보인다. 지우는 순간에 말해 주지 않으면 사용자는 며칠 뒤에
    "왜 안 바뀌지" 로 만난다.
    """
    assert cli.main(["remove", "a"]) == 0  # a 가 활성이다
    out = capsys.readouterr().out
    assert "adopt" in out, out


def test_removing_an_idle_slot_says_nothing_extra(env, capsys) -> None:
    """정상 경로는 조용해야 한다. 늘 뜨는 경고는 곧 안 읽힌다."""
    assert cli.main(["remove", "b"]) == 0
    assert "adopt" not in capsys.readouterr().out


def test_list_warns_when_two_labels_hold_the_same_account(env, capsys) -> None:
    """같은 이메일이 두 라벨에 있으면 진 쪽 사본은 갱신을 못 받고 썩는다.

    활성 판정은 정렬 첫 일치가 이기므로(`store.active_label`), 나머지는 sync-back 대상이
    되지 않는다. 화면 어디에도 그 사실이 없어서, 사용자는 두 줄이 그냥 둘인 줄 안다.
    """
    _write_auth(store.slot_auth(env, "dup"), "a@example.com")  # a 와 같은 계정
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "a@example.com" in out
    assert "dup" in out and "same account" in out, out


def test_a_busy_lock_tells_you_what_to_do(env, capsys, monkeypatch) -> None:
    """경로만 찍혀 나왔다 — `codex-swap: /home/u/.codex/accounts/.lock`.

    TUI 는 같은 상황에 "다른 전환이 진행 중이다. 잠시 뒤 다시 눌러라" 라고 말한다.
    문구가 이미 있는데 CLI 로 오지 않았다.
    """

    def busy(*a, **k):
        raise store.LockBusy(str(env.accounts_dir / ".lock"))

    monkeypatch.setattr("codex_swap.core.store.switch_lock", busy)
    assert cli.main(["use", "b"]) == 1
    err = capsys.readouterr().err
    assert "in progress" in err, err


def test_adopt_points_a_codex_home_user_at_the_right_variable(env, capsys, monkeypatch) -> None:
    """`CODEX_HOME` 으로 홈을 옮긴 사용자는 **로그인돼 있는데** 아니라는 말을 듣는다.

    이 도구가 `CODEX_HOME` 을 따라가지 않는 것은 의도다 — 따라가면 `rotate` 의 재귀
    방어가 죽는다(`config.load` 참조). 그래서 대신 무엇을 하면 되는지 말해야 한다.
    안내가 없으면 사용자는 `codex login` 을 다시 돌리고, 그건 같은 자리에 또 쓰여
    영영 낫지 않는다.
    """
    store.active_auth(env).unlink()
    monkeypatch.setenv("CODEX_HOME", "/somewhere/else")
    assert cli.main(["adopt", "fresh"]) == 1
    err = capsys.readouterr().err
    assert "CODEX_ACCOUNT_DEFAULT_HOME=/somewhere/else" in err, err


def test_adopt_says_the_plain_thing_when_codex_home_is_not_involved(env, capsys) -> None:
    """엉뚱한 안내를 늘 붙이지는 않는다."""
    store.active_auth(env).unlink()
    assert cli.main(["adopt", "fresh"]) == 1
    err = capsys.readouterr().err
    assert "not logged in" in err and "CODEX_ACCOUNT_DEFAULT_HOME" not in err, err


# ── 슬롯 수명주기: 이름 바꾸기 · 다시 로그인 · 지우기 확인 ─────────────────
#
# 셋 다 "없어서 도구가 고장 나지는 않지만, 없으면 파괴적인 우회로밖에 없는" 자리다.


def test_rename_moves_the_slot_and_keeps_the_credentials(env, capsys) -> None:
    """이름을 바꾸려면 지금까지 `use` -> `adopt` -> `remove` 를 거쳐야 했다.

    비활성 계정은 그 우회로도 안 되고 손으로 `mv` 하는 수밖에 없었다. 파괴적 명령을
    이름 바꾸기의 필수 단계로 두면 안 된다.
    """
    assert cli.main(["rename", "b", "personal"]) == 0
    assert identity.email_of(store.slot_auth(env, "personal")) == "b@example.com"
    assert not store.slot_dir(env, "b").exists()
    assert "personal" in capsys.readouterr().out


def test_rename_refuses_to_land_on_an_existing_label(env, capsys) -> None:
    """덮어쓰면 그 계정의 보관본이 사라진다 — `adopt` 와 같은 종류의 손실이다."""
    assert cli.main(["rename", "b", "a"]) == 1
    assert identity.email_of(store.slot_auth(env, "a")) == "a@example.com"
    assert identity.email_of(store.slot_auth(env, "b")) == "b@example.com"
    assert "already exists" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["../evil", ".hidden", "a/b", ""])
def test_rename_validates_both_names(env, capsys, bad: str) -> None:
    assert cli.main(["rename", "a", bad]) == 1
    assert cli.main(["rename", bad, "fresh"]) == 1
    assert store.slot_dir(env, "a").is_dir()


def test_rename_forgets_the_cached_usage(env) -> None:
    """캐시는 라벨로 색인된다. 옛 이름에 남으면 `remove` 때와 같은 오염이 생긴다."""
    _cache_usage(env, "b", 40)
    assert cli.main(["rename", "b", "personal"]) == 0
    assert cache.read_stale(env, "b") is None


def test_rename_keeps_working_for_the_active_account(env) -> None:
    """활성 판정은 이메일로 하므로 이름이 바뀌어도 표식과 실제가 어긋나지 않는다."""
    assert cli.main(["rename", "a", "primary"]) == 0
    assert store.active_label(env) == "primary"


def test_add_force_replaces_a_rotten_slot_without_removing_it_first(env, capsys) -> None:
    """오래 안 쓴 슬롯은 토큰이 만료된다. 고치려면 `remove` 를 **먼저** 해야 했다.

    즉 되돌릴 수 없는 삭제를 하고 나서, 실패할 수 있는 브라우저 로그인을 시도하는
    순서였다. 로그인이 실패하면 아무것도 남지 않는다.
    """
    calls: list[tuple[list[str], dict[str, str]]] = []

    def runner(argv, env_):
        calls.append((argv, env_))
        _write_auth(store.slot_auth(env, "b"), "renewed@example.com")
        return 0

    assert cli.cmd_add(env, "b", force=True, runner=runner) == 0
    assert len(calls) == 1
    assert identity.email_of(store.slot_auth(env, "b")) == "renewed@example.com"


def test_add_without_force_still_refuses_an_existing_label(env, capsys) -> None:
    assert cli.main(["add", "b"]) == 1
    err = capsys.readouterr().err
    assert "already exists" in err and "--force" in err, err


def test_remove_asks_before_deleting_when_a_person_is_watching(env, capsys, monkeypatch) -> None:
    """자격증명 삭제는 되돌릴 수 없다. 사람이 보고 있으면 한 번 묻는다."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert cli.main(["remove", "b"]) == 1
    assert store.slot_auth(env, "b").is_file(), "거절했는데 지웠다"
    assert "b@example.com" in capsys.readouterr().out


def test_remove_proceeds_when_you_say_yes(env, capsys, monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert cli.main(["remove", "b"]) == 0
    assert not store.slot_dir(env, "b").exists()


def test_remove_yes_skips_the_question(env, capsys, monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(
        "builtins.input", lambda _: (_ for _ in ()).throw(AssertionError("--yes 인데 물었다"))
    )
    assert cli.main(["remove", "--yes", "b"]) == 0
    assert not store.slot_dir(env, "b").exists()


def test_remove_does_not_ask_a_script(env, capsys, monkeypatch) -> None:
    """파이프 뒤에서 물으면 영영 안 끝난다. 물을 수 없으면 묻지 않는다."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr(
        "builtins.input", lambda _: (_ for _ in ()).throw(AssertionError("tty 가 아닌데 물었다"))
    )
    assert cli.main(["remove", "b"]) == 0


# ── 기계가 읽는 출력 ────────────────────────────────────────────────────────
#
# 이 도구는 래퍼·훅에서 불리는 것이 존재 이유인데, 스크립트가 상태를 알아내려면 사람이
# 읽으라고 만든 표를 파싱해야 했다. 그 표는 폭·문구가 바뀌는 것이 정상이라 계약이 될 수
# 없다. `--json` 은 **바뀌지 않기로 한 표면**이므로 여기서 모양을 고정한다.


def test_list_json_is_a_stable_shape(env, capsys) -> None:
    _cache_usage(env, "a", 58)
    assert cli.main(["list", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)

    assert doc["active"] == "a"
    assert doc["autoSwitch"] is True
    assert doc["policy"] == {
        "ladder": [50, 70, 85, 95],
        "margin": 5,
        "cooldown": 900,
        "cacheTtl": 300,
        "checkInterval": 60,
        "busyWindow": 180,
    }
    by_label = {a["label"]: a for a in doc["accounts"]}
    assert set(by_label) == {"a", "b"}
    assert by_label["a"]["email"] == "a@example.com"
    assert by_label["a"]["usedPercent"] == 58
    assert by_label["a"]["stale"] is False
    assert by_label["a"]["active"] is True
    # 한 번도 못 읽은 슬롯은 **없는 값**이지 0 이 아니다. 0 으로 채우면 "가장 덜 쓴
    # 계정" 으로 읽혀 정반대의 판단이 나온다.
    assert by_label["b"]["usedPercent"] is None
    assert by_label["b"]["active"] is False


def test_list_json_marks_a_stale_reading(env, capsys) -> None:
    _cache_usage(env, "a", 58, age=env.cache_ttl + 10)
    cli.main(["list", "--json"])
    doc = json.loads(capsys.readouterr().out)
    entry = next(a for a in doc["accounts"] if a["label"] == "a")
    assert entry["usedPercent"] == 58 and entry["stale"] is True


def test_list_json_says_nothing_in_prose(env, capsys) -> None:
    """사람용 줄이 섞이면 `json.loads` 가 깨진다 — 표·범례·경고 전부."""
    _cache_usage(env, "a", 58, age=env.cache_ttl + 10)
    _write_auth(store.slot_auth(env, "dup"), "a@example.com")  # 중복 경고 대상
    (env.accounts_dir / "config.json").write_text("{ broken")  # 깨진 정책 경고 대상
    assert cli.main(["list", "--json"]) == 0
    json.loads(capsys.readouterr().out)  # 깨지면 여기서 실패한다


def test_list_json_on_an_empty_store_is_still_json(tmp_path, monkeypatch, capsys) -> None:
    home = tmp_path / "home"
    (home / ".codex/accounts").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_ACCOUNT_DEFAULT_HOME", str(home / ".codex"))
    monkeypatch.setenv("CODEX_ACCOUNTS_DIR", str(home / ".codex/accounts"))
    assert cli.main(["list", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["accounts"] == [] and doc["active"] is None


def test_list_json_reports_the_off_switch(env, capsys) -> None:
    env.off_switch.parent.mkdir(parents=True, exist_ok=True)
    env.off_switch.touch()
    cli.main(["list", "--json"])
    assert json.loads(capsys.readouterr().out)["autoSwitch"] is False


def test_status_json_carries_the_usage(env, capsys, monkeypatch) -> None:
    monkeypatch.setattr(
        "codex_swap.core.probe.probe",
        _probe_recorder(ProbeResult.of(Usage(used_percent=64, plan_type="pro")), []),
    )
    assert cli.main(["status", "--fresh", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["active"] == "a" and doc["usedPercent"] == 64 and doc["planType"] == "pro"
    assert doc["ok"] is True


def test_status_json_reports_a_failure_as_data(env, capsys, monkeypatch) -> None:
    """실패도 파싱 가능해야 한다. 스크립트가 stderr 문구를 읽게 두면 안 된다."""
    monkeypatch.setattr(
        "codex_swap.core.probe.probe", _probe_recorder(ProbeResult(ProbeOutcome.UNKNOWN), [])
    )
    assert cli.main(["status", "--fresh", "--json"]) == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["ok"] is False and doc["usedPercent"] is None


# ── 리셋 크레딧이 사람 눈에 닿는가 ─────────────────────────────────────────
#
# 실기기 검토에서 나온 것이다. TUI 에는 `CRED` 열이 있고 `--json` 에도 값이 있는데,
# 사람이 가장 자주 쓰는 `list`·`status` 에만 없었다. 크레딧은 **소진됐을 때의 유일한
# 탈출구**라, 하필 그것이 필요한 순간에 두 표면이 침묵한다.


def test_list_shows_reset_credits(env, capsys) -> None:
    cache.write(env, "a", {"usedPercent": 95, "resetsAt": None, "resetCredits": 2})
    cache.write(env, "b", {"usedPercent": 40, "resetsAt": None, "resetCredits": 0})
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "CRED" in out, out
    row_a = next(ln for ln in out.splitlines() if " a " in ln)
    assert "2" in row_a.split("95%")[1], row_a


def test_list_credit_column_is_a_dash_when_unknown(env, capsys) -> None:
    """0 과 "모른다" 는 다르다. 0 으로 채우면 없는 크레딧을 **없다고 단정**한다.

    `resetsAt` 를 채워 두는 것이 중요하다 — RESET 열도 비면 `-` 라서, 그냥 "행에 `-` 가
    있나" 로 보면 CRED 가 `0` 이어도 통과한다. 실제로 그렇게 뮤테이션을 놓쳤다.
    """
    import time

    cache.write(env, "a", {"usedPercent": 50, "resetsAt": int(time.time()) + 9999})
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    row = next(ln for ln in out.splitlines() if " a " in ln)
    cred = row.split("50%")[1].split()[0]
    assert cred == "-", f"CRED 칸이 {cred!r} 다: {row}"


def test_status_shows_reset_credits(env, capsys, monkeypatch) -> None:
    monkeypatch.setattr(
        "codex_swap.core.probe.probe",
        _probe_recorder(ProbeResult.of(Usage(used_percent=96, reset_credits=1)), []),
    )
    assert cli.main(["status", "--fresh"]) == 0
    assert "credits 1" in capsys.readouterr().out


def test_list_says_what_to_do_when_every_account_is_spent(env, capsys) -> None:
    """전 계정이 사다리 끝을 넘으면 자동 전환이 할 수 있는 일이 없다.

    그 사실도, 언제 풀리는지도, 크레딧이 있다는 것도 화면 어디에도 없었다 — `rotate`
    는 침묵하고 `--dry-run` 만 `ladder exhausted` 라고 답한다. 사용자는 표에 99% 두 줄만
    보고 무엇을 기다려야 하는지 모른다.
    """
    import time

    # 경계값을 피한다. `secs // 3600` 은 정확히 7200 초에서 내림으로 1 이 된다.
    soon = int(time.time()) + 7200 + 600
    cache.write(env, "a", {"usedPercent": 99, "resetsAt": soon + 90000, "resetCredits": 1})
    cache.write(env, "b", {"usedPercent": 97, "resetsAt": soon, "resetCredits": 0})
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "every account" in out, out
    assert "in 2h" in out, out  # 가장 이른 리셋
    assert "credit" in out, out  # 남은 크레딧을 쓸 수 있다는 것


def test_list_is_quiet_when_something_is_still_usable(env, capsys) -> None:
    """늘 뜨는 경고는 곧 안 읽힌다."""
    cache.write(env, "a", {"usedPercent": 99, "resetsAt": None})
    cache.write(env, "b", {"usedPercent": 40, "resetsAt": None})
    assert cli.main(["list"]) == 0
    assert "every account" not in capsys.readouterr().out


def test_list_does_not_declare_exhaustion_it_cannot_see(env, capsys) -> None:
    """`list` 는 프로브를 돌리지 않는다. **모르는 슬롯**이 있으면 단정하면 안 된다.

    아는 것만 보고 "전부 소진" 이라고 말하면, 정작 멀쩡한 슬롯을 두고 사용자가 리셋을
    기다린다. 모르는 쪽이 답일 수 있다.
    """
    cache.write(env, "a", {"usedPercent": 99, "resetsAt": None, "resetCredits": 1})
    # b 는 한 번도 못 읽었다 — 캐시에 항목이 없다.
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "?" in out
    assert "every account" not in out, out
