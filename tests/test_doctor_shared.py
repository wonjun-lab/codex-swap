"""`doctor` — 공식 앱과 refresh token 을 나눠 쥔 자리.

공식 앱과 한 기기에서 같이 살면서 실제로 본 것이 이 파일의 출발점이다.

- 같은 refresh token 을 두 곳이 쥐어, 앱의 codex 로그에 `401 Encountered invalidated oauth
  token` 이 수만 줄 쌓였다.

여기서 재는 것은 `doctor` 가 그 공유를 **짚어 내는가**, 그리고 진단하다가 그 토큰을 갱신해 앱을
로그아웃시키지 않는가다. 실제 codex 도, 실제 토큰도 쓰지 않는다. 판정에 넣는 입력은 **제품
상수를 되받아 쓰지 않고 글자 그대로 적는다** — 상수를 입력으로 쓰면 그 상수가 틀려도 테스트가
따라 틀려서 아무것도 못 잡는다.

임시 HOME `box` 는 `_coexist.py` 에 있다.
"""

from __future__ import annotations

import json

from _coexist import _auth
from codex_swap.core import config, doctor

# ── doctor: 앱과 토큰을 나눠 쥔 자리 ────────────────────────────────────────────


def _shared_setup(box) -> config.Settings:
    box.app.mkdir(parents=True)
    s = config.load()
    _auth(box.home / ".codex/auth.json", "shared@example.com", refresh="rt-LINEAGE-SECRET")
    _auth(s.accounts_dir / "shared/auth.json", "shared@example.com", refresh="rt-LINEAGE-SECRET")
    _auth(s.accounts_dir / "master/auth.json", "master@example.com", refresh="rt-own-login")
    return s


def test_doctor_finds_a_slot_sharing_a_refresh_token_with_the_app(box) -> None:
    s = _shared_setup(box)
    found = doctor.shared_with_app(s)
    assert [f.label for f in found] == ["shared"], found
    assert "add shared --force" in found[0].fix
    assert "SECRET" not in " ".join(f"{f.detail} {f.fix}" for f in found)


def test_doctor_never_probes_a_slot_that_shares_the_apps_token(box) -> None:
    """**프로브가 그 토큰을 갱신하면 앱이 로그아웃된다.** 진단하다 앱을 망가뜨리면 안 된다.

    게다가 프로브를 먼저 돌리면 그 갱신이 공유의 증거까지 지워, 경고할 슬롯이 "reachable" 로
    나왔다.
    """
    s = _shared_setup(box)
    probed: list[str] = []

    def fake_check(_settings, label, _active):
        probed.append(label)
        return doctor.Finding(label, doctor.OK, "reachable")

    box.mp.setattr(doctor, "check", fake_check)
    box.mp.setattr(doctor, "drifted", lambda _s: None)
    found = doctor.run(s)
    assert "shared" not in probed, probed
    assert "master" in probed, "제 로그인을 가진 슬롯까지 검사를 멈췄다"
    assert found[0].state == doctor.SHARED


