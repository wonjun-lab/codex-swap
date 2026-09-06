"""프로브를 우회하지 않고 실측 응답을 파이프로 재생한다.

분류만 맞아도 요청 순서나 자식 회수가 틀리면 매 CLI 호출에 장애가 누적된다.
그래서 subprocess 는 실제로 띄우고, 계정 대신 고정된 서버 응답만 읽힌다.
"""

import json
import os
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from codex_swap.core import probe
from codex_swap.core.types import ProbeOutcome, ProbeResult, Usage

FIXTURES = Path(__file__).parent / "fixtures" / "probe"


def captured(name: str = "ok") -> list[str]:
    return json.loads((FIXTURES / f"{name}.json").read_text())["messages"]


def replace_response(messages: list[str], request_id: int, **fields: object) -> list[str]:
    result = []
    for line in messages:
        message = json.loads(line)
        if message.get("id") == request_id:
            message.update(fields)
            line = json.dumps(message)
        result.append(line)
    return result


def response(messages: list[str], request_id: int) -> dict:
    return next(msg for line in messages if (msg := json.loads(line)).get("id") == request_id)


class Server:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.home = root / "home"
        self.home.mkdir()
        self.events_path = root / "events.ndjson"
        self.scenario_path = root / "scenario.json"
        self.path = root / "fakecodex"
        self.path.write_text(
            f"#!/bin/sh\nexec {shlex.quote(sys.executable)} "
            f'{shlex.quote(str(FIXTURES / "fake_app_server.py"))} "$@"\n'
        )
        self.path.chmod(0o700)
        monkeypatch.setenv("HOME", str(self.home))
        monkeypatch.setenv("CODEX_HOME", str(self.home / ".codex"))
        monkeypatch.setenv("PROBE_SCENARIO", str(self.scenario_path))
        monkeypatch.setenv("PROBE_EVENTS", str(self.events_path))
        self.configure()

    def configure(self, messages: list[str] | None = None, **options: object) -> None:
        self.scenario_path.write_text(
            json.dumps({"messages": captured() if messages is None else messages, **options})
        )

    def events(self) -> list[dict]:
        if not self.events_path.exists():
            return []
        return [json.loads(line) for line in self.events_path.read_text().splitlines()]

    def run(self, timeout_ms: int = 2000) -> ProbeResult:
        return probe.probe(self.path, self.home, timeout_ms=timeout_ms)

    def assert_reaped(self) -> None:
        starts = [event for event in self.events() if event["kind"] == "start"]
        assert starts, "자식이 뜨지 않았다면 회수 검증도 성립하지 않는다"
        for event in starts:
            pid = event["pid"]
            try:
                waited, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                continue
            # 회귀가 나도 실행 중인 자식을 테스트 밖에 남기지 않는다.
            if waited == 0:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            pytest.fail(f"probe 가 자식 {pid} 를 회수하지 않았다")


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Server]:
    fake = Server(tmp_path, monkeypatch)
    yield fake
    if fake.events():
        fake.assert_reaped()


@pytest.mark.parametrize(
    "name",
    [
        "ok",
        "token_revoked",
        "empty_access_token",
        "expired_id_token",
        "unauthenticated",
        "apikey_only",
    ],
)
def test_captured_classification(server: Server, name: str) -> None:
    messages = captured(name)
    server.configure(messages)
    result = server.run()
    if name != "ok":
        assert result == ProbeResult.auth_failed()
        return
    assert response(messages, 2)["result"]["requiresOpenaiAuth"] is True
    assert result == ProbeResult.of(
        Usage(36, "fixture@example.com", "pro", 36, None, 1789232459, False)
    )
    assert type(result.usage.resets_at) is int


@pytest.mark.parametrize(
    ("name", "expected"),
    [("unauthenticated", ProbeOutcome.AUTH_FAILED), ("apikey_only", ProbeOutcome.UNKNOWN)],
)
def test_structure_and_wording_are_independent(
    server: Server, name: str, expected: ProbeOutcome
) -> None:
    messages = captured(name)
    account = response(messages, 2)["result"]["account"]
    assert (account is None) is (name == "unauthenticated")
    server.configure(
        replace_response(messages, 3, error={"code": -32600, "message": "rate limits unavailable"})
    )
    assert server.run().outcome is expected


