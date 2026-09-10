"""CLI 와 TUI 가 같은 것을 지키는가.

이 프로젝트에서 반복해서 나온 결함이 하나 있다 — **한쪽 표면에만 가드가 있는 것.** 그때마다
약한 쪽이 이 도구의 실제 안전 수준이 됐고, 하필 기본 표면(인자 없이 `codex-swap`)이 TUI 라
그쪽이 약하면 대부분의 사용자가 약한 쪽을 쓴다.

지금까지 그렇게 갈렸던 것들: adopt 의 덮어쓰기 보호 · `use` 의 경고 · `CRED` 열 · 그리고
여기서 잡은 **전환 가드**.

여기서 재는 것은 "두 표면의 문구가 같은가" 가 아니다. 문구는 달라도 된다 — 화면과 파이프는
다른 매체다. 재는 것은 **같은 위험 앞에서 둘 다 멈추는가** 다.

의도적으로 한쪽에만 있는 것은 아래 `KNOWN_ASYMMETRY` 에 이유와 함께 적는다. 적히지 않은
비대칭이 생기면 그것은 결함이다.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from codex_swap import cli, tui
from codex_swap.core import config, store
from codex_swap.core import credits as credits_core

KNOWN_ASYMMETRY = {
    "rotate": "훅이 부르는 명령이다. 사람이 화면에서 누를 일이 없다",
    "clean": "유지보수용. 잘못 눌러서 좋을 것이 없는 정리 작업이다",
    "--json": "기계용 출력. 화면에는 대응물이 없다",
    "add": "브라우저 로그인을 띄운다. curses 를 벗어났다 돌아오는 경로가 따로 필요하다",
    "rename": "아직 없다. TUI 에 넣을 값어치는 있지만 이번 범위가 아니다",
    "remove": "아직 없다. 위와 같다",
}
"""**적혀 있다고 괜찮다는 뜻은 아니다.** 아는 채로 두었다는 뜻이다."""


def _auth(path: Path, email: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    claims = base64.urlsafe_b64encode(json.dumps({"email": email}).encode())
    path.write_text(json.dumps({"tokens": {"id_token": f"h.{claims.decode().rstrip('=')}.s"}}))


@pytest.fixture
def orphan(_isolated_home: Path) -> config.Settings:
    """슬롯은 둘인데 **지금 로그인된 계정은 어느 슬롯도 아니다.**

    전환하면 그 자격증명이 사라진다 — 슬롯 사본이 없으므로 되돌릴 방법도 없다.
    """
    s = config.load()
    _auth(s.accounts_dir / "master/auth.json", "a@example.com")
    _auth(s.accounts_dir / "shared/auth.json", "b@example.com")
    _auth(s.default_home / "auth.json", "nobody@example.com")
    assert store.active_label(s) is None, "전제가 깨졌다 — 활성이 슬롯과 맞아 버렸다"
    return s


# ── 전환이 로그인된 계정을 버리려 할 때 ─────────────────────────────────────


def test_the_cli_refuses_and_names_the_way_out(orphan: config.Settings) -> None:
    with pytest.raises(cli.CliError) as exc:
        cli.cmd_use(orphan, "master")
    said = str(exc.value)
    assert "not in any slot" in said, said
    assert "adopt" in said, said
    assert "--force" in said, said


def test_the_tui_stops_too_instead_of_only_warning(orphan: config.Settings) -> None:
    """**여기가 갈려 있었다.** TUI 는 꼬리말에 경고 한 줄만 띄우고 그냥 전환했다.

    기본 표면이 자격증명을 더 쉽게 버리면, 경고가 있다는 사실은 위안이 되지 않는다.
    """
    view = tui.build_view(orphan)
    before = store.active_auth(orphan).read_text()

    after = tui.do_switch(view)
    assert after.switch_armed, "묻지도 않고 진행했다"
    assert "nobody@example.com" in after.message, after.message
    assert "adopt" in after.message, after.message
    assert store.active_auth(orphan).read_text() == before, "전환이 이미 일어났다"


def test_pressing_switch_again_goes_through(orphan: config.Settings) -> None:
    """완전히 막지는 않는다. 임시 로그인을 일부러 버리는 용법이 있다 — CLI 의 `--force`."""
    view = tui.do_switch(tui.build_view(orphan))
    after = tui.do_switch(view)
    assert not after.switch_armed
    assert "Switched to" in after.message, after.message


def test_moving_the_cursor_takes_the_arming_back(orphan: config.Settings) -> None:
    """물어본 것을 잊고 나중에 누른 `s` 가 곧바로 버리면 묻는 의미가 없다."""
    armed = tui.do_switch(tui.build_view(orphan))
    assert armed.switch_armed
    moved = tui.build_view(orphan, cursor=armed.cursor + 1)
    assert not moved.switch_armed


def test_a_registered_active_account_switches_without_a_second_press(
    _isolated_home: Path,
) -> None:
    """가드가 평상시를 방해하면 안 된다 — 매번 두 번 눌러야 하면 곧 반사적으로 넘긴다."""
    s = config.load()
    _auth(s.accounts_dir / "master/auth.json", "a@example.com")
    _auth(s.accounts_dir / "shared/auth.json", "b@example.com")
    _auth(s.default_home / "auth.json", "a@example.com")
    view = tui.build_view(s)
    at = next(i for i, row in enumerate(view.rows) if row.label == "shared")
    after = tui.do_switch(tui.replace(view, cursor=at))
    assert not after.switch_armed
    assert "Switched to shared" in after.message, after.message


def test_no_live_login_means_there_is_nothing_to_discard(_isolated_home: Path) -> None:
    """버릴 것이 없는데 묻는 것은 잡음이다."""
    s = config.load()
    _auth(s.accounts_dir / "master/auth.json", "a@example.com")
    _auth(s.accounts_dir / "shared/auth.json", "b@example.com")
    view = tui.build_view(s)
    assert view.active_email is None
    after = tui.do_switch(view)
    assert not after.switch_armed, after.message


# ── 비대칭 목록이 실제와 맞는가 ─────────────────────────────────────────────


def test_every_cli_command_is_either_on_screen_or_listed_as_known(
    _isolated_home: Path,
) -> None:
    """새 명령을 CLI 에만 붙이고 화면을 잊는 일이 반복됐다.

    여기서 막는 것은 "전부 화면에 넣어라" 가 아니다 — 넣지 않기로 했으면 **그 사실을 적어
    두라**는 것이다. 적히지 않은 비대칭은 결함이거나 잊은 것이다.
    """
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if getattr(a, "choices", None))

    # 별칭은 같은 파서를 가리킨다. 이름 단위로 보면 `rm` 이 `remove` 와 따로 걸려,
    # 목록에 둘을 다 적어야 하는 잡일이 생긴다.
    by_parser: dict[int, set[str]] = {}
    for name, sub_parser in sub.choices.items():
        by_parser.setdefault(id(sub_parser), set()).add(name)

    on_screen = {action for action, _ in tui.MENU} | {
        "use",
        "switch",
        "list",
        "ls",
        "status",
    }
    known = on_screen | set(KNOWN_ASYMMETRY)
    missing = {sorted(names)[0] for names in by_parser.values() if not (names & known)}
    assert not missing, (
        f"화면에 없고 목록에도 없는 명령: {sorted(missing)}\n"
        "TUI 에 넣거나, 안 넣는 이유를 KNOWN_ASYMMETRY 에 적어라"
    )


# ── 쿠폰 소비: 두 표면이 똑같이 막는가 ──────────────────────────────────────
#
# 여기가 비어 있었다. CLI 쪽은 촘촘히 쟀는데 화면 쪽은 한 줄도 없어서, 확인 입력을 아예
# 안 견주도록 바꿔도 아무 테스트가 물지 않았다 — **테스트 자체의 패리티 공백**이다.

SOON = tui.Credit(id="soon", status="available", expires_at=2_000_000_000, title="Full reset")
BUSY = tui.Credit(id="busy", status="redeeming", expires_at=2_000_000_000, title="Full reset")


@pytest.fixture
def screen(_isolated_home: Path, monkeypatch: pytest.MonkeyPatch):
    """쿠폰 화면 + 실제로 소비 요청이 나간 id 를 담는 목록."""
    from codex_swap.core import credits as credits_core

    s = config.load()
    _auth(s.accounts_dir / "master/auth.json", "a@example.com")
    _auth(s.default_home / "auth.json", "a@example.com")
    spent: list[str] = []

    def fake(_settings, _label, credit, _expect):
        spent.append(credit.id)
        return tui.probe.CreditOutcome.RESET

    monkeypatch.setattr(credits_core, "spend", fake)
    accounts = (credits_core.Account("master", "a@example.com", True, _usage_with(SOON)),)
    view = tui.replace(tui.build_view(s), mode="credits", credit_accounts=accounts, credit_cursor=0)
    return view, spent


def _usage_with(*credits: tui.Credit):
    from codex_swap.core.types import Usage

    return Usage(
        used_percent=98, email="a@example.com", reset_credits=len(credits), credits=credits
    )


@pytest.mark.parametrize("typed", [None, "", "n", "no", "master", "ye", "yolo"])
def test_the_screen_spends_nothing_without_a_yes(screen, typed: str | None) -> None:
    """무응답·거절·**옛 어휘**(라벨) 어느 것도 소비로 읽히면 안 된다."""
    view, spent = screen
    after = tui.apply_spend(view, typed)
    assert spent == [], typed
    assert "Left it alone" in after.message, after.message


@pytest.mark.parametrize("typed", ["y", "yes", "Y", "YES", " y "])
def test_a_yes_spends(screen, typed: str) -> None:
    view, spent = screen
    after = tui.apply_spend(view, typed)
    assert spent == ["soon"], typed
    assert "spent" in after.message, after.message


@pytest.mark.parametrize(
    ("answer", "ok"),
    [
        ("y", True),
        ("yes", True),
        ("Y", True),
        (" yes ", True),
        (None, False),  # 프롬프트가 끊겼다. 침묵은 승낙이 아니다
        ("", False),
        ("n", False),
        ("master", False),  # 옛 어휘
        ("ye", False),
    ],
)
def test_both_surfaces_take_the_same_word_for_yes(answer: str | None, ok: bool) -> None:
    """**같은 동작을 두 표면이 같은 어휘로 물어야 한다.**

    화면만 라벨을 그대로 치게 하던 때가 있었다(`Type shared to spend:`). 되돌릴 수 없으니 더
    세게 막자는 뜻이었는데, 무엇을 치라는 것인지부터 애매했고 — 계정 이름? `use`? 쿠폰
    이름? — 파이프에서는 `y` 면 되는 일이 화면에서만 달랐다.

    판정을 `core` 에 두면 갈릴 자리가 없어진다. 두 표면이 각자 적으면 한쪽만 `YES` 를
    받거나 한쪽만 `None` 을 승낙으로 읽는 식으로 어긋난다.
    """
    assert credits_core.said_yes(answer) is ok


def test_the_screen_will_not_offer_a_credit_that_is_not_available(_isolated_home: Path) -> None:
    """`redeeming` 은 이미 쓰이는 중이다. 물어보는 것 자체가 잘못이다."""
    from codex_swap.core import credits as credits_core

    s = config.load()
    _auth(s.accounts_dir / "master/auth.json", "a@example.com")
    _auth(s.default_home / "auth.json", "a@example.com")
    accounts = (credits_core.Account("master", "a@example.com", True, _usage_with(BUSY)),)
    view = tui.replace(tui.build_view(s), mode="credits", credit_accounts=accounts)
    assert tui.spend_prompt(view) is None
    assert "usable reset" in tui.apply_spend(view, "y").message


def test_the_prompt_names_the_account_and_asks_a_yes_or_no(screen) -> None:
    """**무엇을 쓰는지**와 **무엇을 치면 되는지**가 한 줄에 다 있어야 한다.

    `Type shared to spend:` 는 둘 다 흐렸다 — 치라는 것이 계정 이름인지 `use` 인지 알 수
    없었고, 그 이름이 쓰이는 대상인지 확인 문구인지도 겹쳐 읽혔다.
    """
    view, _ = screen
    asked = tui.spend_prompt(view)
    assert asked is not None
    assert "master" in asked  # 어느 계정의 리셋인지
    assert "[y/N]" in asked  # 무엇을 누르면 되는지
    assert "Type" not in asked


@pytest.mark.parametrize(
    ("outcome", "cleared"),
    [
        ("RESET", True),
        ("UNKNOWN", True),  # 썼는지 모르는 채로 낡은 숫자를 믿는 것이 더 나쁘다
        ("NOTHING_TO_RESET", False),  # 아무 일도 안 일어났다
        ("ALREADY_REDEEMED", True),  # 같은 시도가 이미 창을 되살렸다
        ("NO_CREDIT", False),  # 쓸 것이 없었다 — 사용량은 그대로다
    ],
)
def test_the_screen_clears_the_stale_usage_exactly_when_the_cli_does(
    _isolated_home: Path, monkeypatch: pytest.MonkeyPatch, outcome: str, cleared: bool
) -> None:
    """`rotate` 는 캐시를 정책 입력으로 읽는다. 안 지우면 방금 되살린 계정을 소진으로 본다."""
    from codex_swap.core import cache
    from codex_swap.core import credits as credits_core

    s = config.load()
    _auth(s.accounts_dir / "master/auth.json", "a@example.com")
    _auth(s.default_home / "auth.json", "a@example.com")
    cache.write(s, "master", {"usedPercent": 98}, now=1000)
    monkeypatch.setattr(
        credits_core,
        "spend",
        lambda *_, **__: getattr(tui.probe.CreditOutcome, outcome),
    )
    accounts = (credits_core.Account("master", "a@example.com", True, _usage_with(SOON)),)
    view = tui.replace(tui.build_view(s), mode="credits", credit_accounts=accounts)
    tui.apply_spend(view, "y")
    assert (cache.read_stale(s, "master") is None) is cleared
    # 위 표가 화면 쪽 구현과 **따로 놀지 않는지** 같이 잰다. 두 표면이 각자 목록을 적으면
    # 한쪽만 새 갈래를 빠뜨린다 — `NO_CREDIT` 이 실제로 그렇게 추가됐다.
    expected = getattr(tui.probe.CreditOutcome, outcome) not in credits_core.SPENT_NOTHING
    assert expected is cleared, f"{outcome}: SPENT_NOTHING 과 이 표가 어긋난다"


# ── 아직 쓸 때가 아니면 두 표면 다 막는가 ──────────────────────────────────


def _screen_at(settings, used: int):
    """사용량이 `used` 인 계정 하나짜리 쿠폰 화면."""
    from codex_swap.core import credits as credits_core
    from codex_swap.core.types import Usage

    usage = Usage(used_percent=used, email="a@example.com", reset_credits=1, credits=(SOON,))
    accounts = (credits_core.Account("master", "a@example.com", True, usage),)
    return tui.replace(tui.build_view(settings), mode="credits", credit_accounts=accounts)


def test_the_screen_stops_before_asking_when_usage_is_left(
    _isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**묻기 전에 세운다.** 물어본 뒤에 막으면 이미 `y` 하나 거리다.

    CLI 는 같은 자리에서 `--force` 를 요구한다. 화면에서는 한 번 더 누르는 것이 그 역할을
    한다 — `do_switch` 가 등록 안 된 계정을 버릴 때 쓰는 것과 같은 어휘다.
    """
    from codex_swap.core import credits as credits_core

    s = config.load()
    _auth(s.accounts_dir / "master/auth.json", "a@example.com")
    _auth(s.default_home / "auth.json", "a@example.com")
    spent: list[str] = []
    monkeypatch.setattr(
        credits_core, "spend", lambda *a, **k: spent.append("x") or tui.probe.CreditOutcome.RESET
    )

    view = _screen_at(s, used=40)
    after = tui.apply_spend(view, "y")

    assert spent == [], "경고 전에 이미 썼다"
    assert after.spend_armed, "한 번 더 누르면 되는 상태로 남아야 한다"
    assert "40%" in after.message, after.message


