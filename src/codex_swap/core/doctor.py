"""무엇이 잘못됐는지, 그래서 **무엇을 하면 되는지.**

화면에 `?` 만 뜨는 상태가 실제로 있었다. 계정 이메일은 멀쩡히 보이는데 사용량 자리만
물음표이고, 왜인지는 어디에도 없었다. 사용자는 도구가 깨진 줄 알고 재설치를 했다.

원인은 SSH 로 접속한 원격에서 로그인한 것이었다. 브라우저가 없는 자리라 OAuth 콜백이
로컬로 가 버려서 `auth.json` 은 생겼는데 그 안의 토큰이 무효였다. 이메일이 보인 것은 그
파일의 `id_token` 을 **네트워크 없이** 풀어 읽기 때문이고, 사용량이 안 보인 것은 그쪽은
진짜 호출이기 때문이다. 파일이 있는지만 보는 진단으로는 절대 안 잡힌다.

그래서 여기서 재는 것은 파일의 유무가 아니라 **쓸 수 있는가**다. 그리고 답마다 다음
행동을 하나씩 붙인다 — 진단이 병명만 말하고 끝나면 사용자는 여전히 검색을 해야 한다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from codex_swap.core import config, discovery, identity, log, probe, store
from codex_swap.core.types import ProbeOutcome

OK = "ok"
AUTH = "auth"
UNREACHABLE = "unreachable"
NO_CREDENTIALS = "no_credentials"
MISMATCH = "mismatch"
DRIFT = "drift"


@dataclass(frozen=True)
class Finding:
    """한 계정에 대한 판정과 **다음 행동**."""

    label: str
    state: str
    detail: str
    fix: str = ""

    @property
    def ok(self) -> bool:
        return self.state == OK


def _login_hint(label: str) -> str:
    """로그인을 다시 하라는 말에 **함정까지** 붙인다.

    "다시 로그인하세요" 만으로는 SSH 에서 또 같은 결과가 나온다 — 사용자가 겪은 그대로다.
    """
    return (
        f"log in again for this slot: codex-swap add {label}. "
        "If you are on a remote box over SSH, do it while sitting at that machine or "
        "forward the OAuth callback port — a browser on your laptop cannot reach the "
        "listener on the remote, and the login half-finishes without saying so"
    )


def check(settings: config.Settings, label: str, active: str | None) -> Finding:
    """슬롯 하나를 실제로 써 본다."""
    auth = store.slot_auth(settings, label)
    if not auth.is_file():
        return Finding(
            label,
            NO_CREDENTIALS,
            "no auth.json in the slot",
            f"codex-swap add {label}, or codex-swap adopt {label} to keep the current login",
        )

    stored = identity.email_of(auth)
    # 활성 계정은 **기본 홈**이 사실이다. 슬롯 사본은 전환 시점의 스냅숏이라 토큰이 뒤처진다.
    home = settings.default_home if label == active else store.slot_dir(settings, label)

    try:
        codex_bin = str(discovery.resolve_codex_bin())
    except Exception as exc:
        return Finding(
            label,
            UNREACHABLE,
            f"cannot run codex: {exc}",
            "check that `codex --version` works, or set CODEX_ACCOUNT_BIN to its path",
        )

    try:
        result = probe.probe(codex_bin, str(home))
    except Exception as exc:
        return Finding(label, UNREACHABLE, f"the probe did not finish: {exc}", "try again")

    if result.outcome is ProbeOutcome.AUTH_FAILED:
        # **여기가 그 자리다.** 파일은 있는데 서버가 거절한다.
        return Finding(
            label,
            AUTH,
            f"the credentials are there but the server rejected them ({stored or 'unknown email'})",
            _login_hint(label),
        )
    if result.outcome is not ProbeOutcome.OK or result.usage is None:
        return Finding(
            label,
            UNREACHABLE,
            "could not read usage — the network or the API did not answer",
            "try again in a moment; if it keeps failing, check your connection",
        )

    live = result.usage.email
    if stored and live and stored != live:
        return Finding(
            label,
            MISMATCH,
            f"the slot says {stored} but the account answering is {live}",
            f"codex-swap adopt {label} to point this slot at the account in use",
        )

    used = result.usage.used_percent
    return Finding(label, OK, f"{used}% used" if used is not None else "reachable")


def drifted(settings: config.Settings) -> Finding | None:
    """**우리를 거치지 않고 활성이 바뀌었나.** 아니면 None.

    원장의 마지막 도착지와 지금 활성이 다르면 누군가 `auth.json` 을 직접 갈아 끼운 것이다.
    실제로 그런 환경이 있다 — ChatGPT 데스크톱 앱이 자기 codex 를 `CODEX_HOME=~/.codex` 로
    띄워 두고 **같은 파일**을 쓴다. 그 앱이 다른 계정으로 로그인돼 있으면 우리가 걸어 둔
    것을 자기 세션으로 되돌려 놓는다.

    사용자에게는 "로그인이 자꾸 풀린다" 로 보이고, 원장에는 `A -> B` 만 쌓이고 `B -> A` 는
    남지 않아 **출발점이 계속 A 인 이상한 이력**이 된다. 그 어긋남이 여기서 잡으려는 것이다.

    이 검사는 슬롯이 아니라 **환경**에 대한 것이라 계정별 검사와 따로 둔다.
    """
    expected = log.last_switch(settings)
    if expected is None:
        return None  # 아직 한 번도 안 바꿨다 — 견줄 것이 없다
    active = store.active_label(settings)
    if active == expected:
        return None
    now = active or (identity.email_of(store.active_auth(settings)) or "an unknown account")
    return Finding(
        "active",
        DRIFT,
        f"codex-swap last switched to {expected}, but {now} is live now — "
        "something changed the credentials without going through it",
        "if the ChatGPT desktop app is installed, its own codex writes ~/.codex/auth.json. "
        "Current codex-swap moves out of its way by itself — update it (codex-swap update), "
        "then run codex-swap init and follow what it says about the wiring",
    )


SHARED = "shared_with_app"


def _refresh_fingerprint(path) -> str | None:
    """그 파일의 refresh token 지문. **토큰 자체는 이 함수 밖으로 나가지 않는다.**

    값이 아니라 해시만 돌려준다 — 견주는 데는 그걸로 충분하고, 진단 출력이나 예외 메시지에
    토큰 조각이 섞일 여지를 처음부터 없앤다.
    """
    import hashlib
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    tokens = data.get("tokens") if isinstance(data, dict) else None
    token = tokens.get("refresh_token") if isinstance(tokens, dict) else None
    if not isinstance(token, str) or not token:
        return None
    # `surrogatepass` 로 인코딩한다. JSON 은 짝 없는 surrogate 를 담을 수 있고, 그대로 `encode()`
    # 하면 `UnicodeEncodeError` 가 나는데 그 예외의 `.object` 에 **토큰 원문이 통째로** 실린다.
    # 정상 토큰에서는 안 생기지만, 진단 도구가 비밀을 흘리는 길은 하나도 남기지 않는다.
    return hashlib.sha256(token.encode("utf-8", "surrogatepass")).hexdigest()


def shared_with_app(settings: config.Settings) -> list[Finding]:
    """**지금 앱이 쥔 refresh token 과 같은 것**을 쥔 자리가 있나.

    refresh token 은 쓰일 때마다 새것으로 바뀌고 옛것은 무효가 된다. 같은 토큰을 두 곳이
    쥐면, 먼저 갱신한 쪽만 살고 다른 쪽은 조용히 로그아웃된다. 앱과 codex-swap 이 한 파일을
    나눠 쓰던 기기에서 정확히 그렇게 됐다 — 앱의 codex 로그에 `401 Encountered invalidated
    oauth token` 이 수만 줄 쌓였고, 사용자에게는 "shared 로그인이 자꾸 풀린다" 로 보였다.

    **찾는 것은 현재 공유뿐이다.** 과거에 공유했다가 앱이 먼저 갱신해 버린 사본은 이미 죽은
    토큰이라 여기서는 안 보이고, 대신 계정별 검사가 "서버가 거절했다" 로 짚는다. 파일을
    옮기는 것으로는 공유가 끊기지 않으며, 그 계정을 따로 다시 로그인시켜야 끊긴다.
    """
    from codex_swap.core import wiring

    if not wiring.app_installed():
        return []
    app_home = wiring.DEFAULT_HOME.expanduser()
    ours = settings.default_home.expanduser()
    same_home = os.path.realpath(ours) == os.path.realpath(app_home)
    found: list[Finding] = []
    if same_home:
        # 분리가 안 된 채 앱과 같은 홈을 쓰고 있다 — 이것 자체가 다툼의 원인이다.
        found.append(
            Finding(
                "active",
                SHARED,
                f"codex-swap and the ChatGPT app are both using {app_home}",
                "unset CODEX_ACCOUNT_DEFAULT_HOME so codex-swap moves out of the app's way, "
                "then run codex-swap init",
            )
        )
    # 같은 홈이어도 **슬롯 비교는 끝까지 한다.** 여기서 멈추면 `run` 은 활성 라벨 하나만 프로브에서
    # 빼고, 앱의 로그인을 그대로 쥔 다른 슬롯(같은 계정을 두 이름으로 등록한 경우)은 검사로
    # 넘긴다 — 그 프로브가 토큰을 갱신하는 순간 앱이 로그아웃된다.
    app_fp = _refresh_fingerprint(app_home / "auth.json")
    if app_fp is None:
        return found

    for label in store.labels(settings):
        if _refresh_fingerprint(store.slot_auth(settings, label)) == app_fp:
            found.append(
                Finding(
                    label,
                    SHARED,
                    "this slot holds the same refresh token as the ChatGPT app — whichever "
                    "refreshes first logs the other out",
                    f"sign this account in on its own: codex-swap add {label} --force",
                )
            )
    if not same_home and _refresh_fingerprint(ours / "auth.json") == app_fp:
        found.append(
            Finding(
                "active",
                SHARED,
                f"{ours}/auth.json holds the same refresh token as the ChatGPT app",
                "switch to a slot that was signed in on its own: codex-swap use <label>",
            )
        )
    return found


def run(settings: config.Settings) -> list[Finding]:
    """등록된 슬롯을 전부 본다. 하나가 실패해도 나머지는 계속한다."""
    # **토큰을 견주는 검사를 프로브보다 먼저 한다.** 프로브는 슬롯의 토큰을 갱신하도록 되어
    # 있어서, 뒤에 두면 방금 그 갱신이 공유의 증거를 지운다 — 경고해야 할 슬롯이 "reachable"
    # 로 나왔다.
    shared = shared_with_app(settings)
    # 앱과 토큰을 나눠 쥔 자리는 **프로브하지 않는다.** 프로브가 그 토큰을 갱신하는 순간 앱 쪽
    # 사본이 무효가 되어, 진단하려다 앱을 로그아웃시킨다.
    flagged = {f.label for f in shared}
    active = store.active_label(settings)
    skip_active = "active" in flagged
    # 환경 쪽 문제는 **맨 앞**에 둔다. 계정마다 "서버가 거절했다" 가 줄줄이 뜨는데 그
    # 까닭이 맨 아래 있으면, 사용자는 그 전에 계정을 다시 만들기 시작한다.
    outside = [f for f in (drifted(settings),) if f is not None]
    out = [
        check(settings, label, active)
        for label in store.labels(settings)
        if label not in flagged and not (skip_active and label == active)
    ]
    return [*shared, *outside, *out]


def summary(findings: list[Finding]) -> str:
    """한 줄 요약. **아무 문제가 없다는 것도 결과다** — 조용하면 안 돈 줄 안다."""
    if not findings:
        return "no accounts registered yet. Start with: codex-swap adopt <label>"
    bad = [f for f in findings if not f.ok]
    if not bad:
        return f"all {len(findings)} account(s) are reachable"
    return f"{len(bad)} of {len(findings)} account(s) need attention"
