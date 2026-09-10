"""`credits` — 쿠폰의 개수만이 아니라 **언제까지인지**.

`list` 의 `CRED` 열은 개수만 말한다. 쿠폰은 만료되는 자원이라 그것만으로는 쓸지 말지를
정할 수 없다 — 오늘 밤 사라질 쿠폰과 두 달 남은 쿠폰이 같은 `1` 로 보인다.

이 화면이 캐시를 안 쓰는 것은 의도다. 상세를 캐시에 얹으려면 `resetCredits` 를 쓰는 여섯
자리에 키를 하나씩 더 넣어야 하고, 그중 한 곳이 빠져서 **캐시 히트일 때만** 크레딧이
사라진 적이 이미 있다.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import pytest

from codex_swap import cli
from codex_swap.core import config, identity, probe
from codex_swap.core.types import Credit, ProbeResult, Usage


def _auth(path: Path, email: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    claims = base64.urlsafe_b64encode(json.dumps({"email": email}).encode())
    path.write_text(json.dumps({"tokens": {"id_token": f"h.{claims.decode().rstrip('=')}.s"}}))


@pytest.fixture
def env(_isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> config.Settings:
    s = config.load()
    _auth(s.accounts_dir / "master/auth.json", "a@example.com")
    _auth(s.accounts_dir / "shared/auth.json", "b@example.com")
    _auth(s.default_home / "auth.json", "a@example.com")
    monkeypatch.setattr(cli.discovery, "resolve_codex_bin", lambda: Path("/bin/true"))
    return s


def _usage(email: str, *credits: Credit, count: int | None = None) -> Usage:
    return Usage(
        used_percent=50,
        email=email,
        reset_credits=len(credits) if count is None else count,
        credits=credits,
    )


def _answer(mapping: dict[str, Usage | None], monkeypatch: pytest.MonkeyPatch) -> None:
    """홈 경로가 아니라 **그 홈의 이메일**로 대답을 고른다.

    `_probe_credits` 는 활성 슬롯만 기본 홈으로 읽으므로, 경로로 짝지으면 활성이 바뀔 때
    테스트가 조용히 다른 것을 재게 된다.
    """

    def fake(_bin: str, home: str) -> ProbeResult:
        email = identity.email_of(Path(home) / "auth.json")
        usage = mapping.get(email or "")
        if usage is None:
            return ProbeResult.unknown()
        return ProbeResult.of(usage)

    monkeypatch.setattr(probe, "probe", fake)


DAY = 86400


def _credit_cell(out: str) -> str:
    """활성 행의 **CREDIT 열**. 열을 집지 않으면 엉뚱한 칸을 보고 통과한다.

    처음에 `row.split()[-1]` 로 썼다가 뮤테이션이 살아남았다 — 그건 EXPIRES 열이라
    CREDIT 이 `-` 에서 `0` 으로 바뀌어도 그대로 통과했다.
    """
    row = next(ln for ln in out.splitlines() if ln.split()[:1] == ["*"])
    return row.split()[3]


def test_the_expiry_is_shown_not_just_the_count(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """개수만으로는 쓸지 말지를 정할 수 없다."""
    _answer(
        {
            "a@example.com": _usage(
                "a@example.com",
                Credit(id="c1", status="available", expires_at=2_000_000_000, title="Full reset"),
            ),
            "b@example.com": _usage("b@example.com"),
        },
        monkeypatch,
    )
    assert cli.cmd_credits(env) == 0
    out = capsys.readouterr().out
    assert "Full reset" in out
    assert "EXPIRES" in out
    assert "05-18" in out, out  # 2_000_000_000 -> 2033-05-18 (로컬)


def test_one_row_per_credit_so_two_expiries_do_not_collapse(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """쿠폰 둘의 만료가 다르면 `2` 한 칸으로는 그 차이가 사라진다."""
    _answer(
        {
            "a@example.com": _usage(
                "a@example.com",
                Credit(id="c1", status="available", expires_at=2_000_000_000, title="Full reset"),
                Credit(id="c2", status="available", expires_at=2_100_000_000, title="Full reset"),
            ),
            "b@example.com": _usage("b@example.com"),
        },
        monkeypatch,
    )
    cli.cmd_credits(env)
    lines = [ln for ln in capsys.readouterr().out.splitlines() if "Full reset" in ln]
    assert len(lines) == 2, lines
    # 라벨은 첫 줄에만. 두 번 적으면 계정이 둘인 것처럼 읽힌다.
    assert lines[1].split()[0] != "master", lines


def test_a_slot_we_could_not_read_is_named_not_guessed(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """`0` 으로 채우면 없는 쿠폰을 **없다고 단정**한다. 못 읽은 것과 없는 것은 다르다."""
    _answer({"a@example.com": _usage("a@example.com")}, monkeypatch)  # shared 는 대답 없음
    cli.cmd_credits(env)
    out = capsys.readouterr().out
    assert "could not read: shared" in out, out
    assert "?" in out, out


def test_a_credit_from_another_account_is_not_credited_to_this_label(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """조회 도중 다른 rotate 가 전환을 끝내면 기본 홈에 이미 다른 계정이 들어 있다.

    그대로 적으면 남의 쿠폰이 이 이름으로 화면에 뜬다 — `refresh_all` 이 같은 이유로
    같은 검사를 한다.
    """

    def wrong(_bin: str, _home: str) -> ProbeResult:
        return ProbeResult.of(
            _usage("someone-else@example.com", Credit(id="x", status="available"))
        )

    monkeypatch.setattr(probe, "probe", wrong)
    cli.cmd_credits(env)
    out = capsys.readouterr().out
    assert "someone-else" not in out, out
    assert "could not read: master, shared" in out, out


def test_the_screen_says_that_spending_cannot_be_undone(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """되돌릴 수 없는 자원이라는 사실이 화면에 있어야 한다."""
    _answer({"a@example.com": _usage("a@example.com")}, monkeypatch)
    cli.cmd_credits(env)
    assert "cannot be undone" in capsys.readouterr().out


def test_no_accounts_points_at_adopt(_isolated_home: Path, capsys) -> None:
    assert cli.cmd_credits(config.load()) == 0
    assert "codex-swap adopt" in capsys.readouterr().out


# ── 기계용 출력 ──────────────────────────────────────────────────────────────


def test_the_json_carries_the_id_so_a_later_command_can_name_a_credit(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _answer(
        {
            "a@example.com": _usage(
                "a@example.com",
                Credit(id="cred_123", status="available", expires_at=2_000_000_000),
            ),
            "b@example.com": _usage("b@example.com"),
        },
        monkeypatch,
    )
    assert cli.cmd_credits_json(env) == 0
    doc = json.loads(capsys.readouterr().out)
    master = next(a for a in doc["accounts"] if a["label"] == "master")
    assert master["credits"][0]["id"] == "cred_123"
    assert master["active"] is True
    assert master["readable"] is True


def test_the_json_says_unreadable_rather_than_zero(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """`availableCount: 0` 은 "쿠폰이 없다" 로 읽힌다. 못 읽은 것은 `null` 이어야 한다."""
    _answer({"a@example.com": _usage("a@example.com")}, monkeypatch)
    cli.cmd_credits_json(env)
    doc = json.loads(capsys.readouterr().out)
    shared = next(a for a in doc["accounts"] if a["label"] == "shared")
    assert shared["availableCount"] is None
    assert shared["readable"] is False
    assert shared["credits"] == []


# ── 만료 표기 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("delta", "want"),
    [
        (30 * 60, "in 30m"),
        (5 * 3600, "in 5h"),
        (3 * DAY, "in 3d"),
        (-60, "expired"),
    ],
)
def test_expiry_reads_the_way_a_person_asks_it(delta: int, want: str) -> None:
    now = 1_700_000_000
    assert want in cli._expiry_text(now + delta, now=now)


def test_an_expired_credit_does_not_say_past(_isolated_home: Path) -> None:
    """사용량 리셋이 지난 것은 좋은 일이고 쿠폰이 지난 것은 **잃은 것**이다.

    둘 다 `past` 로 적으면 그 차이가 사라진다.
    """
    now = 1_700_000_000
    assert "expired" in cli._expiry_text(now - 1, now=now)
    assert "past" in cli._reset_text(now - 1, now=now)


@pytest.mark.parametrize("value", [None, True, False, "soon", [], {}])
def test_an_expiry_we_cannot_read_is_a_dash(value: object) -> None:
    """서버가 안 주거나 모양이 다를 수 있다. 그때 지어내지 않는다."""
    assert cli._expiry_text(value) == "-"


# ── 파싱 ────────────────────────────────────────────────────────────────────


def test_a_credit_without_a_usable_id_is_dropped() -> None:
    """지목할 수 없는 항목은 목록에 있어 봐야 사용자를 오해시킨다."""
    node = {
        "credits": [
            {"id": "", "status": "available"},
            {"status": "available"},
            {"id": 7, "status": "available"},
            {"id": "keep", "status": "available"},
        ]
    }
    got = probe._credits(node)
    assert [c.id for c in got] == ["keep"]


def test_the_count_comes_from_the_server_not_from_the_list_length() -> None:
    """개수와 목록은 **따로** 읽는다.

    둘이 어긋날 수 있다 — 목록에 만료된 것이 남아 있거나 세는 규칙이 서버 쪽에서 바뀌면.
    그때 목록에서 다시 세면 화면이 서버와 다른 말을 한다. 개수는 서버가 준 것을 쓴다.
    """
    node = {"availableCount": 1, "credits": [{"id": "a"}, {"id": "b"}]}
    assert probe._as_count(node["availableCount"]) == 1
    assert len(probe._credits(node)) == 2


@pytest.mark.parametrize("node", [None, {}, {"credits": None}, {"credits": "x"}, {"credits": {}}])
def test_a_shape_we_do_not_recognise_yields_no_credits(node: object) -> None:
    assert probe._credits(node) == ()


def test_a_count_without_detail_is_still_shown(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """서버가 개수만 주고 목록을 안 줄 수 있다.

    codex 자신이 그 경우를 갖고 있다 — "rate limit reset credit detail request timed out;
    falling back to the usage response". 그때 목록 길이로 세면 **2 개 있는데 0 이라고**
    적는다. 사용자는 쿠폰이 사라진 줄 안다.
    """
    _answer({"a@example.com": _usage("a@example.com", count=2)}, monkeypatch)
    cli.cmd_credits(env)
    assert _credit_cell(capsys.readouterr().out) == "2"


def test_a_count_we_do_not_know_is_a_dash_not_a_zero(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _answer(
        {"a@example.com": Usage(used_percent=50, email="a@example.com", reset_credits=None)},
        monkeypatch,
    )
    cli.cmd_credits(env)
    assert _credit_cell(capsys.readouterr().out) == "-"


def test_credit_detail_never_reaches_the_cache(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """상세를 캐시에 얹는 순간 쓰는 자리가 여섯 개 늘고, 그중 하나가 빠지는 날이 온다.

    실제로 `resetCredits` 하나가 그렇게 빠져서 **캐시 히트일 때만** 크레딧이 사라졌다.
    """
    _answer(
        {
            "a@example.com": _usage(
                "a@example.com", Credit(id="secret_credit_id", status="available")
            ),
            "b@example.com": _usage("b@example.com"),
        },
        monkeypatch,
    )
    cli.refresh_all(env)
    raw = (env.accounts_dir / ".usage-cache.json").read_text()
    assert "secret_credit_id" not in raw, raw
    assert "credits" not in raw, raw


def test_the_server_count_is_not_lost_when_the_list_is_shown(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """목록을 그리면 줄 수가 곧 개수처럼 읽힌다.

    만료됐거나 `redeeming` 인 쿠폰이 섞이면 그 둘이 다르다 — 세 줄이 보이는데 쓸 수 있는
    것은 하나일 수 있다. 그때 아무 말이 없으면 사용자는 셋이라고 믿는다.
    """
    _answer(
        {
            "a@example.com": _usage(
                "a@example.com",
                Credit(id="c1", status="available", expires_at=2_000_000_000),
                Credit(id="c2", status="redeeming", expires_at=2_000_000_000),
                Credit(id="c3", status="expired", expires_at=1_600_000_000),
                count=1,
            ),
            "b@example.com": _usage("b@example.com"),
        },
        monkeypatch,
    )
    cli.cmd_credits(env)
    out = capsys.readouterr().out
    assert "counts 1 usable but sent detail for 3" in out, out
    assert "expired or already being redeemed" in out, out
    assert len([ln for ln in out.splitlines() if "05-18" in ln or "09-13" in ln]) >= 2, out


def test_nothing_is_said_when_the_count_matches_the_list(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """늘 떠 있는 안내는 곧 안 읽힌다. 어긋날 때만 말한다."""
    _answer(
        {
            "a@example.com": _usage(
                "a@example.com",
                Credit(id="c1", status="available", expires_at=2_000_000_000),
                Credit(id="c2", status="available", expires_at=2_100_000_000),
            ),
            "b@example.com": _usage("b@example.com"),
        },
        monkeypatch,
    )
    cli.cmd_credits(env)
    assert "usable of" not in capsys.readouterr().out


def test_the_other_direction_of_the_mismatch_is_not_described_backwards(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """개수가 상세보다 **많을** 때도 있다 — 서버가 일부만 보낸 경우다.

    한 방향 문구만 쓰면 "나머지는 만료됐다" 라고 **없는 사실**을 말한다. codex 검토가
    잡은 자리이고, 실제로 그 문구로 쓰여 있었다.
    """
    _answer(
        {
            "a@example.com": _usage(
                "a@example.com",
                Credit(id="c1", status="available", expires_at=2_000_000_000),
                count=2,
            ),
            "b@example.com": _usage("b@example.com"),
        },
        monkeypatch,
    )
    cli.cmd_credits(env)
    out = capsys.readouterr().out
    assert "counts 2 usable but sent detail for 1" in out, out
    assert "expired" not in out, out
    assert "did not arrive" in out, out


def test_a_failed_slot_is_not_sent_to_a_command_that_cannot_reach_it(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """`status --fresh` 는 **활성 계정만** 조회한다.

    실패한 것이 비활성 슬롯이면 그 안내를 따라가도 그 슬롯에 닿지 않는다. codex 검토가
    잡았다.
    """
    _answer({"a@example.com": _usage("a@example.com")}, monkeypatch)  # shared 실패
    cli.cmd_credits(env)
    out = capsys.readouterr().out
    assert "could not read: shared" in out, out
    assert "status --fresh" not in out, out


def test_asking_for_json_with_no_accounts_does_not_need_codex(
    _isolated_home: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """슬롯이 없으면 프로브할 것도 없다.

    사람용 판은 안내 한 줄을 내고 0 으로 끝나는데 기계용 판만 바이너리를 찾다 죽었다.
    """

    def missing() -> Path:
        raise RuntimeError("no codex on PATH")

    monkeypatch.setattr(cli.discovery, "resolve_codex_bin", missing)
    assert cli.cmd_credits_json(config.load()) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["accounts"] == []


# ── 시각 계산 (codex 검토에서 나온 것) ───────────────────────────────────────


def test_a_daylight_saving_boundary_does_not_swallow_an_hour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """로컬 시각으로 바꾼 뒤 빼면 시계를 되돌리는 한 시간이 뺄셈에서 사라진다.

    `America/New_York` 에서 20 분 남은 쿠폰이 `(expired)` 로 나왔다.
    """
    import datetime
    import time as _time

    monkeypatch.setenv("TZ", "America/New_York")
    _time.tzset()
    try:
        now = datetime.datetime(2026, 11, 1, 5, 50, tzinfo=datetime.UTC).timestamp()
        soon = datetime.datetime(2026, 11, 1, 6, 10, tzinfo=datetime.UTC).timestamp()
        assert "in 20m" in cli._expiry_text(soon, now=now)
        assert "expired" not in cli._expiry_text(soon, now=now)
    finally:
        monkeypatch.undo()
        _time.tzset()


def test_a_credit_that_just_expired_does_not_read_as_almost_due() -> None:
    """`int(-0.5)` 는 0 이라 만료 직후 1 초 동안 `in 0m` 이었다."""
    assert "expired" in cli._expiry_text(1_700_000_000, now=1_700_000_000.5)


@pytest.mark.parametrize("value", [1_700_000_000_000, -1_700_000_000_000, 10**18])
def test_an_epoch_outside_the_calendar_does_not_take_the_screen_down(value: int) -> None:
    """밀리초 epoch 하나가 섞이면 `fromtimestamp` 가 던진다.

    행을 다 모은 뒤 찍는 구조라, 그 예외 하나에 **멀쩡한 슬롯의 결과까지** 화면에 못
    나온다. 모르는 값 하나에 명령 전체를 걸 이유가 없다.
    """
    assert cli._expiry_text(value) == "-"
    assert cli._reset_text(value) == "-"


# ── codex 검토가 지목한 공백 (뮤테이션으로 전부 생존 확인) ──────────────────


def test_one_slot_blowing_up_does_not_take_the_others_with_it(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """`ProbeResult.unknown()` 은 정상 반환이다. **예외**는 그것과 다른 경로다.

    슬롯 하나가 던지는데 안 막으면 명령 전체가 죽어, 멀쩡한 계정의 쿠폰도 못 본다.
    """

    def explode(_bin: str, home: str) -> ProbeResult:
        if identity.email_of(Path(home) / "auth.json") == "b@example.com":
            raise RuntimeError("app-server went away")
        return ProbeResult.of(_usage("a@example.com", Credit(id="c1", status="available")))

    monkeypatch.setattr(probe, "probe", explode)
    assert cli.cmd_credits(env) == 0
    out = capsys.readouterr().out
    assert "could not read: shared" in out, out
    assert "master" in out, out


def test_the_active_account_is_read_from_the_live_home_not_the_slot_copy(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """슬롯 사본은 토큰이 갱신되며 뒤처지고 **기본 홈이 언제나 사실**이다.

    `refresh_all` 이 같은 이유로 같은 선택을 한다. 이메일로는 못 가른다 — 둘 다 같은
    계정이라서다. 그래서 **경로**로 가른다.
    """

    def by_path(_bin: str, home: str) -> ProbeResult:
        if Path(home) == env.default_home:
            return ProbeResult.of(_usage("a@example.com", Credit(id="live", status="available")))
        return ProbeResult.unknown()

    monkeypatch.setattr(probe, "probe", by_path)
    cli.cmd_credits(env)
    out = capsys.readouterr().out
    assert "could not read: shared" in out, out
    assert "could not read: master" not in out, "활성을 슬롯 사본으로 읽었다\n" + out


def test_a_credit_that_is_not_available_says_so(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """`redeeming` 인 쿠폰이 `available` 과 똑같이 보이면 쓸 수 있는 줄 안다."""
    _answer(
        {
            "a@example.com": _usage(
                "a@example.com",
                Credit(id="c1", status="redeeming", title="Full reset"),
                count=0,
            ),
            "b@example.com": _usage("b@example.com"),
        },
        monkeypatch,
    )
    cli.cmd_credits(env)
    assert "(redeeming)" in capsys.readouterr().out


def test_each_credit_shows_its_own_expiry(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """만료가 다른데 같은 날짜가 두 줄이면, 하나가 곧 사라지는 것을 못 본다."""
    _answer(
        {
            "a@example.com": _usage(
                "a@example.com",
                Credit(id="c1", status="available", expires_at=2_000_000_000),
                Credit(id="c2", status="available", expires_at=2_100_000_000),
            ),
            "b@example.com": _usage("b@example.com"),
        },
        monkeypatch,
    )
    cli.cmd_credits(env)
    out = capsys.readouterr().out
    dates = set(re.findall(r"\b\d{2}-\d{2} \d{2}:\d{2}\b", out))
    assert len(dates) == 2, f"두 쿠폰이 같은 만료일로 나왔다: {dates}\n{out}"


def test_the_json_carries_more_than_the_id(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """`id` 만 단언하면 나머지 필드가 통째로 사라져도 통과한다."""
    _answer(
        {
            "a@example.com": _usage(
                "a@example.com",
                Credit(
                    id="c1",
                    status="available",
                    title="Full reset",
                    granted_at=1_600_000_000,
                    expires_at=2_000_000_000,
                ),
            ),
            "b@example.com": _usage("b@example.com"),
        },
        monkeypatch,
    )
    cli.cmd_credits_json(env)
    doc = json.loads(capsys.readouterr().out)
    credit = next(a for a in doc["accounts"] if a["label"] == "master")["credits"][0]
    assert credit == {
        "id": "c1",
        "status": "available",
        "title": "Full reset",
        "grantedAt": 1_600_000_000,
        "expiresAt": 2_000_000_000,
    }


def test_the_json_keeps_the_count_and_the_email_for_readable_accounts(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """못 읽은 계정의 `null` 만 재면, 읽은 계정까지 `null` 이 돼도 통과한다."""
    _answer(
        {
            "a@example.com": _usage("a@example.com", count=3),
            "b@example.com": _usage("b@example.com", count=0),
        },
        monkeypatch,
    )
    cli.cmd_credits_json(env)
    doc = json.loads(capsys.readouterr().out)
    by_label = {a["label"]: a for a in doc["accounts"]}
    assert by_label["master"]["availableCount"] == 3
    assert by_label["shared"]["availableCount"] == 0
    assert by_label["master"]["email"] == "a@example.com"
    assert by_label["shared"]["email"] == "b@example.com"


def test_the_json_marks_only_the_active_account_active(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """전부 활성으로 적으면 스크립트가 엉뚱한 계정에 대고 동작한다."""
    _answer(
        {"a@example.com": _usage("a@example.com"), "b@example.com": _usage("b@example.com")},
        monkeypatch,
    )
    cli.cmd_credits_json(env)
    doc = json.loads(capsys.readouterr().out)
    assert doc["active"] == "master"
    assert [a["label"] for a in doc["accounts"] if a["active"]] == ["master"]


def test_the_json_flag_actually_reaches_the_json_function(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """함수를 직접 부르는 테스트만 있으면 **배선이 끊겨도** 전부 통과한다.

    실제 CLI 에서 `--json` 이 표를 찍는 일이 그렇게 생긴다.
    """
    _answer(
        {"a@example.com": _usage("a@example.com"), "b@example.com": _usage("b@example.com")},
        monkeypatch,
    )
    assert cli.main(["credits", "--json"]) == 0
    json.loads(capsys.readouterr().out)  # 표였다면 여기서 터진다
