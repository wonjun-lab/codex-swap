"""`doctor` — 계정이 **실제로 쓸 수 있는지**.

이 파일이 지키는 것은 "파일이 있나" 가 아니라 "써지나" 다. 그 둘이 갈리는 상태가 실제로
있었다 — SSH 로 접속한 원격에서 로그인하면 브라우저가 없어 OAuth 콜백이 로컬로 가 버리고,
`auth.json` 은 생겼는데 그 안의 토큰은 무효다. 이메일은 그 파일을 네트워크 없이 풀어
읽으므로 멀쩡히 보이고, 사용량만 `?` 가 된다.

그래서 여기서 가장 중요한 테스트는 **파일이 있는데 서버가 거절하는 경우**다.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from codex_swap import cli
from codex_swap.core import config, doctor
from codex_swap.core.types import ProbeResult, Usage


def _auth(path: Path, email: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    claims = base64.urlsafe_b64encode(json.dumps({"email": email}).encode())
    path.write_text(json.dumps({"tokens": {"id_token": f"h.{claims.decode().rstrip('=')}.s"}}))


@pytest.fixture
def env(_isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> config.Settings:
    s = config.load()
    _auth(s.accounts_dir / "master/auth.json", "a@example.com")
    _auth(s.default_home / "auth.json", "a@example.com")
    monkeypatch.setattr(cli.discovery, "resolve_codex_bin", lambda: Path("/bin/true"))
    monkeypatch.setattr(doctor.discovery, "resolve_codex_bin", lambda: Path("/bin/true"))
    return s


def _probe(monkeypatch: pytest.MonkeyPatch, result: ProbeResult) -> None:
    monkeypatch.setattr(doctor.probe, "probe", lambda *_, **__: result)


def test_credentials_that_the_server_rejects_are_named_as_such(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**이 도구가 만들어진 이유다.**

    파일은 멀쩡한데 토큰이 죽었다. "auth.json 이 있다" 만 보는 진단은 이것을 통과시키고,
    사용자는 화면의 `?` 만 보며 도구가 깨진 줄 안다.
    """
    _probe(monkeypatch, ProbeResult.auth_failed())
    found = doctor.check(env, "master", "master")
    assert found.state == doctor.AUTH
    assert not found.ok
    assert "rejected" in found.detail, found.detail
    assert found.fix, "무엇을 하면 되는지가 없다"


def test_the_fix_warns_about_the_ssh_trap(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "다시 로그인하세요" 만으로는 SSH 에서 **또 같은 결과**가 나온다.

    브라우저는 노트북에 있고 대기 중인 서버는 원격에 있어, 로그인이 반쪽만 끝난 채 아무
    말도 하지 않는다. 사용자가 실제로 이 함정에 빠졌다.
    """
    _probe(monkeypatch, ProbeResult.auth_failed())
    fix = doctor.check(env, "master", "master").fix
    assert "SSH" in fix, fix
    assert "port" in fix or "sitting" in fix, fix


def test_a_missing_auth_file_is_not_confused_with_a_dead_token(env: config.Settings) -> None:
    """둘은 조치가 다르다 — 하나는 `add`, 하나는 재로그인이다."""
    found = doctor.check(env, "nobody", None)
    assert found.state == doctor.NO_CREDENTIALS
    assert "add nobody" in found.fix or "adopt nobody" in found.fix, found.fix


def test_a_network_failure_is_not_called_an_auth_problem(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """섞으면 사용자가 멀쩡한 로그인을 지우고 다시 한다."""
    _probe(monkeypatch, ProbeResult.unknown())
    found = doctor.check(env, "master", "master")
    assert found.state == doctor.UNREACHABLE
    assert "auth" not in found.detail.lower(), found.detail


def test_a_slot_pointing_at_a_different_account_is_caught(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """슬롯이 A 라고 적어 뒀는데 답하는 것은 B 다. 그대로 두면 남의 사용량을 본다."""
    _probe(monkeypatch, ProbeResult.of(Usage(used_percent=10, email="someone@else.com")))
    found = doctor.check(env, "master", "master")
    assert found.state == doctor.MISMATCH
    assert "adopt master" in found.fix, found.fix


def test_a_healthy_account_says_so(env: config.Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    _probe(monkeypatch, ProbeResult.of(Usage(used_percent=42, email="a@example.com")))
    found = doctor.check(env, "master", "master")
    assert found.ok
    assert "42%" in found.detail


def test_the_active_account_is_read_from_the_default_home(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """슬롯 사본은 전환 시점의 스냅숏이라 토큰이 뒤처진다 — `status` 가 그래서 틀렸었다."""
    seen: list[str] = []

    def record(_bin: object, home: str) -> ProbeResult:
        seen.append(home)
        return ProbeResult.of(Usage(used_percent=1, email="a@example.com"))

    monkeypatch.setattr(doctor.probe, "probe", record)
    doctor.check(env, "master", "master")
    assert seen == [str(env.default_home)], seen


def test_everything_healthy_still_says_something(env: config.Settings) -> None:
    """조용히 끝내면 사용자는 점검이 안 돈 줄 안다."""
    assert "reachable" in doctor.summary([doctor.Finding("a", doctor.OK, "1% used")])


def test_no_accounts_points_at_the_first_step(env: config.Settings) -> None:
    assert "adopt" in doctor.summary([])


def test_the_command_exits_nonzero_when_something_is_broken(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """스크립트가 이 명령으로 상태를 볼 수 있어야 한다."""
    _probe(monkeypatch, ProbeResult.auth_failed())
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out, out
    assert "→" in out, "조치가 결과 바로 밑에 없다"


def test_the_command_exits_zero_when_all_is_well(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _probe(monkeypatch, ProbeResult.of(Usage(used_percent=42, email="a@example.com")))
    assert cli.main(["doctor"]) == 0
    assert "ok" in capsys.readouterr().out
