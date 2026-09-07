"""사용량 프로브 — app-server JSON-RPC 클라이언트.

`lib/codex-rate-limits.mjs` 를 대신한다. node 가 필요했던 이유는 프로브가 JavaScript 로
쓰여 있었기 때문이지 codex 를 띄우는 데 node 가 필요해서가 아니었다. `.mjs` 가 하는 일은
`spawn(codexBin, ["app-server"])` 로 자식을 띄우고 NDJSON JSON-RPC 를 주고받는 것뿐이라
표준 라이브러리 `subprocess` + `json` 이 그대로 대신한다 (설계문 §2.2).

`codex login status` 는 로그인 여부만 답하고 `codex exec --json` 이벤트에는 rate limit 이
실려 오지 않는다. 한도 수치를 공식 경로로 얻는 유일한 지점이 app-server 의
`account/rateLimits/read` 이고, 이 호출은 턴을 시작하지 않으므로 토큰을 소비하지 않는다.

바이너리 경로를 인자로 받는 이유는 재귀 때문이다. `~/.local/bin/codex` 는 dotfiles
wrapper 라, 그걸 부르면 wrapper 가 다시 rotate 를 돌리고 rotate 가 다시 이 프로브를
부른다. 호출자(`discovery`)가 이미 해석해 둔 upstream 경로만 실행한다.

돌려주는 것은 불리언이 아니라 3-variant 다 (`Ok` / `AuthFailed` / `Unknown`). 인증 실패와
네트워크·파싱 실패는 정반대의 결론으로 가고, 둘을 묶으면 하필 전환이 가장 절실한 순간에
스위처가 손을 놓는다 (설계문 §6.4.1).
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import select
import subprocess
import time
from pathlib import Path

from codex_swap.core.types import ProbeResult, Usage

DEFAULT_TIMEOUT_MS = 20_000
"""요청**당** 타임아웃. 전역 데드라인이 아니다 (설계문 §6.3)."""

_TEARDOWN_WAIT_S = 2.0
_READ_CHUNK = 65536

# 서버가 보는 값이라 `.mjs` 와 같아야 한다. 병행 기간에 한쪽만 다른 이름으로 붙으면
# 서버측 로그에서 두 구현이 구별되지 않거나(같아야 좋다) 미묘한 게이팅이 갈릴 수 있다.
_CLIENT_INFO = {
    "name": "codex-account-rotate",
    "title": "codex-account-rotate",
    "version": "1.0.0",
}

AUTH_FAILURE_RE = re.compile(
    r"\b401\b"
    r"|token_revoked"
    r"|token_expired"
    r"|invalid_grant"
    r"|unauthorized"
    # `signing?` 이 아니라 `sign(?:ing)?` 이다. 실물 문구가 둘로 갈린다 —
    # `Please sign in again.` 과 `Please try signing in again.` 이고, 원래의
    # `sign in again` 리터럴은 후자에서 `signing` 때문에 깨져 매치하지 않았다.
    # 지금까지 후자를 살려 준 것은 같은 메시지에 우연히 들어 있던 `401` 하나뿐이라,
    # 이 항목은 사실상 죽은 채로 단일 실패점을 만들고 있었다.
    r"|sign(?:ing)? in again"
    r"|logged out"
    # `account/rateLimits/read` 가 -32600 으로 접히는 자리. 실측 두 문구가 여기 걸린다
    # (codex-cli 0.153.4, `tests/fixtures/probe/`) —
    #   `codex account authentication required to read rate limits`   (auth.json 미인증)
    #   `chatgpt authentication required to read rate limits`          (auth_mode=apikey)
    # 아래 `_account_missing` 구조 판정이 앞의 것을 잡지만 뒤의 것은 못 잡는다. 그쪽은
    # `account` 가 `{"type":"apiKey"}` 로 **non-null** 이기 때문이다.
    r"|authentication required",
    # ASCII 플래그를 쓰는 이유는 `\b` 다. JS 정규식은 `/u` 없이 단어 경계를 ASCII 로
    # 판정하므로, 비-ASCII 문자가 붙은 `한401` 에서 JS 는 매치하고 Python 기본(유니코드
    # 단어 경계)은 매치하지 않는다.
    re.IGNORECASE | re.ASCII,
)
"""인증 실패의 문구 기반 증거 (설계문 §6.3).