def test_network_failure_stays_unknown(server: Server) -> None:
    """음성 대조군 — 인증 실패 승격이 네트워크 장애를 삼키면 안 된다.

    자격증명이 **유효한데** 네트워크만 죽은 실측 응답이다. `account/read` 는 로컬
    `auth.json` 파싱이라 계정이 그대로 채워지고, 한도 읽기만 -32603 으로 접힌다.
    구조 판정도 문구 판정도 걸리지 않아야 `Unknown` 이고, 그래야 `policy.decide` 가
    `Indeterminate` 로 끝내 **움직이지 않는다.** 여기까지 AuthFailed 로 삼키면
    인터넷이 끊길 때마다 멀쩡한 계정을 차례로 떠난다.
    """
    messages = captured("network_failure")
    assert response(messages, 2)["result"]["account"] is not None
    server.configure(messages)
    assert server.run() == ProbeResult.unknown()


@pytest.mark.parametrize("message", ["Please try signing in again.", "Please sign in again."])
def test_sign_in_wording_does_not_need_401(message: str) -> None:
    assert probe.AUTH_FAILURE_RE.search(message)


def test_handshake_waits_for_initialize_and_preserves_order(server: Server) -> None:
    assert server.run().ok
    events = server.events()
    received = [event["message"] for event in events if event["kind"] == "received"]
    assert [(msg.get("id"), msg["method"]) for msg in received] == [
        (1, "initialize"),
        (None, "initialized"),
        (2, "account/read"),
        (3, "account/rateLimits/read"),
    ]
    assert "id" not in received[1]
    assert received[0]["params"]["clientInfo"] == {
        "name": "codex-account-rotate",
        "title": "codex-account-rotate",
        "version": "1.0.0",
    }
    assert (
        next(event for event in events if event["kind"] == "before_initialize_response")[
            "premature"
        ]
        is False
    )
    sent = next(i for i, event in enumerate(events) if event["kind"] == "sent")
    initialized = next(
        i
        for i, event in enumerate(events)
        if event.get("message", {}).get("method") == "initialized"
    )
    assert sent < initialized
    assert any("remoteControl/status/changed" in event.get("line", "") for event in events)


def test_unrelated_ids_noise_and_partial_reads(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    poison = [
        json.dumps({"id": value, "error": {"message": "401"}})
        for value in (None, True, False, 999, "1")
    ]
    messages = captured()
    # 두 번째 응답 앞에도 잡음을 둬야 initialize 의 오류 무시가 거짓 양성을 숨기지 않는다.
    for index in (2, 0):
        messages[index:index] = ["diagnostic 401", "", "[1,2]", "{broken", *poison]
    server.configure(messages, fragment=True)
    monkeypatch.setattr(probe, "_READ_CHUNK", 7)
    assert server.run().usage == Usage(
        36, "fixture@example.com", "pro", 36, None, 1789232459, False
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, False),
        (False, False),
        (True, True),
        (0, False),
        (1, True),
        (0.0, False),
        (1.5, True),
        (float("nan"), False),
        ("", False),
        ("0", True),
        ({}, True),
        ([], True),
    ],
)
def test_js_truthy(value: object, expected: bool) -> None:
    assert probe.js_truthy(value) is expected


@pytest.mark.parametrize("request_id", [2, 3])
@pytest.mark.parametrize(
    ("error", "ok"), [(None, True), (False, True), (0, True), ("", True), ({}, False), ([], False)]
)
def test_error_gate_uses_js_truthiness(
    server: Server, request_id: int, error: object, ok: bool
) -> None:
    server.configure(replace_response(captured(), request_id, error=error))
    assert server.run().outcome is (ProbeOutcome.OK if ok else ProbeOutcome.UNKNOWN)
    assert [
        event["message"].get("id") for event in server.events() if event["kind"] == "received"
    ] == [1, None, 2, 3]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, None),
        (False, None),
        (0, 0),
        (4, 4),
        (4.0, 4),
        (37.5, None),
        ("95", 95),
        ("4.0", None),
        ("", None),
        # bash 의 관문은 `^[0-9]+$` 라 ASCII 전용이다. `str.isdigit()` 만 믿으면
        # FULLWIDTH·ARABIC-INDIC 숫자가 통과하고, SUPERSCRIPT TWO 는 참인데
        # `int()` 에서 ValueError 로 터진다 — 그 예외는 `probe()` 의 어느 except 에도
        # 안 걸려 호출자 터미널까지 올라간다. 리터럴로 적으면 ruff RUF001 이 막으므로
        # 이스케이프로 쓴다.
        ("\uff19\uff15", None),  # FULLWIDTH "95"
        ("\u00b2", None),  # SUPERSCRIPT TWO
        ("\u0669\u0665", None),  # ARABIC-INDIC "95"
        (-1, None),
        (-1.0, None),
        (None, None),
        ({}, None),
        (float("inf"), None),
        (float("nan"), None),
    ],
)
def test_accepts_pct(value: object, expected: int | None) -> None:
    assert probe.accepts_pct(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    # jq 의 `// empty` 가 접는 셋 — missing·null·false — 에 빈 문자열이 더해진다.
    # 렌더링이 비어서 bash 도 접는 자리다. `0` 은 `"0"` 으로 남으므로 접히지 않는다.
    [(None, False), (False, False), ("", False), (0, True), (True, True), ("primary", True)],
)
def test_reached(value: object, expected: bool) -> None:
    assert probe._reached(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, None),
        (False, None),
        (123, 123),
        (123.0, 123),
        (123.5, None),
        (-1, -1),
        ("123", None),
        (None, None),
    ],
)
def test_as_epoch(value: object, expected: int | None) -> None:
    assert probe._as_epoch(value) == expected


