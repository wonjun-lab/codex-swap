"""`credits use` — **되돌릴 수 없는** 유일한 동작.

이 파일이 지키는 것은 "쿠폰을 쓴다" 가 아니라 **안 쓸 때 안 쓰는 것**이다. 슬롯 삭제는
살아 있는 자격증명을 건드리지 않아 되돌릴 여지가 있지만, 쿠폰은 재발급이 없다.

그래서 여기서 재는 것 대부분은 성공 경로가 아니라 **멈추는 경로**다 — 확인 없이는 안
간다, tty 가 아니면 거부한다, 신원이 어긋나면 멈춘다, 결과를 모르면 모른다고 말한다.

실제 쿠폰은 이 파일 어디에서도 쓰이지 않는다. `probe.consume_credit` 은 전부 가짜다.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from codex_swap import cli
from codex_swap.core import cache, config, identity, probe
from codex_swap.core.types import Credit, ProbeOutcome, ProbeResult, Usage


def _auth(path: Path, email: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    claims = base64.urlsafe_b64encode(json.dumps({"email": email}).encode())
    path.write_text(json.dumps({"tokens": {"id_token": f"h.{claims.decode().rstrip('=')}.s"}}))


SOON = Credit(id="soon", status="available", expires_at=1_800_000_000, title="Full reset")
LATER = Credit(id="later", status="available", expires_at=1_900_000_000, title="Bonus reset")
BUSY = Credit(id="busy", status="redeeming", expires_at=1_700_000_000, title="Full reset")


@pytest.fixture
def env(_isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> config.Settings:
    s = config.load()
    _auth(s.accounts_dir / "master/auth.json", "a@example.com")
    _auth(s.accounts_dir / "shared/auth.json", "b@example.com")
    _auth(s.default_home / "auth.json", "a@example.com")
    monkeypatch.setattr(cli.discovery, "resolve_codex_bin", lambda: Path("/bin/true"))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    return s


@pytest.fixture
def spent(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """실제로 소비 요청이 나간 쿠폰 id. **비어 있어야 하는 테스트가 대부분이다.**"""
    calls: list[str] = []

    def fake(_bin: object, _home: object, credit_id: str, **_: object) -> probe.CreditOutcome:
        calls.append(credit_id)
        return probe.CreditOutcome.RESET

    monkeypatch.setattr(probe, "consume_credit", fake)
    return calls


def _has(monkeypatch: pytest.MonkeyPatch, *credits: Credit, count: int | None = None) -> None:
    def fake(_bin: object, home: str) -> ProbeResult:
        return ProbeResult.of(
            Usage(
                used_percent=98,
                email=identity.email_of(Path(home) / "auth.json"),
                reset_credits=len(credits) if count is None else count,
                credits=credits,
            )
        )

    monkeypatch.setattr(probe, "probe", fake)


def _answer(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    monkeypatch.setattr("builtins.input", lambda _="": text)


# ── 안 쓰는 경로 ────────────────────────────────────────────────────────────


def test_saying_anything_but_yes_spends_nothing(
    env: config.Settings, spent: list[str], monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _has(monkeypatch, SOON)
    _answer(monkeypatch, "n")
    assert cli.cmd_credits_use(env) == 1
    assert spent == []
    assert "Left it alone" in capsys.readouterr().out


@pytest.mark.parametrize("answer", ["", " ", "no", "Y ES", "yes please", "1"])
def test_only_a_bare_yes_counts(
    env: config.Settings, spent: list[str], monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    """`y`·`yes` 말고는 전부 거절이다. 되돌릴 수 없는 동작에서 관대할 이유가 없다."""
    _has(monkeypatch, SOON)
    _answer(monkeypatch, answer)
    assert cli.cmd_credits_use(env) == 1
    assert spent == []


def test_an_interrupted_prompt_spends_nothing(
    env: config.Settings, spent: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """파이프가 끊겨 `EOFError` 가 나면 그것은 승인이 아니다."""

    def eof(_: str = "") -> str:
        raise EOFError

    _has(monkeypatch, SOON)
    monkeypatch.setattr("builtins.input", eof)
    assert cli.cmd_credits_use(env) == 1
    assert spent == []


def test_a_script_without_a_terminal_is_refused(
    env: config.Settings, spent: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`remove` 는 같은 상황에 묻지 않고 진행한다. 여기는 다르다.

    슬롯 삭제는 **사본**을 지우는 것이라 살아 있는 자격증명이 남지만, 쿠폰은 재발급이
    없다. 스크립트가 실수로 태우면 되돌릴 방법이 아예 없다.
    """
    _has(monkeypatch, SOON)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(cli.CliError, match="--yes"):
        cli.cmd_credits_use(env)
    assert spent == []