자식의 exit code 나 stderr 가 아니라 **JSON-RPC 오류 메시지**에 이 정규식을 건다.
stderr 의 `401` 이나 exit 127 을 인증 실패로 분류하면, 네트워크 실패로 접어야 할
자리에서 계정을 갈아끼운다.

더 이상 **유일한** 증거는 아니다. 문구는 서버 것이라 언제든 바뀌므로 `_account_missing`
의 구조 판정을 나란히 둔다. 둘은 겹치지 않는다 — 각자 상대가 놓치는 축을 잡는다.
"""


class ProbeError(Exception):
    """프로브가 사용량을 얻지 못했다.

    **메시지 문자열이 계약의 일부다.** `probe()` 가 이 문자열을 `AUTH_FAILURE_RE` 에
    걸어 인증 실패(exit 3)와 그 밖(exit 1)을 가르므로, `.mjs` 가 던지던 문구를 그대로
    쓴다. 이 예외는 `probe()` 밖으로 나가지 않는다.
    """


class ProbeAuthError(ProbeError):
    """인증 실패가 **구조로** 확정됐다. 문구 매칭을 거치지 않는다.

    `AUTH_FAILURE_RE` 는 서버가 쓰는 영어 문장에 묶여 있다. 실제로 그 결합이 한 번
    끊어졌다 — `account/read` 가 `{"account": null, "requiresOpenaiAuth": true}` 를
    돌려주고 `account/rateLimits/read` 가 `-32600 "codex account authentication required
    to read rate limits"` 로 접히는 미인증 상태에서, 옛 정규식은 아무 항목도 걸지 못해
    `Unknown` 으로 갔다. 그러면 `policy.decide` 가 `Indeterminate` 로 끝내 **아무것도
    하지 않는다** — exit-3 분기가 정확히 막으려던 상황에서 스위처가 손을 놓는 것이다.
    """


def js_truthy(value: object) -> bool:
    """JavaScript 의 진리값.

    프로브의 `error` 게이트는 키 존재가 아니라 **값의 진리값**이었다(`if (account.error)`).
    그래서 `error: null`·`false`·`0`·`""` 는 전부 성공 경로로 흐르고, `error: {}` 와
    `error: []` 는 오류로 잡힌다. `if "error" in msg` 로 옮기면 이 네 경우가 전부
    뒤집힌다 (설계문 §6.4).
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, float):
        return value != 0.0 and not math.isnan(value)
    if isinstance(value, str):
        return value != ""
    # `{}` · `[]` 를 포함해 그 밖의 객체는 JS 에서 전부 참이다.
    return True


def accepts_pct(value: object) -> int | None:
    """bash 가 통과시키는 사용량 값만 정수로 돌려준다 (설계문 §6.4).

    bash 는 `jq -r` 로 문자열화한 뒤 `^[0-9]+$` 로 판정했다. jq 는 정수값 double 을
    소수점 없이 렌더링하므로 `usedPercent: 4.0` 은 `"4"` 가 되어 **통과하고**, 문자열
    `"95"` 도 통과한다. 실제로 거부되는 것은 비정수 실수·음수·boolean 이다.

    `re.fullmatch(r"[0-9]+", str(v))` 로 옮기면 안 된다 — `str(4.0)` 이 `"4.0"` 이라
    bash 가 통과시키는 값에서 실패한다.
    """
    if isinstance(value, bool):
        # bool 이 int 의 하위타입이라 **반드시 먼저** 거른다. 아니면 `True` 가 `1` 로
        # 통과해 사용량 1% 인 계정이 된다.
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value >= 0 else None
    if isinstance(value, str):
        # `isdigit()` 하나로는 안 된다. bash 의 관문은 `^[0-9]+$` 라 ASCII 전용인데
        # `str.isdigit()` 은 유니코드 숫자를 전부 참으로 본다 — FULLWIDTH DIGIT
        # (U+FF10~U+FF19) 로 쓴 "95" 는 95 로 **통과해 버리고**, SUPERSCRIPT TWO
        # (U+00B2) 는 참인데 `int()` 가 ValueError 를 던진다. 그 예외는 `ProbeError`
        # 도 `OSError` 도 아니라 `probe()` 의 두 except 를 그냥 지나쳐 호출자까지
        # 올라간다. rotate 는 매 codex 호출에 실리고 그 경로의 stderr 는 사용자
        # 터미널로 흐르므로(계약 2), 트레이스백 한 장이 그대로 화면에 실린다.
        return int(value) if value.isascii() and value.isdigit() else None
    return None