def test_sharing_the_apps_home_keeps_the_probe_off_every_copy_of_its_login(box) -> None:
    """**같은 홈이라고 슬롯 비교를 건너뛰었다.**

    그러면 활성 라벨 하나만 프로브에서 빠지고, 앱의 로그인을 그대로 쥔 다른 슬롯은 검사로
    넘어간다 — 같은 계정을 두 이름으로 등록해 두면 그렇다(활성은 이메일로 하나만 고른다).
    그 프로브가 토큰을 갱신하면 앱이 로그아웃된다.
    """
    box.app.mkdir(parents=True)
    box.mp.setenv("CODEX_ACCOUNT_DEFAULT_HOME", str(box.home / ".codex"))
    s = config.load()
    _auth(box.home / ".codex/auth.json", "master@example.com", refresh="rt-APP-LOGIN")
    _auth(s.accounts_dir / "master/auth.json", "master@example.com", refresh="rt-APP-LOGIN")
    _auth(s.accounts_dir / "master-2/auth.json", "master@example.com", refresh="rt-APP-LOGIN")
    _auth(s.accounts_dir / "work/auth.json", "work@example.com", refresh="rt-own-login")
    probed: list[str] = []

    def fake_check(_settings, label, _active):
        probed.append(label)
        return doctor.Finding(label, doctor.OK, "reachable")

    box.mp.setattr(doctor, "check", fake_check)
    box.mp.setattr(doctor, "drifted", lambda _s: None)
    found = doctor.run(s)
    assert probed == ["work"], probed
    assert "both using" in found[0].detail, found
    # 프로브를 막는 것만으로는 모자라다 — 어느 슬롯이 앱과 로그인을 나눠 쥐었는지 **처음부터**
    # 짚어야 사용자가 그 슬롯을 다시 로그인시킨다. 활성이라 검사에서 빠진 슬롯도 마찬가지다.
    assert {"master", "master-2"} <= {f.label for f in found if f.state == doctor.SHARED}, found
    # 같은 홈이면 활성 파일이 곧 앱의 파일이다. "다른 슬롯으로 바꿔라" 를 덧붙이면 틀린 안내다 —
    # 홈을 나누기 전에는 어느 슬롯으로 바꿔도 앱과 같은 파일을 쓴다.
    assert [f.label for f in found].count("active") == 1, found


def test_doctor_looks_again_right_before_each_probe(box) -> None:
    """**공유 검사와 프로브 사이에 슬롯 내용이 바뀔 수 있다** — `add`·`adopt` 가 동시에 돌면.

    첫 검사 결과만 믿으면 그사이 앱의 로그인을 받은 슬롯이 프로브로 넘어가 앱을 로그아웃시킨다.
    """
    s = _shared_setup(box)
    box.mp.setattr(doctor, "shared_with_app", lambda _settings, _labels=None: [])
    probed: list[str] = []

    def fake_check(_settings, label, _active):
        probed.append(label)
        return doctor.Finding(label, doctor.OK, "reachable")

    box.mp.setattr(doctor, "check", fake_check)
    box.mp.setattr(doctor, "drifted", lambda _s: None)
    found = doctor.run(s)
    assert probed == ["master"], probed
    assert [f.label for f in found if f.state == doctor.SHARED] == ["shared"], found


def test_a_malformed_token_cannot_leak_through_an_exception(box, tmp_path) -> None:
    """짝 없는 surrogate 를 그대로 `encode()` 하면 예외 객체에 토큰 원문이 실린다."""
    p = tmp_path / "auth.json"
    p.write_text(json.dumps({"tokens": {"refresh_token": "rt-\ud800-SECRET"}}))
    assert isinstance(doctor._refresh_fingerprint(p), str)


def test_doctor_is_quiet_when_every_slot_has_its_own_login(box) -> None:
    box.app.mkdir(parents=True)
    s = config.load()
    _auth(box.home / ".codex/auth.json", "app@example.com", refresh="rt-app")
    _auth(s.accounts_dir / "work/auth.json", "work@example.com", refresh="rt-work")
    assert doctor.shared_with_app(s) == []


def test_doctor_calls_out_sharing_the_apps_home_outright(box) -> None:
    box.app.mkdir(parents=True)
    box.mp.setenv("CODEX_ACCOUNT_DEFAULT_HOME", str(box.home / ".codex"))
    found = doctor.shared_with_app(config.load())
    assert len(found) == 1 and "both using" in found[0].detail, found


def test_doctor_has_nothing_to_say_about_the_app_when_there_is_none(box) -> None:
    s = config.load()
    _auth(box.home / ".codex/auth.json", "me@example.com", refresh="rt-same")
    _auth(s.accounts_dir / "me/auth.json", "me@example.com", refresh="rt-same")
    assert doctor.shared_with_app(s) == []
