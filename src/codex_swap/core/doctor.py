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
        "if the ChatGPT desktop app is signed in, its own codex runs with "
        "CODEX_HOME=~/.codex and will keep putting its account back. Give the CLI a "
        "home of its own: export CODEX_HOME=~/.codex-cli and "
        "CODEX_ACCOUNT_DEFAULT_HOME=~/.codex-cli, then register the accounts there",
    )


def run(settings: config.Settings) -> list[Finding]:
    """등록된 슬롯을 전부 본다. 하나가 실패해도 나머지는 계속한다."""
    active = store.active_label(settings)
    out = [check(settings, label, active) for label in store.labels(settings)]
    # 환경 쪽 문제는 **맨 앞**에 둔다. 계정마다 "서버가 거절했다" 가 줄줄이 뜨는데 그
    # 까닭이 맨 아래 있으면, 사용자는 그 전에 계정을 다시 만들기 시작한다.
    outside = drifted(settings)
    return [outside, *out] if outside is not None else out


def summary(findings: list[Finding]) -> str:
    """한 줄 요약. **아무 문제가 없다는 것도 결과다** — 조용하면 안 돈 줄 안다."""
    if not findings:
        return "no accounts registered yet. Start with: codex-swap adopt <label>"
    bad = [f for f in findings if not f.ok]
    if not bad:
        return f"all {len(findings)} account(s) are reachable"
    return f"{len(bad)} of {len(findings)} account(s) need attention"