def probe(
    codex_bin: str | os.PathLike[str],
    home: str | os.PathLike[str] | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> ProbeResult:
    """한 계정의 사용량을 읽는다.

    `home` 은 그 라벨의 `CODEX_HOME` 이다. `None` 이면 자식이 주변 환경을 그대로
    물려받는다 — 활성 계정처럼 기본 홈으로 재우려는 경우다(설계문 §7.5 D3: 가짜 라벨을
    경로에 끼워 넣지 않는다).
    """
    try:
        usage = _run(codex_bin, home, timeout_ms)
    except ProbeAuthError:
        # 구조로 확정됐다. 문구를 다시 묻지 않는다.
        return ProbeResult.auth_failed()
    except ProbeError as err:
        if AUTH_FAILURE_RE.search(str(err)):
            return ProbeResult.auth_failed()
        return ProbeResult.unknown()
    except OSError:
        # spawn 실패(ENOENT·EACCES)·파이프 붕괴. 계정 상태에 대해서는 아무 말도 하지
        # 않으므로 인증 실패가 아니라 Unknown 이다 — exit 127 을 인증 실패로 읽는 것이
        # 정확히 §6.3 이 금지하는 오분류다.
        return ProbeResult.unknown()
    return ProbeResult.of(usage)


def _run(
    codex_bin: str | os.PathLike[str],
    home: str | os.PathLike[str] | None,
    timeout_ms: int,
) -> Usage:
    # 셸을 거치지 않고 인자 배열로 띄운다. 경로는 discovery 가 해석한 upstream 바이너리다.
    proc = subprocess.Popen(
        [os.fspath(codex_bin), "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        # 자식 stderr 는 버린다. `.mjs` 의 `stdio: [...,"ignore"]` 와 같고, 무엇보다
        # 이 경로의 stderr 는 매 codex 호출에서 사용자 터미널로 흘러가는 채널이다(계약 2).
        stderr=subprocess.DEVNULL,
        env=_probe_env(codex_bin, home),
    )
    try:
        return _converse(proc, timeout_ms / 1000.0)
    finally:
        _shutdown(proc)


def _converse(proc: subprocess.Popen[bytes], timeout_s: float) -> Usage:
    conn = _Conn(proc, timeout_s)

    # `initialize` 는 **응답을 기다린 뒤에** `initialized` 를 보낸다. 응답의 `error` 는
    # 검사하지 않는다 — id 만 맞으면 다음으로 간다 (설계문 §6.3, `.mjs` 와 동일).
    conn.request(1, "initialize", {"clientInfo": _CLIENT_INFO})
    conn.notify("initialized")
    account = conn.request(2, "account/read")
    limits = conn.request(3, "account/rateLimits/read")

    # 오류 게이트는 두 응답을 **모두 받은 뒤**다. `.mjs` 가 그 순서라, account 가 오류인
    # 경우에도 rateLimits 요청이 실제로 나간다.
    account_error = account.get("error")
    if js_truthy(account_error):
        raise ProbeError(f"account/read: {_js_message(account_error)}")
    limits_error = limits.get("error")
    if js_truthy(limits_error):
        message = f"account/rateLimits/read: {_js_message(limits_error)}"
        # 사용량 읽기가 이미 실패한 **뒤에만** 구조 판정을 얹는다. 이 순서가 중요하다 —
        # 앞에 두면 `account` 가 없어도 한도는 읽히는 조합에서 멀쩡한 사용량을 버리고
        # 전환해 버린다. 여기서는 `Unknown` 으로 갈 실패를 `AuthFailed` 로 **승격**만 한다.
        if _account_missing(account):
            raise ProbeAuthError(f"{message} (account/read 가 account:null 을 돌려줬다)")
        raise ProbeError(message)

    rate_limits = _prop(limits.get("result"), "rateLimits")
    if not js_truthy(rate_limits):
        raise ProbeError("rateLimits 가 비어 있다 (로그인 상태를 확인하라)")

    # 두 관문의 **순서**가 중요하다. `.mjs` 는 `typeof === "number"` 인 창을 전부 모아
    # `Math.max` 를 내고, 정수 판정(`^[0-9]+$`)은 bash 가 **그 max 에만** 건다. 그래서
    # 창별로 먼저 거르면 안 된다 — primary=4 · secondary=37.5 일 때 bash 는 max 37.5 가
    # 정규식에 걸려 "읽지 못했다"(rc=1)로 끝나지만, 창별로 걸러 37.5 를 버리면 남은 4 가
    # used_percent 가 되어 **사용량을 과소보고**하고 그 슬롯이 전환 대상 1 순위가 된다.
    # 정책이 Indeterminate 로 갈 자리에서 가짜 4% 로 전환하는 셈이다.
    def _number(v: object) -> float | None:
        """JS `typeof === "number"` 재현.

        bool 은 JS 에서 number 가 아니므로 먼저 뺀다 — Python 에서는 int 의 하위 타입이라
        그냥 두면 통과한다.
        """
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    # 표시용 필드는 게이트를 받지 않는다. `.mjs` 의 `pct()` 는 숫자면 그대로 싣고, 정수
    # 판정은 bash 가 `usedPercent` **하나에만** 건다. 그래서 primary=50 · secondary=37.5
    # 같은 조합에서 max(50)는 통과하고 secondaryPercent 는 37.5 로 표시된다.
    primary = _number(_prop(_prop(rate_limits, "primary"), "usedPercent"))
    secondary = _number(_prop(_prop(rate_limits, "secondary"), "usedPercent"))

    numeric = [v for v in (primary, secondary) if v is not None]
    if not numeric:
        raise ProbeError("usedPercent 를 읽지 못했다")
    # 관문의 **순서**가 중요하다. 창별로 먼저 거르면 primary=4 · secondary=37.5 에서
    # 37.5 가 버려지고 남은 4 가 used_percent 가 되어 사용량을 과소보고한다 — bash 는
    # max 37.5 가 정규식에 걸려 "읽지 못했다"(rc=1)로 끝나는 자리다. 정책이
    # Indeterminate 로 갈 곳에서 가짜 4% 로 전환하게 되므로 순서를 지킨다.
    used = accepts_pct(max(numeric))
    if used is None:
        raise ProbeError("usedPercent 가 정수가 아니다")
    windows = [used]

    acct = _prop(account.get("result"), "account")
    email = _prop(acct, "email")
    # JS `??` 는 null·undefined 만 넘긴다. 빈 문자열 planType 은 폴백하지 않고 그대로다.
    plan = _prop(acct, "planType")
    if plan is None:
        plan = _prop(rate_limits, "planType")

    return Usage(
        # 창이 둘이면 먼저 막히는 쪽이 실질 한도다.
        used_percent=max(windows),
        email=email if isinstance(email, str) else None,
        plan_type=plan if isinstance(plan, str) else None,
        primary_percent=primary,
        secondary_percent=secondary,
        resets_at=_as_epoch(_prop(_prop(rate_limits, "primary"), "resetsAt")),
        # 쿠폰은 `rateLimits` 밖, 응답 최상위에 있다. `bash`/`.mjs` 는 이 값을 읽지
        # 않았으므로 패리티 대상이 아니고, 없으면 없는 대로 None 이다.
        reset_credits=_as_count(_prop(_prop(limits.get("result"), "rateLimitResetCredits"),
                                      "availableCount")),
        reached=_reached(_prop(rate_limits, "rateLimitReachedType")),
    )


class _Conn:
    """NDJSON JSON-RPC 한 세션.

    프레이밍은 개행 구분이고 `Content-Length` 헤더는 쓰지 않는다. 타임아웃은 전역
    데드라인이 아니라 **요청당**이다 (설계문 §6.3) — 전역으로 두면 앞선 요청이 느렸을 때
    뒤의 요청이 아직 살아 있는데도 잘린다.
    """

    def __init__(self, proc: subprocess.Popen[bytes], timeout_s: float) -> None:
        if proc.stdin is None or proc.stdout is None:
            raise ProbeError("app-server 파이프를 열지 못했다")
        self._stdin = proc.stdin
        self._stdout = proc.stdout
        self._fd = proc.stdout.fileno()
        self._timeout_s = timeout_s
        self._buf = b""

    def notify(self, method: str, params: dict[str, object] | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def request(
        self,
        request_id: int,
        method: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        deadline = time.monotonic() + self._timeout_s
        while True:
            message = self._next_message(deadline, method)
            message_id = message.get("id")
            # `.mjs` 는 id 가 null 이 아니고 대기 중인 것과 같을 때만 깨운다. 그 사이의
            # 서버 통지와 다른 id 의 응답은 그냥 흘려보낸다.
            if message_id is None or isinstance(message_id, bool):
                continue
            if message_id == request_id:
                return message

    def _next_message(self, deadline: float, method: str) -> dict[str, object]:
        while True:
            newline = self._buf.find(b"\n")
            if newline < 0:
                self._fill(deadline, method)
                continue
            line = self._buf[:newline]
            self._buf = self._buf[newline + 1 :]
            text = line.decode("utf-8", "replace").strip()
            # app-server 는 stdout 에 진단 줄을 섞어 흘린다. `{` 로 시작하지 않거나
            # 파싱에 실패한 줄은 조용히 버린다 (`.mjs` 와 같다).
            if not text.startswith("{"):
                continue
            try:
                message = json.loads(text)
            except ValueError:
                continue
            if isinstance(message, dict):
                return message

    def _fill(self, deadline: float, method: str) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProbeError(f"timeout: {method}")
        # 버퍼드 리더의 `readline()` 은 데이터가 올 때까지 무한정 막힌다. fd 를 직접
        # 골라야 요청당 데드라인이 성립한다.
        ready, _, _ = select.select([self._fd], [], [], remaining)
        if not ready:
            raise ProbeError(f"timeout: {method}")
        chunk = os.read(self._fd, _READ_CHUNK)
        if not chunk:
            # 자식이 응답 없이 끝났다. `.mjs` 는 여기서 unref 된 타이머 때문에 그냥
            # 빈 stdout 으로 종료했고 bash 가 rc=1 로 접었다 — 결론(Unknown)은 같고
            # 여기서는 남은 타임아웃을 기다리지 않는다.
            raise ProbeError(f"app-server closed stdout before answering {method}")
        self._buf += chunk

    def _send(self, payload: dict[str, object]) -> None:
        self._stdin.write(json.dumps(payload).encode("utf-8") + b"\n")
        self._stdin.flush()


def _probe_env(
    codex_bin: str | os.PathLike[str],
    home: str | os.PathLike[str] | None,
) -> dict[str, str]:
    """자식에게 줄 환경.

    `CODEX_ROTATE_SKIP=1` 은 프로브가 띄운 codex 가 다시 회전을 돌리는 재귀를 막고,
    `CODEX_HOME` 은 슬롯 디렉토리를 자식의 홈으로 준다 — 그래서 프로브가 보관된 토큰을
    갱신해 주고 사본이 만료로 썩지 않는다(계약 4). `CODEX_ROTATE_FORCE` 는 상속됐을 수도
    있는 표식이라 지운다. 프로브는 계정을 갈아끼우지 않는다.
    """
    env = os.environ.copy()
    env["CODEX_ROTATE_SKIP"] = "1"
    env.pop("CODEX_ROTATE_FORCE", None)
    if home is not None:
        env["CODEX_HOME"] = os.fspath(home)

    # 탐색된 경로의 **lexical** parent 를 PATH 앞에 붙인다. npm(nvm) 설치본의 codex 는
    # `#!/usr/bin/env node` 스크립트라, 축소 PATH(systemd·훅)에서 그대로 띄우면
    # `env: 'node': No such file or directory` 로 127 에 죽는다 (설계문 §6.2, 실측).
    # `resolve()` 를 쓰면 심링크를 따라가 node 가 없는
    # `node_modules/@openai/codex/bin` 으로 가므로 보정 자체가 무의미해진다.
    # `.mjs` 는 이 보정을 하지 않았고, 대화형 PATH 에 node 가 있어서 가려져 있었다.
    located_dir = str(Path(os.fspath(codex_bin)).parent)
    if located_dir not in ("", "."):
        current = env.get("PATH", "")
        env["PATH"] = f"{located_dir}{os.pathsep}{current}" if current else located_dir
    return env


def _shutdown(proc: subprocess.Popen[bytes]) -> None:
    """어떤 경로로 빠져나가든 자식을 남기지 않는다.

    파이프를 먼저 닫아 app-server 가 스스로 끝날 기회를 준 뒤 terminate → 짧은 대기 →
    kill 로 조인다. `.mjs` 는 `child.kill()` 한 번이 전부였고 종료를 확인하지 않았다.
    프로브는 매 codex 호출에 실리므로 여기서 새는 자식은 그대로 누적된다.
    """
    for stream in (proc.stdin, proc.stdout):
        if stream is not None:
            with contextlib.suppress(OSError):
                stream.close()
    proc.terminate()
    try:
        proc.wait(timeout=_TEARDOWN_WAIT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=_TEARDOWN_WAIT_S)


def _prop(obj: object, key: str) -> object | None:
    """JS 의 `obj?.key`. 객체가 아니면 undefined 다.

    `rateLimits` 가 숫자·문자열처럼 진리값만 참인 값으로 와도 죽지 않아야 한다 —
    JS 는 그 경우 조용히 undefined 를 내고 아래의 "usedPercent 를 읽지 못했다" 로 간다.
    """
    return obj.get(key) if isinstance(obj, dict) else None


def _account_missing(account: dict[str, object]) -> bool:
    """`account/read` 가 계정을 못 내놨는가 — 인증 실패의 구조적 증거.

    보는 것은 `result.account` **하나뿐**이다. 이 판정을 실측으로 좁힌 과정이 그대로
    근거다 (`tests/fixtures/probe/`, codex-cli 0.153.4).

    | 상태 | `result.account` | `requiresOpenaiAuth` |
    | --- | --- | --- |
    | 정상 (36% 를 정상 보고) | `{type, email, planType}` | **`true`** |
    | 토큰 폐기 (401) | `{type, email, planType}` | **`true`** |
    | access_token 빈 문자열 (401) | `{type, email, planType}` | **`true`** |
    | id_token 만료 (401) | `{type, email, planType}` | **`true`** |
    | 미인증 (-32600) | `null` | `true` |
    | auth_mode=apikey (-32600) | `{"type":"apiKey"}` | `true` |

    `requiresOpenaiAuth` 는 **정상 계정에도 항상 참**이다. "이 배포는 OpenAI 인증을
    요구한다" 는 서버 구성값이지 이 계정이 인증에 실패했다는 뜻이 아니다. 그것까지
    인증 실패로 읽으면 모든 계정이 매번 AuthFailed 로 분류되어, 살아 있는 계정을 끝없이
    갈아끼우고 `Ok` 경로는 영영 도달하지 못한다. 그래서 이 필드는 **보지 않는다.**

    `account` 가 비는 것은 자격증명이 없거나 갱신할 수 없을 때뿐이다. 그리고 이 응답은
    로컬 `auth.json` 파싱에서 나오므로 네트워크가 죽어도 계정이 있으면 채워진다 —
    호출부가 "한도 읽기 실패" 와 겹쳐 볼 때 네트워크 실패와 갈리는 근거가 이것이다.
    """
    return _prop(account.get("result"), "account") is None


def _reached(value: object) -> bool:
    """bash 의 `jq -r '.reached // empty'` 가 비어 있지 않은가 (설계문 §6.4).

    jq 의 `//` 는 missing·null·**false** 만 접는다. `0` 은 `"0"` 으로 남아 비어 있지
    않으므로 bash 는 `reached: 0` 을 **소진으로 처리한다**. `if payload.get("reached")`
    는 이것을 false 로, `is not None` 은 `reached: false` 를 참으로 뒤집는다 — 둘 다
    틀린다.

    빈 문자열도 jq 렌더링이 비어서 bash 가 접는다. 서버가 주는 값은 null 아니면
    `"primary"` 류 문자열이라 실물에 없는 구석이지만, 술어를 반만 옮기면 다음 사람이
    이 docstring 을 믿고 잘못된 결론을 낸다. 세 값을 다 접는다.
    """
    return value is not None and value is not False and value != ""


def _as_count(value: object) -> int | None:
    """음이 아닌 정수만 받는다. 표시 전용이라 느슨하게, 다만 거짓말은 하지 않는다.

    `bool` 을 먼저 거르는 이유는 `accepts_pct` 와 같다 — `True` 가 `1` 로 통과하면
    "쿠폰 1개" 라는 없는 사실이 화면에 뜬다.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _as_epoch(value: object) -> int | None:
    """`resetsAt`. bash 는 이 값을 검증하지 않고 표시에만 썼으므로 느슨하게 받는다."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _js_message(error: object) -> str:
    """`.mjs` 의 `${err.message}` 보간을 재현한다.

    이 문자열이 `AUTH_FAILURE_RE` 가 보는 대상이라 형태 자체가 계약이다. JS 에서
    객체가 아니거나 `message` 키가 없으면 `undefined` 가 그대로 찍힌다 — 그 문자열은
    정규식에 걸리지 않으므로 Unknown 으로 접힌다. 실물에서 오는 값은 문자열뿐이다.
    """
    if not isinstance(error, dict) or "message" not in error:
        return "undefined"
    message = error["message"]
    if isinstance(message, str):
        return message
    if message is None:
        return "null"
    if isinstance(message, bool):
        return "true" if message else "false"
    if isinstance(message, int | float):
        return str(message)
    return "[object Object]"
