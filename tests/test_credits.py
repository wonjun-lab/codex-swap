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
