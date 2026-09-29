"""`update --codex` — upstream codex 를 **codex 자신의** `update` 로 갈아 끼운다.

재는 것은 세 가지다. 이미 최신이면 아무것도 안 한다(매일 도는 타이머가 부른다), 부를
때는 wrapper 가 아니라 upstream 을 부른다, 그 옆의 npm 이 이기도록 PATH 를 맞춘다.

실제 codex 도 네트워크도 건드리지 않는다. 실행과 조회는 전부 가짜다.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codex_swap import cli
from codex_swap.core import codexupdate, discovery

# ── 판 번호 ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "version"),
    [
        ("codex-cli 0.158.0\n", "0.158.0"),
        ("codex-cli 0.159.0-alpha.2", "0.159.0-alpha.2"),
        ("", None),
        ("codex-cli dev", None),
    ],
)
def test_the_version_is_read_from_codex_output(text: str, version: str | None) -> None:
    assert codexupdate.parse_version(text) == version


@pytest.mark.parametrize(
    ("latest", "current", "newer"),
    [
        ("0.158.0", "0.157.1", True),
        ("0.158.0", "0.158.0", False),
        ("0.158.0", "0.159.0-alpha.1", False),
        ("0.158.0", "0.158.0-alpha.3", True),
        ("0.160.0", "0.99.9", True),
    ],
)
def test_only_a_later_release_counts_as_newer(latest: str, current: str, newer: bool) -> None:
    """사전판을 깔아 둔 사람에게 `latest` 는 옛 판이다. 그리로 가는 것은 내려가는 것이다.
    `0.99` 와 `0.160` 을 문자열로 견주면 거꾸로 나온다."""
    assert codexupdate.is_newer(latest, current) is newer


def test_the_latest_comes_from_the_npm_beside_codex() -> None:
    """HTTP 는 직접 하지 않는다 — README 가 이 패키지에 HTTP 클라이언트가 없다고 약속한다."""
    seen: list[tuple[list[str], object]] = []

    def npm(args: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        seen.append((list(args), kwargs.get("env")))
        return subprocess.CompletedProcess(args, 0, "0.158.0\n", "")

    env = {"PATH": "/n/v24/bin:/usr/bin"}
    assert codexupdate.latest_version(env=env, runner=npm) == "0.158.0"
    assert seen == [(["npm", "view", "@openai/codex", "version"], env)]


@pytest.mark.parametrize("failure", ["exit", "missing"])
def test_an_unreachable_registry_is_unknown_not_current(failure: str) -> None:
    def npm(args: list[str], **_: object) -> subprocess.CompletedProcess:
        if failure == "missing":
            raise FileNotFoundError("npm")
        return subprocess.CompletedProcess(args, 1, "", "ENOTFOUND registry.npmjs.org")

    assert codexupdate.latest_version(runner=npm) is None


def test_the_package_has_no_http_client() -> None:
    import codex_swap

    root = Path(codex_swap.__file__).parent
    for source in root.rglob("*.py"):
        text = source.read_text()
        for client in ("urllib.request", "http.client", "import requests", "import httpx"):
            assert client not in text, f"{source.name} imports {client}"


# ── 명령 ─────────────────────────────────────────────────────────────────────


class FakeCodex:
    """`--version` 과 `update` 만 아는 가짜. 갱신이 끝나면 판이 바뀐다."""

    def __init__(self, have: str | None, after: str | None = None, update_exit: int = 0) -> None:
        self.have = have
        self.after = after
        self.update_exit = update_exit
        self.calls: list[tuple[list[str], dict[str, str] | None]] = []

    def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        env = kwargs.get("env")
        self.calls.append((list(args), env if isinstance(env, dict) else None))
        if args[1:] == ["--version"]:
            if self.have is None:
                return subprocess.CompletedProcess(args, 1, "", "broken")
            return subprocess.CompletedProcess(args, 0, f"codex-cli {self.have}\n", "")
        if args[1:] == ["update"]:
            if self.update_exit == 0 and self.after is not None:
                self.have = self.after
            return subprocess.CompletedProcess(args, self.update_exit, "", "")
        raise AssertionError(f"unexpected call: {args}")

    @property
    def updates(self) -> list[list[str]]:
        return [args for args, _ in self.calls if args[1:] == ["update"]]


@pytest.fixture
def upstream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """nvm 처럼 node 옆에 놓인 upstream. wrapper 는 다른 자리에 있다."""
    bin_dir = tmp_path / "node" / "v24" / "bin"
    bin_dir.mkdir(parents=True)
    real = bin_dir / "codex"
    real.write_text("#!/usr/bin/env node\n")
    real.chmod(0o755)
    monkeypatch.setattr(discovery, "resolve_codex_bin", lambda *_, **__: real)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    return real


def _latest(monkeypatch: pytest.MonkeyPatch, version: str | None) -> None:
    monkeypatch.setattr(codexupdate, "latest_version", lambda **_: version)


def test_already_current_runs_nothing(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """매일 도는 타이머가 부른다. 견주지 않으면 날마다 같은 판을 다시 깐다."""
    fake = FakeCodex("0.158.0")
    _latest(monkeypatch, "0.158.0")
    assert cli.cmd_update_codex(assume_yes=True, runner=fake) == 0
    assert fake.updates == []
    assert "already up to date" in capsys.readouterr().out


def test_a_newer_release_runs_the_upstream_update(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    fake = FakeCodex("0.157.1", after="0.158.0")
    _latest(monkeypatch, "0.158.0")
    assert cli.cmd_update_codex(assume_yes=True, runner=fake) == 0
    assert fake.updates == [[str(upstream), "update"]]
    out = capsys.readouterr().out
    assert "have 0.157.1 · latest 0.158.0" in out, out
    assert "codex is now 0.158.0" in out, out
    assert "until they restart" in out, out


def test_the_update_finds_the_npm_next_to_codex(
    upstream: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """npm 설치본의 `update` 는 PATH 의 npm 을 부른다. 다른 node 의 npm 이 이기면 새 판은
    **다른 prefix** 에 깔리고 쓰는 쪽은 그대로다 — 성공이라고 찍히면서."""
    fake = FakeCodex("0.157.1", after="0.158.0")
    _latest(monkeypatch, "0.158.0")
    cli.cmd_update_codex(assume_yes=True, runner=fake)
    [(_, env)] = [(a, e) for a, e in fake.calls if a[1:] == ["update"]]
    assert env is not None
    assert env["PATH"].split(":")[0] == str(upstream.parent), env["PATH"]


def test_check_only_never_updates(upstream: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    fake = FakeCodex("0.157.1")
    _latest(monkeypatch, "0.158.0")
    assert cli.cmd_update_codex(check_only=True, runner=fake) == 0
    assert fake.updates == []
    assert f"would run: {upstream} update" in capsys.readouterr().out


def test_a_prerelease_ahead_of_latest_is_left_alone(
    upstream: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeCodex("0.159.0-alpha.1")
    _latest(monkeypatch, "0.158.0")
    assert cli.cmd_update_codex(assume_yes=True, runner=fake) == 0
    assert fake.updates == []


@pytest.mark.parametrize(("have", "latest"), [("0.157.1", None), (None, "0.158.0")])
def test_not_knowing_either_side_still_updates(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, have: str | None, latest: str | None
) -> None:
    """레지스트리를 못 읽은 것을 "최신" 으로 접으면 갱신이 있는데도 없다고 믿는다. 판을
    못 읽는 codex 는 깨졌을 수 있고, 그때야말로 새로 까는 것이 맞다."""
    fake = FakeCodex(have, after="0.158.0")
    _latest(monkeypatch, latest)
    assert cli.cmd_update_codex(assume_yes=True, runner=fake) == 0
    assert fake.updates == [[str(upstream), "update"]]


@pytest.mark.parametrize("answer", ["", "n", "later"])
def test_saying_anything_but_yes_updates_nothing(
    upstream: Path, monkeypatch: pytest.MonkeyPatch, answer: str, capsys
) -> None:
    fake = FakeCodex("0.157.1")
    _latest(monkeypatch, "0.158.0")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _="": answer)
    assert cli.cmd_update_codex(runner=fake) == 1
    assert fake.updates == []
    assert "Left it alone" in capsys.readouterr().out


def test_a_failed_update_is_reported_not_swallowed(
    upstream: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """타이머는 종료 코드로만 말한다. 0 을 돌려주면 실패가 로그에서도 사라진다."""
    fake = FakeCodex("0.157.1", update_exit=2)
    _latest(monkeypatch, "0.158.0")
    with pytest.raises(cli.CliError, match="exit 2"):
        cli.cmd_update_codex(assume_yes=True, runner=fake)


def test_no_codex_names_the_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*_: object, **__: object) -> Path:
        raise discovery.UpstreamNotFound("could not find the codex binary")

    monkeypatch.setattr(discovery, "resolve_codex_bin", missing)
    with pytest.raises(cli.CliError, match="npm install -g @openai/codex"):
        cli.cmd_update_codex(assume_yes=True, runner=FakeCodex("0.157.1"))


def test_the_flag_reaches_the_codex_update(monkeypatch: pytest.MonkeyPatch) -> None:
    """`update --codex` 가 codex-swap 자신을 갈아 끼우는 쪽으로 새면 안 된다."""
    seen: dict[str, object] = {}
    monkeypatch.setattr(cli, "cmd_update_codex", lambda **kw: seen.setdefault("codex", kw) and 0)
    monkeypatch.setattr(cli, "cmd_update", lambda **kw: pytest.fail("updated codex-swap instead"))
    assert cli.main(["update", "--codex", "--check"]) == 0
    assert seen["codex"] == {"check_only": True, "assume_yes": False}