def test_pressing_enter_again_spends_anyway(
    _isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """완전히 막지는 않는다 — CLI 의 `--force` 와 같은 탈출구가 화면에도 있어야 한다."""
    from codex_swap.core import credits as credits_core

    s = config.load()
    _auth(s.accounts_dir / "master/auth.json", "a@example.com")
    _auth(s.default_home / "auth.json", "a@example.com")
    spent: list[str] = []
    monkeypatch.setattr(
        credits_core, "spend", lambda *a, **k: spent.append("x") or tui.probe.CreditOutcome.RESET
    )

    armed = tui.apply_spend(_screen_at(s, used=40), "y")
    after = tui.apply_spend(armed, "y")
    assert spent == ["x"]
    assert "spent" in after.message, after.message


def test_moving_the_cursor_takes_the_spend_warning_back(_isolated_home: Path) -> None:
    """경고를 잊고 나중에 누른 `enter` 가 곧바로 프롬프트를 띄우면 세운 의미가 없다."""
    s = config.load()
    _auth(s.accounts_dir / "master/auth.json", "a@example.com")
    _auth(s.default_home / "auth.json", "a@example.com")
    armed = tui.replace(_screen_at(s, used=40), spend_armed=True)
    assert not tui.move_credits(armed, +1).spend_armed


def test_a_nearly_spent_account_is_not_nagged(_isolated_home: Path) -> None:
    """가드가 평상시를 방해하면 안 된다 — 매번 두 번 눌러야 하면 곧 반사적으로 넘긴다."""
    s = config.load()
    _auth(s.accounts_dir / "master/auth.json", "a@example.com")
    _auth(s.default_home / "auth.json", "a@example.com")
    assert tui.spend_warning(_screen_at(s, used=98)) is None


@pytest.mark.parametrize("outcome", list(tui.probe.CreditOutcome), ids=lambda o: o.name)
def test_every_outcome_says_something_instead_of_crashing(
    _isolated_home: Path, monkeypatch: pytest.MonkeyPatch, outcome
) -> None:
    """**서버가 갈래를 하나 더 보내면 화면이 죽었다.**

    문구를 dict 로 두고 `[outcome]` 으로 꺼내는데, 열거형에 갈래를 추가하고 이 표에 넣는
    것을 잊으면 그 자리에서 `KeyError` 다 — 하필 되돌릴 수 없는 동작 직후, 사용자가 결과를
    가장 알고 싶은 순간에.

    실제로 `noCredit` 이 그랬다. 서버는 처음부터 보내고 있었고 우리만 몰랐다.
    """
    from codex_swap.core import credits as credits_core

    s = config.load()
    _auth(s.accounts_dir / "master/auth.json", "a@example.com")
    _auth(s.default_home / "auth.json", "a@example.com")
    monkeypatch.setattr(credits_core, "spend", lambda *_, **__: outcome)
    accounts = (credits_core.Account("master", "a@example.com", True, _usage_with(SOON)),)
    view = tui.replace(tui.build_view(s), mode="credits", credit_accounts=accounts)

    after = tui.apply_spend(view, "y")
    assert after.message.strip(), f"{outcome.name} 에서 아무 말이 없다"


def test_no_credit_is_not_reported_as_maybe_spent(
    _isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**확정된 사실을 불확실로 접으면 안 된다.**

    `UNKNOWN` 을 실패로 접으면 안 되는 것과 같은 무게의, 반대 방향 오분류다. 아무 일도
    없었는데 "썼는지 모른다" 고 하면 사용자는 잃지도 않은 쿠폰을 걱정한다.
    """
    from codex_swap.core import credits as credits_core

    s = config.load()
    _auth(s.accounts_dir / "master/auth.json", "a@example.com")
    _auth(s.default_home / "auth.json", "a@example.com")
    monkeypatch.setattr(credits_core, "spend", lambda *_, **__: tui.probe.CreditOutcome.NO_CREDIT)
    accounts = (credits_core.Account("master", "a@example.com", True, _usage_with(SOON)),)
    view = tui.replace(tui.build_view(s), mode="credits", credit_accounts=accounts)

    said = tui.apply_spend(view, "y").message
    assert "may or may not" not in said, said
    assert "Nothing was spent" in said, said


def test_the_screen_reports_a_refusal_from_core_instead_of_claiming_success(
    _isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codex_swap.core import credits as credits_core

    s = config.load()
    _auth(s.accounts_dir / "master/auth.json", "a@example.com")
    _auth(s.default_home / "auth.json", "a@example.com")

    def refuse(*_: object, **__: object):
        raise credits_core.CreditError("another switch is in progress. Nothing was spent")

    monkeypatch.setattr(credits_core, "spend", refuse)
    accounts = (credits_core.Account("master", "a@example.com", True, _usage_with(SOON)),)
    view = tui.replace(tui.build_view(s), mode="credits", credit_accounts=accounts)
    after = tui.apply_spend(view, "y")
    assert "Nothing was spent" in after.message, after.message


# ── core 가 소비 직전에 다시 보는가 ─────────────────────────────────────────


def test_core_refuses_when_the_account_changed_between_asking_and_spending(
    _isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """확인을 기다리는 동안 그 라벨이 **다른 계정을 담게** 될 수 있다.

    `adopt` 가 같은 이름 위에 지금 로그인된 계정을 덮어쓰는 것이 그런 경우다. 승인한 것은
    A 인데 요청은 B 의 자격증명으로 나간다. **두 표면 모두** 이 검사에 기댄다.

    활성 라벨이 안 맞는 쪽으로 어긋나는 경우는 이 검사가 필요 없다 — `_home_for` 가 슬롯
    사본으로 떨어지고, 그 사본은 여전히 그 계정의 것이다. 위험한 것은 **같은 이름이 다른
    계정을 가리키게 되는 것**이다.
    """
    from codex_swap.core import credits as credits_core

    s = config.load()
    _auth(s.accounts_dir / "master/auth.json", "a@example.com")
    _auth(s.default_home / "auth.json", "a@example.com")

    sent: list[str] = []

    def record(*_: object, **__: object):
        sent.append("went")
        return credits_core.probe.CreditOutcome.RESET

    monkeypatch.setattr(credits_core.probe, "consume_credit", record)
    monkeypatch.setattr(credits_core, "_codex_bin", lambda: "/bin/true")

    # 승인한 뒤, 소비 직전에 그 이름이 다른 계정을 담게 됐다.
    _auth(s.accounts_dir / "master/auth.json", "someone-else@example.com")
    _auth(s.default_home / "auth.json", "someone-else@example.com")

    with pytest.raises(credits_core.CreditError, match="Nothing was spent"):
        credits_core.spend(s, "master", SOON, "a@example.com")
    assert sent == [], "승인한 계정이 아닌데 요청이 나갔다"


def test_core_goes_through_when_the_account_is_still_the_one_approved(
    _isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """검사가 평상시를 막으면 기능이 없는 것과 같다."""
    from codex_swap.core import credits as credits_core

    s = config.load()
    _auth(s.accounts_dir / "master/auth.json", "a@example.com")
    _auth(s.default_home / "auth.json", "a@example.com")
    sent: list[str] = []
    monkeypatch.setattr(
        credits_core.probe,
        "consume_credit",
        lambda *_, **__: (sent.append("went"), credits_core.probe.CreditOutcome.RESET)[1],
    )
    monkeypatch.setattr(credits_core, "_codex_bin", lambda: "/bin/true")
    assert (
        credits_core.spend(s, "master", SOON, "a@example.com")
        is credits_core.probe.CreditOutcome.RESET
    )
    assert sent == ["went"]


def test_the_screen_has_nothing_the_command_line_cannot_reach(_isolated_home: Path) -> None:
    """**역방향도 본다.** 지금까지 TUI 에만 있던 것이 둘 있었다 — 정책 편집과 자동 전환.

    그 둘은 화면을 못 쓰는 환경(ssh 파이프·스크립트)에서 아예 손댈 수 없었고, README 는
    `touch ~/.claude/.codex-rotate-off` 라고 **내부 파일을 직접 만들라**고 안내했다.
    """
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if getattr(a, "choices", None))
    commands = set(sub.choices)
    for action, title in tui.MENU:
        if action == "quit":
            continue  # 화면을 닫는 것이지 기능이 아니다
        if action == "refresh":
            assert "list" in commands, "화면의 새로고침에 대응하는 명령이 없다"
            continue
        assert action in commands, f"화면에만 있는 기능: {title}"
