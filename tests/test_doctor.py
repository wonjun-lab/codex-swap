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


# ── 우리를 거치지 않고 바뀐 경우 ───────────────────────────────────────────
#
# 이것이 mbp-m5m 에서 실제로 일어난 일이다. ChatGPT 데스크톱 앱이 자기 codex 를
# `CODEX_HOME=~/.codex` 로 띄워 두고 같은 auth.json 을 쓰는데, 앱이 다른 계정으로 로그인돼
# 있으면 우리가 걸어 둔 것을 자기 세션으로 되돌려 놓는다. 사용자에게는 "로그인이 자꾸
# 풀린다" 로 보인다.


def test_an_outside_change_is_caught(env: config.Settings) -> None:
    """원장은 shared 로 갔다는데 살아 있는 것은 master 다."""
    from codex_swap.core import log

    _auth(env.accounts_dir / "shared/auth.json", "b@example.com")
    log.append(env, from_label="master", to_label="shared", reason="manual (tui)")
    # 앱이 되돌려 놓은 상태 — 활성 자리에 master 가 다시 들어와 있다.
    _auth(env.default_home / "auth.json", "a@example.com")

    found = doctor.drifted(env)
    assert found is not None
    assert found.state == doctor.DRIFT
    assert "shared" in found.detail and "master" in found.detail, found.detail


def test_the_fix_names_the_chatgpt_app_and_a_separate_home(env: config.Settings) -> None:
    """**원인을 모르면 계정을 다시 만드는 데 시간을 쓴다.** 실제로 그랬다."""
    from codex_swap.core import log

    _auth(env.accounts_dir / "shared/auth.json", "b@example.com")
    log.append(env, from_label="master", to_label="shared", reason="manual (tui)")
    _auth(env.default_home / "auth.json", "a@example.com")

    fix = doctor.drifted(env).fix
    assert "ChatGPT" in fix, fix
    # 예전에는 `export CODEX_HOME=… CODEX_ACCOUNT_DEFAULT_HOME=…` 를 손으로 치게 했다. 지금은
    # codex-swap 이 앱을 보고 스스로 비켜 서므로, 남은 일은 새 판을 받고 init 이 짚는 배선을
    # 고치는 것뿐이다. 수동 export 를 계속 권하면 dotfiles wrapper 와 우리 판단이 갈린다.
    assert "codex-swap init" in fix, fix
    assert "export CODEX_HOME" not in fix, fix


def test_a_matching_state_is_not_reported(env: config.Settings) -> None:
    """가드가 평상시를 방해하면 안 된다 — 정상인데 매번 경고하면 곧 안 읽힌다."""
    from codex_swap.core import log

    log.append(env, from_label="shared", to_label="master", reason="manual (tui)")
    assert doctor.drifted(env) is None


def test_no_ledger_yet_means_nothing_to_compare(env: config.Settings) -> None:
    """한 번도 안 바꿨으면 견줄 것이 없다. 첫 실행에 경고가 뜨면 안 된다."""
    assert doctor.drifted(env) is None


def test_the_environment_problem_is_listed_first(
    env: config.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """계정마다 실패가 줄줄이 뜨는데 까닭이 맨 아래 있으면, 그 전에 계정을 다시 만든다."""
    from codex_swap.core import log

    _auth(env.accounts_dir / "shared/auth.json", "b@example.com")
    log.append(env, from_label="master", to_label="shared", reason="manual (tui)")
    _auth(env.default_home / "auth.json", "a@example.com")
    _probe(monkeypatch, ProbeResult.auth_failed())

    found = doctor.run(env)
    assert found[0].state == doctor.DRIFT, [f.state for f in found]


def test_the_ledger_reader_takes_the_last_arrival(env: config.Settings) -> None:
    """마지막 줄만 본다. 원장은 계속 자라는 파일이라 통째로 읽으면 이 검사가 가장 비싸진다."""
    from codex_swap.core import log

    for target in ("master", "shared", "master"):
        log.append(env, from_label="x", to_label=target, reason="r")
    assert log.last_switch(env) == "master"
