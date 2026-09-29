"""upstream codex 를 새 판으로 — `update --codex` 와 화면을 열 때의 확인.

재는 것은 네 가지다. 이미 최신이면 아무것도 안 한다, 부를 때는 wrapper 가 아니라
upstream 을 부른다, 그 옆의 npm 이 이기도록 PATH 를 맞춘다, npm 설치본에만 `npm install -g`
를 돌린다.

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
        if _is_update(args):
            if self.update_exit == 0 and self.after is not None:
                self.have = self.after
            return subprocess.CompletedProcess(args, self.update_exit, "", "")
        raise AssertionError(f"unexpected call: {args}")

    @property
    def updates(self) -> list[list[str]]:
        return [args for args, _ in self.calls if _is_update(args)]


def _is_update(args: list[str]) -> bool:
    return args[1:] == ["update"] or args[:2] == ["npm", "install"]


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
    """견주지 않으면 부를 때마다 같은 판을 다시 깐다."""
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
    [(_, env)] = [(a, e) for a, e in fake.calls if _is_update(a)]
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
    """스크립트는 종료 코드로만 말한다. 0 을 돌려주면 실패가 로그에서도 사라진다."""
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


# ── npm 설치본 ───────────────────────────────────────────────────────────────


@pytest.fixture
def npm_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`npm install -g` 가 까는 모양 — bin/codex 가 lib/node_modules 안의 스크립트를 가리킨다."""
    prefix = tmp_path / "node" / "v24"
    script = prefix / "lib" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    script.parent.mkdir(parents=True)
    script.write_text("#!/usr/bin/env node\n")
    script.chmod(0o755)
    (prefix / "bin").mkdir()
    link = prefix / "bin" / "codex"
    link.symlink_to(Path("..") / "lib" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js")
    monkeypatch.setattr(discovery, "resolve_codex_bin", lambda *_, **__: link)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    return link


NPM_INSTALL = ["npm", "install", "-g", "@openai/codex@latest"]


def test_an_npm_install_is_updated_with_npm(npm_codex: Path, upstream_elsewhere: Path) -> None:
    """사용자가 손으로 치던 그 명령이다. 다른 설치 방식에 npm 을 돌리면 두 번째 codex 가 생긴다."""
    assert codexupdate.update_command(npm_codex) == NPM_INSTALL
    assert codexupdate.update_command(upstream_elsewhere) == [str(upstream_elsewhere), "update"]


@pytest.fixture
def upstream_elsewhere(tmp_path: Path) -> Path:
    """npm 이 아닌 설치 — standalone 처럼 실행 파일이 그대로 놓인 자리."""
    real = tmp_path / "standalone" / "bin" / "codex"
    real.parent.mkdir(parents=True)
    real.write_text("#!/bin/sh\n")
    real.chmod(0o755)
    return real


def test_update_codex_on_an_npm_install_runs_npm(
    npm_codex: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeCodex("0.158.0", after="0.159.0")
    _latest(monkeypatch, "0.159.0")
    assert cli.cmd_update_codex(assume_yes=True, runner=fake) == 0
    assert fake.updates == [NPM_INSTALL]
    [(_, env)] = [(a, e) for a, e in fake.calls if _is_update(a)]
    assert env is not None
    assert env["PATH"].split(":")[0] == str(npm_codex.parent), env["PATH"]


# ── 화면을 열 때 ─────────────────────────────────────────────────────────────

ON = {cli.UPDATE_CODEX_ON_OPEN_ENV: ""}


def _enter(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    prompts: list[str] = []
    monkeypatch.setattr("builtins.input", lambda text="": prompts.append(text) or "")
    return prompts


def test_opening_with_a_newer_release_installs_it_then_shows_the_version(
    npm_codex: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    fake = FakeCodex("0.158.0", after="0.159.0")
    _latest(monkeypatch, "0.159.0")
    prompts = _enter(monkeypatch)
    cli.update_codex_on_open(ON, runner=fake)
    ran = [args for args, _ in fake.calls]
    assert ran == [
        [str(npm_codex), "--version"],
        NPM_INSTALL,
        [str(npm_codex), "--version"],
    ]
    out = capsys.readouterr().out
    assert "codex 0.158.0 → 0.159.0" in out, out
    assert "running: npm install -g @openai/codex@latest" in out, out
    assert "running: codex --version" in out, out
    # 곧 화면이 덮는다 — 무엇이 깔렸는지 읽을 틈을 준다.
    assert len(prompts) == 1


def test_opening_when_current_does_nothing(
    npm_codex: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    fake = FakeCodex("0.159.0")
    _latest(monkeypatch, "0.159.0")
    prompts = _enter(monkeypatch)
    cli.update_codex_on_open(ON, runner=fake)
    assert fake.updates == []
    assert prompts == []
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(("have", "latest"), [("0.158.0", None), (None, "0.159.0")])
def test_opening_without_knowing_both_sides_does_nothing(
    npm_codex: Path, monkeypatch: pytest.MonkeyPatch, have: str | None, latest: str | None
) -> None:
    """`update --codex` 와 반대다. 화면을 열 때마다 지나는 자리라, 오프라인이면 열 때마다
    재설치를 돌리게 된다."""
    fake = FakeCodex(have, after="0.159.0")
    _latest(monkeypatch, latest)
    prompts = _enter(monkeypatch)
    cli.update_codex_on_open(ON, runner=fake)
    assert fake.updates == []
    assert prompts == []


def test_opening_leaves_a_non_npm_codex_alone(
    upstream_elsewhere: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(discovery, "resolve_codex_bin", lambda *_, **__: upstream_elsewhere)
    monkeypatch.setattr(
        codexupdate, "latest_version", lambda **_: pytest.fail("asked npm for a non-npm codex")
    )
    fake = FakeCodex("0.158.0")
    cli.update_codex_on_open(ON, runner=fake)
    assert fake.calls == []


@pytest.mark.parametrize("value", ["0", "off", "false", "no"])
def test_opening_can_be_told_not_to_check(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        discovery, "resolve_codex_bin", lambda *_, **__: pytest.fail("looked for codex")
    )
    cli.update_codex_on_open({cli.UPDATE_CODEX_ON_OPEN_ENV: value}, runner=FakeCodex("0.158.0"))


def test_opening_without_codex_still_opens(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*_: object, **__: object) -> Path:
        raise discovery.UpstreamNotFound("could not find the codex binary")

    monkeypatch.setattr(discovery, "resolve_codex_bin", missing)
    cli.update_codex_on_open(ON, runner=FakeCodex("0.158.0"))


def test_a_failed_install_on_open_is_shown_and_the_screen_still_opens(
    npm_codex: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """계정 관리까지 막을 이유는 없다. 다만 실패를 삼키지는 않는다."""
    fake = FakeCodex("0.158.0", update_exit=243)
    _latest(monkeypatch, "0.159.0")
    prompts = _enter(monkeypatch)
    cli.update_codex_on_open(ON, runner=fake)
    assert "codex update failed (exit 243)" in capsys.readouterr().err
    assert [args for args, _ in fake.calls if args[1:] == ["--version"]] == [
        [str(npm_codex), "--version"]
    ]
    assert len(prompts) == 1


def test_the_registry_wait_is_short_on_open(
    npm_codex: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def latest(**kw: object) -> None:
        seen.update(kw)
        return None

    monkeypatch.setattr(codexupdate, "latest_version", latest)
    cli.update_codex_on_open(ON, runner=FakeCodex("0.158.0"))
    assert isinstance(seen["timeout"], (int, float)) and seen["timeout"] <= 15


def test_the_bare_command_checks_before_the_screen_opens(monkeypatch: pytest.MonkeyPatch) -> None:
    from codex_swap import tui

    order: list[str] = []
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr(cli, "update_codex_on_open", lambda *_, **__: order.append("check"))
    monkeypatch.setattr(tui, "run", lambda _settings: order.append("screen") or 0)
    assert cli.main([]) == 0
    assert order == ["check", "screen"]


def test_other_commands_never_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """`exec` 는 모든 codex 호출(T3 Code 의 app-server 포함)이 지난다. 거기서 npm 을 돌리면
    stdio 로 말하는 앱의 첫 줄이 설치 로그가 된다."""
    monkeypatch.setattr(
        cli, "update_codex_on_open", lambda *_, **__: pytest.fail("checked outside the screen")
    )
    monkeypatch.setattr(cli, "cmd_home", lambda _settings: 0)
    assert cli.main(["home"]) == 0
