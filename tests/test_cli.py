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
from codex_swap.core import cache, config, identity, store
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
    assert "쓸 수 없는 라벨" in capsys.readouterr().err


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
    assert "쓸 수 없는 라벨" in capsys.readouterr().err


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
    assert "전환했다: b" in capsys.readouterr().out


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
    assert "캐시가 낡았다" not in out


def test_list_still_shows_an_expired_number_marked_stale(env, capsys) -> None:
    """TTL 이 지났다고 물음표를 찍으면 안 된다. 그건 '읽지 못했다' 가 아니다."""
    _cache_usage(env, "a", 38, age=env.cache_ttl + 10)
    cli.main(["list"])
    out = capsys.readouterr().out
    assert "~38%" in out
    assert "캐시가 낡았다" in out, "범례가 없으면 `~` 를 오류로 읽는다"


def test_list_keeps_the_question_mark_for_a_slot_never_read(env, capsys) -> None:
    """한 번도 못 읽은 슬롯은 정직하게 물음표다. 지어내지 않는다."""
    cli.main(["list"])
    out = capsys.readouterr().out
    assert "?" in out and "%" not in out.split("사다리")[0]
    assert "캐시가 낡았다" not in out


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
    assert "자동 전환: 꺼짐" in out and str(env.off_switch) in out


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
    assert "삭제: a" in capsys.readouterr().out


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
    assert "활성 계정: a" in out and "사용량: 38%" in out


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
    assert "사용량: 71%" in out and "38%" not in out
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
    assert "사용량: 71%" in capsys.readouterr().out
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
    assert "z@example.com" in out and "슬롯 미등록" in out
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
    assert "사용량: 조회 실패" in capsys.readouterr().out


def test_status_survives_a_throwing_probe(env, capsys, monkeypatch) -> None:
    """프로브가 던져도 역추적이 사용자 화면으로 새면 안 된다."""

    def boom(*a, **k):
        raise RuntimeError("id_token eyJhbGciOi... 유출 금지")

    monkeypatch.setattr("codex_swap.core.probe.probe", boom)
    assert cli.main(["status", "--fresh"]) == 1
    out = capsys.readouterr()
    assert "사용량: 조회 실패" in out.out
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
    """로그인 전에는 "조회 실패" 가 아니라 "로그인하라" 여야 한다.

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
    assert "조회 실패" not in out, out
    assert "codex login" in out, out


def test_add_reports_an_unrunnable_codex_instead_of_a_traceback(env, capsys, monkeypatch) -> None:
    """실행 파일이 없으면 `subprocess` 가 `OSError` 를 던진다 — 그게 화면까지 갔다.

    `CODEX_ACCOUNT_BIN` 은 의도적으로 검증하지 않는다(핫패스). 그 대가를 `add` 가
    raw traceback 으로 치르고 있었다. `main` 의 예외 처리는 `CliError` 계열만 잡는다.
    """
    monkeypatch.setenv("CODEX_ACCOUNT_BIN", str(tmp_missing := env.accounts_dir / "no-such-codex"))
    assert not tmp_missing.exists()
    assert cli.main(["add", "fresh"]) == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "codex" in err and str(tmp_missing) in err, err
