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