def limits_messages(primary: object, secondary: object, reached: object = None) -> list[str]:
    messages = captured()
    result = response(messages, 3)["result"]
    result["rateLimits"]["primary"]["usedPercent"] = primary
    result["rateLimits"]["secondary"] = {"usedPercent": secondary}
    result["rateLimits"]["rateLimitReachedType"] = reached
    return replace_response(messages, 3, result=result)


@pytest.mark.parametrize(
    ("primary", "secondary", "expected"),
    [(4.0, None, 4), (50, 37.5, 50), (4, 95, 95), (True, 20, 20)],
)
def test_max_precedes_integer_gate(
    server: Server, primary: object, secondary: object, expected: int
) -> None:
    server.configure(limits_messages(primary, secondary))
    result = server.run()
    assert result.ok and result.usage.used_percent == expected
    assert result.usage.secondary_percent == secondary


def test_fractional_max_is_unknown_with_exact_reason(server: Server) -> None:
    server.configure(limits_messages(4, 37.5))
    assert server.run() == ProbeResult.unknown()
    # 공개 결과는 이유를 지우므로 같은 실제 프로세스 경로에서 예외 문구도 확인한다.
    with pytest.raises(probe.ProbeError, match=r"^usedPercent 가 정수가 아니다$"):
        probe._run(server.path, server.home, 2000)


@pytest.mark.parametrize("value", [None, False, "95", {}])
def test_non_numeric_windows_are_unknown(server: Server, value: object) -> None:
    server.configure(limits_messages(value, None))
    assert server.run() == ProbeResult.unknown()


@pytest.mark.parametrize(("value", "expected"), [(0, True), (False, False)])
def test_reached_survives_the_protocol(server: Server, value: object, expected: bool) -> None:
    server.configure(limits_messages(36, None, value))
    result = server.run()
    assert result.ok and result.usage.reached is expected


def test_timeout_is_per_request(server: Server) -> None:
    # 두 지연의 합은 제한을 넘기되, 각 응답에는 CI 스케줄링 여유를 둔다.
    server.configure(delays={"2": 0.5, "3": 0.5})
    started = time.monotonic()
    assert server.run(timeout_ms=900).ok, server.events()
    assert time.monotonic() - started > 0.9


def test_silent_server_times_out_and_is_reaped(server: Server) -> None:
    server.configure(stop_at=2, mode="silence")
    started = time.monotonic()
    assert server.run(timeout_ms=250) == ProbeResult.unknown()
    elapsed = time.monotonic() - started
    # spawn·회수 시간까지 250ms 로 묶으면 CI 부하를 프로토콜 결함으로 오인한다.
    assert 0.25 <= elapsed < 1.0
    server.assert_reaped()


@pytest.mark.parametrize(
    ("mode", "stop_at"), [("exit", 1), ("exit", 2), ("close", 3), ("exit127", 1)]
)
def test_early_exit_and_stdout_close_are_unknown_and_reaped(
    server: Server, mode: str, stop_at: int
) -> None:
    server.configure(stop_at=stop_at, mode=mode)
    assert server.run(timeout_ms=400) == ProbeResult.unknown()
    server.assert_reaped()