def test_dry_run_spends_nothing_and_says_so(
    env: config.Settings, spent: list[str], monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _has(monkeypatch, SOON)
    assert cli.cmd_credits_use(env, dry_run=True) == 0
    assert spent == []
    out = capsys.readouterr().out
    assert "would spend" in out
    assert "nothing was spent" in out


def test_dry_run_does_not_ask(
    env: config.Settings, spent: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """묻지 않는 것이 맞다 — 아무것도 안 하는 명령에 확인을 붙이면 그 확인이 값싸진다."""

    def never(_: str = "") -> str:
        raise AssertionError("dry-run 인데 물었다")

    _has(monkeypatch, SOON)
    monkeypatch.setattr("builtins.input", never)
    assert cli.cmd_credits_use(env, dry_run=True) == 0
    assert spent == []


def test_an_account_with_no_credit_stops_before_asking(
    env: config.Settings, spent: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _has(monkeypatch, count=0)
    with pytest.raises(cli.CliError, match="no usage reset to spend"):
        cli.cmd_credits_use(env)
    assert spent == []


def test_a_count_without_usable_detail_does_not_guess(
    env: config.Settings, spent: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """개수는 있는데 상세가 없다. 지목할 수 없으면 **쓰지 않는다.**"""
    _has(monkeypatch, count=2)
    with pytest.raises(cli.CliError, match="no usable detail"):
        cli.cmd_credits_use(env)
    assert spent == []


def test_a_credit_that_is_being_redeemed_is_not_usable(
    env: config.Settings, spent: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`redeeming` 은 이미 쓰이는 중이다. 그것을 또 쓰면 하나를 헛되이 태운다."""
    _has(monkeypatch, BUSY, count=0)
    with pytest.raises(cli.CliError, match="no usage reset to spend"):
        cli.cmd_credits_use(env)
    assert spent == []


def test_naming_a_credit_that_is_not_there_spends_nothing_else(
    env: config.Settings, spent: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """지목한 것이 없다고 **아무거나** 쓰면 안 된다. 사용자는 그것을 골랐다."""
    _has(monkeypatch, SOON, LATER)
    with pytest.raises(cli.CliError, match="no usable reset with id"):
        cli.cmd_credits_use(env, credit_id="gone")
    assert spent == []


def test_the_account_changing_under_us_stops_the_spend(
    env: config.Settings, spent: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """조회와 소비 사이에 다른 rotate 가 전환을 끝내면 기본 홈에 이미 다른 계정이 있다.

    그대로 진행하면 **남의 쿠폰을 쓴다.** `credits` 가 같은 이유로 같은 검사를 한다.
    """

    def someone_else(_bin: object, _home: object) -> ProbeResult:
        return ProbeResult.of(
            Usage(used_percent=98, email="stranger@example.com", reset_credits=1, credits=(SOON,))
        )

    monkeypatch.setattr(probe, "probe", someone_else)
    with pytest.raises(cli.CliError, match="Nothing was spent"):
        cli.cmd_credits_use(env)
    assert spent == []


def test_a_slot_we_cannot_read_stops_the_spend(
    env: config.Settings, spent: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(probe, "probe", lambda *_: ProbeResult.unknown())
    with pytest.raises(cli.CliError, match="could not read"):
        cli.cmd_credits_use(env)
    assert spent == []


def test_an_unknown_label_stops_before_touching_the_network(
    env: config.Settings, spent: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def never(*_: object, **__: object) -> ProbeResult:
        raise AssertionError("없는 라벨인데 조회했다")

    monkeypatch.setattr(probe, "probe", never)
    with pytest.raises(cli.CliError, match="no such label"):
        cli.cmd_credits_use(env, "nope")
    assert spent == []


# ── 쓰는 경로 ───────────────────────────────────────────────────────────────


def test_yes_spends_the_credit_that_expires_first(
    env: config.Settings, spent: list[str], monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """먼저 잃을 것부터 쓴다. 어차피 곧 사라질 것이다."""
    _has(monkeypatch, LATER, SOON)
    _answer(monkeypatch, "y")
    assert cli.cmd_credits_use(env) == 0
    assert spent == ["soon"]
    assert "spent:" in capsys.readouterr().out


def test_a_credit_with_no_expiry_is_not_treated_as_urgent(
    env: config.Settings, spent: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """모르는 것을 급한 것으로 취급하면, 날짜가 찍힌 쿠폰을 놔두고 정체 모를 것을 태운다."""
    nameless = Credit(id="nameless", status="available", expires_at=None)
    _has(monkeypatch, nameless, LATER)
    assert cli.cmd_credits_use(env, assume_yes=True) == 0
    assert spent == ["later"]


def test_naming_a_credit_overrides_the_choice(
    env: config.Settings, spent: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _has(monkeypatch, SOON, LATER)
    assert cli.cmd_credits_use(env, credit_id="later", assume_yes=True) == 0
    assert spent == ["later"]


def test_the_confirmation_names_the_account_and_the_expiry(
    env: config.Settings, spent: list[str], monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """라벨만 보여 주면 어느 계정인지 확신할 수 없다. 만료일이 없으면 왜 이것인지 모른다."""
    seen: list[str] = []
    _has(monkeypatch, SOON)
    monkeypatch.setattr("builtins.input", lambda _="": "y")
    cli.cmd_credits_use(env)
    seen.append(capsys.readouterr().out)
    assert "master" in seen[0]
    assert "a@example.com" in seen[0]
    assert "Full reset" in seen[0]
    assert "cannot be undone" in seen[0]


def test_spending_uses_the_active_account_by_default(
    env: config.Settings, spent: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """쿠폰을 쓰고 싶은 순간은 대개 **지금 쓰는 계정**이 막힌 때다."""
    homes: list[str] = []

    def record(_bin: object, home: str) -> ProbeResult:
        homes.append(home)
        return ProbeResult.of(
            Usage(
                used_percent=98,
                email=identity.email_of(Path(home) / "auth.json"),
                reset_credits=1,
                credits=(SOON,),
            )
        )

    monkeypatch.setattr(probe, "probe", record)
    cli.cmd_credits_use(env, assume_yes=True)
    assert homes == [str(env.default_home)], homes


def test_naming_an_inactive_label_reads_its_slot_copy(
    env: config.Settings, spent: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    homes: list[str] = []

    def record(_bin: object, home: str) -> ProbeResult:
        homes.append(home)
        return ProbeResult.of(
            Usage(
                used_percent=98,
                email=identity.email_of(Path(home) / "auth.json"),
                reset_credits=1,
                credits=(SOON,),
            )
        )

    monkeypatch.setattr(probe, "probe", record)
    cli.cmd_credits_use(env, "shared", assume_yes=True)
    assert homes == [str(env.accounts_dir / "shared")], homes


# ── 결과를 어떻게 읽는가 ────────────────────────────────────────────────────


def _outcome(monkeypatch: pytest.MonkeyPatch, value: probe.CreditOutcome) -> None:
    monkeypatch.setattr(probe, "consume_credit", lambda *_, **__: value)


def test_already_redeemed_is_not_reported_as_success(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _has(monkeypatch, SOON)
    _outcome(monkeypatch, probe.CreditOutcome.ALREADY_REDEEMED)
    assert cli.cmd_credits_use(env, assume_yes=True) == 1
    assert "already redeemed" in capsys.readouterr().out


def test_nothing_to_reset_says_the_credit_is_still_yours(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """쓸 것이 없어서 안 썼다. 이것을 실패로만 적으면 사용자는 쿠폰을 잃은 줄 안다."""
    _has(monkeypatch, SOON)
    _outcome(monkeypatch, probe.CreditOutcome.NOTHING_TO_RESET)
    assert cli.cmd_credits_use(env, assume_yes=True) == 1
    assert "still yours" in capsys.readouterr().out


def test_no_credit_says_nothing_was_spent_not_that_it_might_have_been(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """**확정된 사실을 불확실로 접으면 안 된다.**

    `UNKNOWN` 을 실패로 접으면 안 되는 것과 같은 무게의, 반대 방향 오분류다. 이 갈래가
    열거형에 없던 동안 `UNKNOWN` 으로 떨어져 "썼는지 모른다" 고 안내했다.
    """
    _has(monkeypatch, SOON)
    _outcome(monkeypatch, probe.CreditOutcome.NO_CREDIT)
    assert cli.cmd_credits_use(env, assume_yes=True) == 1
    said = capsys.readouterr().out
    assert "Nothing was spent" in said, said
    assert "may or may not" not in said, said


def test_an_unreadable_outcome_does_not_claim_it_failed(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """`UNKNOWN` 을 실패로 적으면 사용자가 하나 더 쓴다.

    되돌릴 수 없는 동작에서 그 오분류의 대가가 가장 크다.
    """
    _has(monkeypatch, SOON)
    _outcome(monkeypatch, probe.CreditOutcome.UNKNOWN)
    assert cli.cmd_credits_use(env, assume_yes=True) == 1
    out = capsys.readouterr().out
    assert "may or may not" in out
    assert "check before trying again" in out.lower()


def test_a_transport_failure_says_it_could_not_spend(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """확정된 실패는 `UNKNOWN` 과 다르다 — 쓰지도 않은 쿠폰을 잃었다고 믿게 하면 안 된다."""

    def boom(*_: object, **__: object) -> probe.CreditOutcome:
        raise probe.ProbeError("chatgpt authentication required")

    _has(monkeypatch, SOON)
    monkeypatch.setattr(probe, "consume_credit", boom)
    with pytest.raises(cli.CliError, match="could not spend"):
        cli.cmd_credits_use(env, assume_yes=True)


# ── 캐시 ────────────────────────────────────────────────────────────────────


def test_a_spent_credit_clears_the_stale_usage(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """쿠폰이 먹으면 사용량 창이 방금 바뀌었는데 캐시에는 쓰기 직전 숫자가 남는다.

    `rotate` 는 그것을 정책 입력으로 읽으므로, 지우지 않으면 방금 되살린 계정을 최대 TTL
    동안 소진된 것으로 취급한다.
    """
    cache.write(env, "master", {"usedPercent": 98}, now=1000)
    _has(monkeypatch, SOON)
    _outcome(monkeypatch, probe.CreditOutcome.RESET)
    cli.cmd_credits_use(env, assume_yes=True)
    assert cache.read_stale(env, "master") is None


def test_an_unknown_outcome_also_clears_the_cache(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """썼는지 모르는 상태에서 낡은 숫자를 믿는 것이 더 나쁘다."""
    cache.write(env, "master", {"usedPercent": 98}, now=1000)
    _has(monkeypatch, SOON)
    _outcome(monkeypatch, probe.CreditOutcome.UNKNOWN)
    cli.cmd_credits_use(env, assume_yes=True)
    assert cache.read_stale(env, "master") is None


@pytest.mark.parametrize(
    "outcome", [probe.CreditOutcome.NOTHING_TO_RESET, probe.CreditOutcome.NO_CREDIT]
)
def test_the_outcomes_that_spent_nothing_keep_the_cache(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch, outcome: probe.CreditOutcome
) -> None:
    """아무 일도 안 일어났다. 지우면 다음 호출이 이유 없이 프로브를 한 번 더 한다."""
    cache.write(env, "master", {"usedPercent": 98}, now=1000)
    _has(monkeypatch, SOON)
    _outcome(monkeypatch, outcome)
    cli.cmd_credits_use(env, assume_yes=True)
    assert cache.read_stale(env, "master") is not None


# ── 배선 ────────────────────────────────────────────────────────────────────


def test_the_command_line_reaches_the_spend(
    env: config.Settings, spent: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """함수를 직접 부르는 테스트만 있으면 배선이 끊겨도 전부 통과한다."""
    _has(monkeypatch, SOON)
    assert cli.main(["credits", "use", "--yes"]) == 0
    assert spent == ["soon"]


def test_the_command_line_dry_run_spends_nothing(
    env: config.Settings, spent: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _has(monkeypatch, SOON)
    assert cli.main(["credits", "use", "--dry-run"]) == 0
    assert spent == []


def test_a_stray_word_after_credits_is_rejected_not_guessed(
    env: config.Settings, spent: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`codex-swap credits master` 는 뜻이 둘로 읽힌다 — 목록인가, 그 계정에 쓰는 것인가.

    argparse 가 거절하는 것이 맞다. 조용히 목록으로 처리하면 "쓰려 했는데 안 썼다" 를
    사용자가 모르고, 반대로 알아서 쓰면 **묻지도 않고 태운다.**
    """
    _has(monkeypatch, SOON)
    with pytest.raises(SystemExit) as exc:
        cli.main(["credits", "master"])
    assert exc.value.code == 2
    assert spent == []


def test_probe_refuses_an_empty_credit_id() -> None:
    """빈 문자열을 서버까지 보내면 무엇이 일어날지 모른다. 여기서 막는다."""
    with pytest.raises(probe.ProbeError, match="must not be empty"):
        probe.consume_credit("/bin/true", None, "")


def test_the_outcome_enum_covers_what_the_server_names() -> None:
    """서버가 쓰는 문자열이 이 열거형과 같아야 파싱이 성립한다.

    `noCredit` 은 한동안 빠져 있었다. 서버는 처음부터 보내고 있었고, 우리는 그것을 "모르는
    이름" 으로 접어 `UNKNOWN` 이라고 적었다 — 아무 일도 없었는데 "썼는지 모른다" 가 된다.
    """
    assert {o.value for o in probe.CreditOutcome} >= {
        "reset",
        "nothingToReset",
        "noCredit",
        "alreadyRedeemed",
    }


def test_probe_outcome_is_untouched_by_the_new_enum() -> None:
    """이름이 비슷한 두 열거형이 섞이면 결과 판정이 통째로 어긋난다."""
    assert probe.CreditOutcome is not ProbeOutcome


# ── `_consume` 자체 (뮤테이션에서 아무도 안 지나던 자리) ────────────────────


class _FakeConn:
    """`_Conn` 자리에 끼워 넣는 가짜. 프로세스도 파이프도 없다.

    `consume_credit` 만 가짜로 두면 `_consume` 안의 판정 — 오류를 올릴지, 모르는 결과를
    어떻게 접을지 — 을 **아무도 지나지 않는다.** 실제로 뮤테이션 둘이 그렇게 살아남았다.
    """

    def __init__(self, reply: dict[str, object]) -> None:
        self._reply = reply
        self.sent: list[tuple[str, object]] = []

    def request(self, _id: int, method: str, params: object = None) -> dict[str, object]:
        self.sent.append((method, params))
        if method.endswith("/consume"):
            return self._reply
        return {"result": {}}

    def notify(self, method: str, params: object = None) -> None:
        self.sent.append((method, params))


def _reply(monkeypatch: pytest.MonkeyPatch, doc: dict[str, object]) -> _FakeConn:
    conn = _FakeConn(doc)
    monkeypatch.setattr(probe, "_Conn", lambda *_, **__: conn)
    return conn


@pytest.mark.parametrize(
    ("outcome", "want"),
    [
        ("reset", probe.CreditOutcome.RESET),
        ("nothingToReset", probe.CreditOutcome.NOTHING_TO_RESET),
        ("noCredit", probe.CreditOutcome.NO_CREDIT),
        ("alreadyRedeemed", probe.CreditOutcome.ALREADY_REDEEMED),
    ],
)
def test_consume_reads_the_outcome_the_server_names(
    monkeypatch: pytest.MonkeyPatch, outcome: str, want: probe.CreditOutcome
) -> None:
    _reply(monkeypatch, {"result": {"outcome": outcome}})
    assert probe._consume(None, "cid", 1.0) is want


@pytest.mark.parametrize(
    "result",
    [
        {"outcome": "somethingBrandNew"},
        {"outcome": None},
        {"outcome": 5},
        {"outcome": {}},
        {},
        None,
    ],
)
def test_an_outcome_we_do_not_recognise_is_unknown_not_success(
    monkeypatch: pytest.MonkeyPatch, result: object
) -> None:
    """서버가 새 갈래를 추가할 수 있다. 모르는 것을 **성공으로 적으면 안 된다** —
    사용자는 사용량이 리셋된 줄 알고 계속 쓰다가 다시 막힌다.
    """
    _reply(monkeypatch, {"result": result})
    assert probe._consume(None, "cid", 1.0) is probe.CreditOutcome.UNKNOWN


def test_a_server_error_is_raised_not_folded_into_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """확정된 실패와 "모른다" 는 다르다.

    인증이 끊겨서 못 쓴 것을 `UNKNOWN` 으로 접으면, 쓰지도 않은 쿠폰을 잃었다고 믿는다.
    """
    _reply(monkeypatch, {"error": {"message": "chatgpt authentication required"}})
    with pytest.raises(probe.ProbeError, match="authentication required"):
        probe._consume(None, "cid", 1.0)


def test_consume_sends_the_credit_id_the_caller_named(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _reply(monkeypatch, {"result": {"outcome": "reset"}})
    probe._consume(None, "cred_abc", 1.0)
    method, params = next(x for x in conn.sent if str(x[0]).endswith("/consume"))
    assert method == "account/rateLimitResetCredit/consume"
    assert params["creditId"] == "cred_abc"


def test_consume_sends_an_idempotency_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """서버가 **필수**로 요구한다. 빠뜨려서 리셋을 아예 못 쓴 적이 있다.

    그때 이 자리를 지키던 테스트는 `params == {"creditId": ...}` 를 기대했다. 필드가 하나도
    없는 것이 정답이라고 못박아 둔 셈이라, 서버가 요구하는 것을 안 보내는 상태를 **통과**
    시켰다. 페이로드 검사는 "우리가 보내는 것" 이 아니라 "받는 쪽이 요구하는 것" 을 기준으로
    적어야 한다.
    """
    conn = _reply(monkeypatch, {"result": {"outcome": "reset"}})
    probe._consume(None, "cred_abc", 1.0)
    _, params = next(x for x in conn.sent if str(x[0]).endswith("/consume"))
    assert set(params) == {"creditId", "idempotencyKey"}
    # 스키마가 UUID 를 권한다. 형식이 깨지면 서버가 다시 `Invalid request` 로 돌려보낸다.
    assert uuid.UUID(params["idempotencyKey"])


def test_the_same_credit_keeps_the_same_attempt_key() -> None:
    """`UNKNOWN` 뒤의 재시도가 **쿠폰을 하나 더 태우지 않게** 하는 것이 이 키의 전부다.

    요청은 나갔는데 결과를 못 읽은 자리라 실제로는 이미 쓰였을 수 있다. 매번 새 키를 만들면
    서버는 그것을 별개의 시도로 보고 두 번째를 태운다.
    """
    assert probe.attempt_key("cred_abc") == probe.attempt_key("cred_abc")


def test_a_different_credit_gets_a_different_attempt_key() -> None:
    """반대쪽 실패도 막아야 한다. 키가 고정되면 **다른 쿠폰**을 쓰려는 것까지 접힌다."""
    assert probe.attempt_key("cred_abc") != probe.attempt_key("cred_xyz")


def test_the_attempt_key_survives_a_restart() -> None:
    """프로세스가 죽었다 살아나도, `cli` 와 `tui` 사이에서도 같아야 한다.

    난수를 기억해 두는 방식이었다면 이 성질이 저장 위치와 그 파일의 수명에 달렸을 것이다.
    쿠폰 id 에서 유도하면 아무 상태 없이 나온다 — 별도 프로세스에서 같은 값이 나온다는 것이
    그 증거다.
    """
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "from codex_swap.core import probe; print(probe.attempt_key('cred_abc'))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == probe.attempt_key("cred_abc")


def test_consume_refuses_an_empty_credit_before_naming_an_attempt() -> None:
    """빈 id 로는 키도 만들 수 없다. 서버는 `creditId` 가 없으면 **아무거나 고른다.**"""
    with pytest.raises(probe.ProbeError):
        probe.attempt_key("")


def test_consume_greets_the_server_before_asking(monkeypatch: pytest.MonkeyPatch) -> None:
    """`initialize` → `initialized` 는 app-server 의 전제다. 빠뜨리면 요청이 거절된다."""
    conn = _reply(monkeypatch, {"result": {"outcome": "reset"}})
    probe._consume(None, "cid", 1.0)
    assert [m for m, _ in conn.sent][:3] == [
        "initialize",
        "initialized",
        "account/rateLimitResetCredit/consume",
    ]


def test_the_command_line_asks_when_yes_was_not_given(
    env: config.Settings, spent: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--yes` 배선이 끊겨 늘 승인이 되어도, `--yes` 를 **주는** 테스트만으로는 못 잡는다."""
    _has(monkeypatch, SOON)
    _answer(monkeypatch, "n")
    assert cli.main(["credits", "use"]) == 1
    assert spent == []


# ── codex 교차 검토가 지목한 것들 ───────────────────────────────────────────


def test_a_lost_answer_is_unknown_not_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """**요청이 나간 뒤** 응답이 끊긴 경우.

    타임아웃·EOF·깨진 응답은 "못 썼다" 가 아니라 "썼는지 모른다" 다 — 서버는 이미
    처리했을 수 있다. 확정 실패로 적으면 사용자가 다시 시도해 하나 더 태운다.

    앞선 테스트들은 `UNKNOWN` 을 **받았을 때** 무엇을 하는지만 쟀다. 실제 불명 상황에서
    `UNKNOWN` 이 **만들어지는지**는 아무도 안 봤다.
    """

    class Gone:
        def __init__(self) -> None:
            self.sent: list[str] = []

        def request(self, _id: int, method: str, _params: object = None) -> dict[str, object]:
            self.sent.append(method)
            if method.endswith("/consume"):
                raise probe.ProbeError("timeout: account/rateLimitResetCredit/consume")
            return {"result": {}}

        def notify(self, method: str, _params: object = None) -> None:
            self.sent.append(method)

    conn = Gone()
    monkeypatch.setattr(probe, "_Conn", lambda *_, **__: conn)
    assert probe._consume(None, "cid", 1.0) is probe.CreditOutcome.UNKNOWN
    assert any(m.endswith("/consume") for m in conn.sent), "요청조차 안 나갔다"


def test_a_greeting_that_fails_is_still_a_definite_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`initialize` 는 소비 요청 **전**이다. 거기서 죽으면 쿠폰은 확실히 그대로다.

    이것까지 `UNKNOWN` 으로 접으면 "썼는지 모른다" 가 남발되어 그 문구가 값싸진다.
    """

    class Deaf:
        def request(self, _id: int, method: str, _params: object = None) -> dict[str, object]:
            raise probe.ProbeError(f"timeout: {method}")

        def notify(self, _method: str, _params: object = None) -> None:
            pass

    monkeypatch.setattr(probe, "_Conn", lambda *_, **__: Deaf())
    with pytest.raises(probe.ProbeError, match="initialize"):
        probe._consume(None, "cid", 1.0)


@pytest.mark.parametrize("label", ["", " ", "\t"])
def test_an_explicitly_empty_label_is_refused_not_swapped_for_the_active_one(
    env: config.Settings, spent: list[str], monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    """`None`(안 적었다)과 `""`(빈 값을 적었다)는 다르다.

    스크립트가 비어 있는 변수를 따옴표로 감싸 넘기면 뒤엣것이 된다. `label or active` 로
    두면 그것이 조용히 활성 계정이 되어, **지목하지도 않은 계정**의 쿠폰을 태운다.
    """
    _has(monkeypatch, SOON)
    with pytest.raises(cli.CliError, match="empty label"):
        cli.cmd_credits_use(env, label, assume_yes=True)
    assert spent == []


def test_an_account_that_will_not_say_who_it_is_does_not_get_charged(
    env: config.Settings, spent: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """조회 화면은 "알 때만 견준다" — 모른다고 갱신을 멈추면 화면이 빈다.

    여기는 반대다. 누구 것인지 확인 못 한 채 태우는 것보다 거절하는 편이 낫다. 이메일이
    비는 응답에서 검사가 **열려** 있으면, 하필 전환이 끼어든 순간에 남의 쿠폰이 승인된
    것으로 처리된다.
    """

    def nameless(_bin: object, _home: object) -> ProbeResult:
        return ProbeResult.of(Usage(used_percent=98, email=None, reset_credits=1, credits=(SOON,)))

    monkeypatch.setattr(probe, "probe", nameless)
    with pytest.raises(cli.CliError, match="could not confirm whose usage reset"):
        cli.cmd_credits_use(env, assume_yes=True)
    assert spent == []


def test_the_account_switching_while_we_wait_for_yes_stops_the_spend(
    env: config.Settings, spent: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """확인을 기다리는 동안 rotate 가 전환을 끝낼 수 있다.

    사용자가 승인한 것은 A 인데 소비 요청은 B 의 자격증명으로 나간다. 조회 시점의 검사만
    있으면 이 창이 열려 있다 — 소비 **직전에** 다시 봐야 한다.
    """
    _has(monkeypatch, SOON)

    def switch_then_yes(_: str = "") -> str:
        _auth(env.default_home / "auth.json", "someone-else@example.com")
        return "y"

    monkeypatch.setattr("builtins.input", switch_then_yes)
    with pytest.raises(cli.CliError, match="Nothing was spent"):
        cli.cmd_credits_use(env)
    assert spent == []


def test_a_switch_in_progress_stops_the_spend(
    env: config.Settings, spent: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """락을 못 잡으면 다른 전환이 도는 중이다. 그 위에 소비를 겹치지 않는다."""
    _has(monkeypatch, SOON)

    def busy(_: config.Settings):
        raise probe_store_lock_busy()

    monkeypatch.setattr(cli.store, "switch_lock", busy)
    with pytest.raises(cli.CliError, match="Nothing was spent"):
        cli.cmd_credits_use(env, assume_yes=True)
    assert spent == []


def probe_store_lock_busy() -> Exception:
    from codex_swap.core import store

    return store.LockBusy("held")


def test_an_expired_credit_is_never_the_automatic_choice(
    env: config.Settings, spent: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """만료가 이른 것부터 쓰는 규칙을 그대로 두면 **이미 지난 쿠폰이 가장 먼저** 뽑힌다.

    정렬 키가 가장 작기 때문이다. 서버가 `available` 이라고 적어 둔 채 날짜만 지난
    항목이 실제로 그렇게 뽑혔다 — codex 교차 검토가 잡았다.
    """
    import time as _time

    dead = Credit(id="dead", status="available", expires_at=int(_time.time()) - 100)
    alive = Credit(id="alive", status="available", expires_at=int(_time.time()) + 86400)
    _has(monkeypatch, dead, alive)
    assert cli.cmd_credits_use(env, assume_yes=True) == 0
    assert spent == ["alive"]


def test_every_usable_credit_being_expired_spends_nothing(
    env: config.Settings, spent: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import time as _time

    dead = Credit(id="dead", status="available", expires_at=int(_time.time()) - 100)
    _has(monkeypatch, dead)
    # **"상세가 없다" 와 갈려야 한다.** 그쪽은 다시 시도하라는 뜻인데, 전부 만료는 아무리
    # 다시 해도 같다.
    with pytest.raises(cli.CliError, match="have all expired"):
        cli.cmd_credits_use(env, assume_yes=True)
    assert spent == []


def test_naming_an_expired_credit_is_still_allowed(
    env: config.Settings, spent: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """지목한 것은 만료 여부를 따지지 않는다.

    사용자가 보고 골랐고 서버는 여전히 `available` 이라고 말한다 — 우리 시계가 틀렸을
    수도 있다. 자동으로 고르지 않는 것과 못 고르게 막는 것은 다르다.
    """
    import time as _time

    dead = Credit(id="dead", status="available", expires_at=int(_time.time()) - 100)
    _has(monkeypatch, dead)
    assert cli.cmd_credits_use(env, credit_id="dead", assume_yes=True) == 0
    assert spent == ["dead"]