def test_stubborn_child_is_killed_and_reaped(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    server.configure(ignore_term=True)
    monkeypatch.setattr(probe, "_TEARDOWN_WAIT_S", 0.05)
    assert server.run().ok
    server.assert_reaped()


@pytest.mark.parametrize("exists", [False, True])
def test_spawn_oserror_is_unknown(tmp_path: Path, exists: bool) -> None:
    executable = tmp_path / "missing-or-not-executable"
    if exists:
        executable.write_text("#!/bin/sh\nexit 127\n")
        executable.chmod(0o600)
    assert probe.probe(executable, tmp_path) == ProbeResult.unknown()


def test_lexical_parent_repairs_env_shebang(
    server: Server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    runtime = binary_dir / "fakenode"
    runtime.write_text(server.path.read_text().replace('"$@"', '"${2}"'))
    runtime.chmod(0o700)
    target = tmp_path / "package" / "fakecodex"
    target.parent.mkdir()
    target.write_text("#!/usr/bin/env fakenode\n")
    target.chmod(0o700)
    executable = binary_dir / "fakecodex"
    executable.symlink_to(target)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    direct = subprocess.run([executable, "app-server"], capture_output=True, timeout=2)
    assert direct.returncode == 127
    assert probe._probe_env(executable, server.home)["PATH"] == f"{binary_dir}:/usr/bin:/bin"
    assert probe.probe(executable, server.home).ok


@pytest.mark.parametrize("inherited", [False, True])
def test_probe_env_recursion_and_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inherited: bool
) -> None:
    monkeypatch.setenv("CODEX_ROTATE_FORCE", "1")
    monkeypatch.setenv("CODEX_ROTATE_SKIP", "0")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    if inherited:
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "inherited"))
    env = probe._probe_env("codex", None)
    assert env["CODEX_ROTATE_SKIP"] == "1"
    assert "CODEX_ROTATE_FORCE" not in env
    assert env.get("CODEX_HOME") == (str(tmp_path / "inherited") if inherited else None)
    assert probe._probe_env("codex", tmp_path)["CODEX_HOME"] == str(tmp_path)
    assert os.environ["CODEX_ROTATE_FORCE"] == "1"


def test_probe_env_without_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PATH", raising=False)
    assert probe._probe_env(tmp_path / "codex", None)["PATH"] == str(tmp_path)


def test_boolean_id_cannot_complete_initialize(server: Server) -> None:
    # initialize 의 error 는 원래 무시한다. 최종 OK 만 보면 True == 1 회귀가 숨는다.
    messages = captured()
    server.configure([json.dumps({"id": True, "result": "wrong response"}), *messages])
    proc = subprocess.Popen(
        [server.path, "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=probe._probe_env(server.path, server.home),
    )
    try:
        actual = probe._Conn(proc, 2).request(1, "initialize")
        assert actual == response(messages, 1)
        assert type(actual["id"]) is int
    finally:
        probe._shutdown(proc)


@pytest.mark.parametrize("value", [None, False, 0, "", {}, 42])
def test_missing_or_malformed_limits_are_unknown(server: Server, value: object) -> None:
    server.configure(replace_response(captured(), 3, result={"rateLimits": value}))
    assert server.run() == ProbeResult.unknown()


@pytest.mark.parametrize(("plan", "expected"), [(None, "pro"), ("", ""), (123, None)])
def test_plan_fallback_is_null_only(server: Server, plan: object, expected: str | None) -> None:
    messages = captured()
    account = response(messages, 2)["result"]
    account["account"]["planType"] = plan
    account["account"]["email"] = 123
    server.configure(replace_response(messages, 2, result=account))
    result = server.run()
    assert result.ok
    assert result.usage.plan_type == expected
    assert result.usage.email is None


def test_missing_account_does_not_override_successful_limits(server: Server) -> None:
    server.configure(replace_response(captured(), 2, result={"account": None}))
    result = server.run()
    assert result.ok and result.usage.used_percent == 36
    assert result.usage.plan_type == "pro"
    assert result.usage.email is None


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ({}, "undefined"),
        ([], "undefined"),
        ({"message": "401"}, "401"),
        ({"message": None}, "null"),
        ({"message": True}, "true"),
        ({"message": False}, "false"),
        ({"message": 401}, "401"),
        ({"message": 1.5}, "1.5"),
        ({"message": {}}, "[object Object]"),
    ],
)
def test_error_message_coercion(error: object, expected: str) -> None:
    assert probe._js_message(error) == expected


@pytest.mark.parametrize("request_id", [1, 2, 3])
def test_rpc_auth_errors_are_classified_after_both_reads(server: Server, request_id: int) -> None:
    server.configure(replace_response(captured(), request_id, error={"message": "401"}))
    expected = ProbeOutcome.OK if request_id == 1 else ProbeOutcome.AUTH_FAILED
    assert server.run().outcome is expected
    assert [
        event["message"].get("id") for event in server.events() if event["kind"] == "received"
    ] == [1, None, 2, 3]
