"""설치 **순서**가 달라도 막히지 않는가.

`test_onboarding` 은 상태 하나하나를 잰다. 여기서 재는 것은 **전이**다 — codex 를 나중에
깔고, 앱을 나중에 깔고, 그 앱을 다시 지우는 동안 도구가 계속 말이 되는가. 각 상태가 따로
성립하는 것으로는 부족하다. 사용자의 기계는 한 상태에 머물지 않고, 가장 잘 깨지는 자리는
언제나 그 사이다.

**한 기계를 만들어 놓고 순서대로 밟는다.** 단계마다 `init` 이 무엇을 말하는지 보는데,
여기서 중요한 것은 "성공하는가" 가 아니라 **"막혔을 때 다음 한 걸음이 있는가"** 다.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

from codex_swap import cli
from codex_swap.core import config, discovery, wiring


class Machine:
    """임시 HOME 하나. codex 와 앱을 켜고 끌 수 있다."""

    def __init__(self, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.home = home
        self.mp = monkeypatch
        self.app = home / "Applications/ChatGPT.app"
        self._codex = False

        monkeypatch.setenv("HOME", str(home))
        # wrapper 를 놓을 자리가 PATH 에 있어야 한다 — 없으면 `init` 이 (옳게) 그 사실을
        # 지적하고, 이 테스트가 보려는 것은 그 경고가 아니다.
        (home / ".local/bin").mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("PATH", f"{home / '.local/bin'}{os.pathsep}{os.environ['PATH']}")
        for var in ("CODEX_ACCOUNT_DEFAULT_HOME", "CODEX_ACCOUNTS_DIR", "CODEX_HOME"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(wiring, "APP_PATHS", (str(self.app),))
        monkeypatch.setattr(wiring, "DEFAULT_HOME", home / ".codex")
        monkeypatch.setattr(wiring, "ISOLATED_PATH", home / ".codex-cli")
        monkeypatch.setattr(wiring, "WRAPPER", home / ".local/bin/codex")
        # PATH 에 이 기기의 진짜 wrapper 가 걸리면 배선 판정이 그것을 잡는다.
        monkeypatch.setattr(wiring.shutil, "which", lambda _: None)

        def resolve():
            if not self._codex:
                raise RuntimeError("codex not installed")
            return Path("/bin/true")

        monkeypatch.setattr(discovery, "resolve_codex_bin", resolve)
        monkeypatch.setattr(cli.discovery, "resolve_codex_bin", resolve)

    # ── 세상이 바뀌는 사건들 ──
    def install_codex(self) -> None:
        self._codex = True

    def install_app(self) -> None:
        self.app.mkdir(parents=True, exist_ok=True)

    def remove_app(self) -> None:
        self.app.rmdir()

    def log_in(self, email: str = "a@example.com") -> None:
        """지금 홈에 로그인해 둔다."""
        self._auth(config.load().default_home / "auth.json", email)

    def register(self, *labels: str) -> None:
        for i, label in enumerate(labels):
            self._auth(config.load().accounts_dir / label / "auth.json", f"{label}@example.com")
            if i == 0:
                self.log_in(f"{label}@example.com")

    # ── 보기 ──
    def init(self, capsys) -> tuple[int, str]:
        code = cli.main(["init"])
        return code, capsys.readouterr().out

    @property
    def settings(self) -> config.Settings:
        return config.load()

    @staticmethod
    def _auth(path: Path, email: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        claims = base64.urlsafe_b64encode(json.dumps({"email": email}).encode())
        path.write_text(json.dumps({"tokens": {"id_token": f"h.{claims.decode().rstrip('=')}.s"}}))


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Machine:
    return Machine(tmp_path / "home", monkeypatch)


# ── 순서 1: codex-swap 을 가장 먼저 ────────────────────────────────────────


def test_swap_first_then_codex_then_accounts(machine: Machine, capsys) -> None:
    """아무것도 없는 기계에서 시작해 하나씩 갖춰 간다."""
    code, out = machine.init(capsys)
    assert code == 1 and "codex was not found" in out, out

    machine.install_codex()
    code, out = machine.init(capsys)
    assert "no accounts yet" in out, out
    assert "adopt" in out, "무엇을 해야 하는지가 없다"

    machine.register("work", "personal")
    code, out = machine.init(capsys)
    assert code == 0, out
    assert "ready" in out, out

    # **파일이 실제로 생겼는지 본다.** "wired" 라고 적는 것과 놓는 것은 다르다 — 출력만
    # 재던 동안, 놓지 않고 경로만 찍도록 바꿔도 테스트가 통과했다.
    wrapper = machine.home / ".local/bin/codex"
    assert wrapper.is_file(), "배선했다고 해 놓고 파일이 없다"
    assert wrapper.stat().st_mode & 0o111, "실행 권한이 없어 PATH 에서 안 잡힌다"
    assert "codex-swap exec" in wrapper.read_text()


# ── 순서 2: 잘 쓰던 기계에 앱이 나중에 들어온다 ────────────────────────────


def test_installing_the_app_later_keeps_the_accounts(machine: Machine, capsys) -> None:
    """**가장 위험한 전이다.** 홈이 바뀌는데 계정까지 따라가면 전부 잃는다."""
    machine.install_codex()
    machine.register("work", "personal")
    before = machine.settings
    assert before.default_home.name == ".codex"

    machine.install_app()
    after = machine.settings

    assert after.default_home.name == ".codex-cli", "앱에게 자리를 안 비켰다"
    assert after.accounts_dir == before.accounts_dir, "계정이 홈을 따라갔다"

    _, out = machine.init(capsys)
    assert "work" in out and "personal" in out, out
    assert "kept apart" in out, out


def test_installing_the_app_later_points_at_the_stranded_login(machine: Machine, capsys) -> None:
    """자격증명은 따라오지 않는다. 그 사실을 말하지 않으면 로그아웃된 것으로 보인다."""
    machine.install_codex()
    machine.register("work", "personal")
    machine.install_app()

    _, out = machine.init(capsys)
    assert "your login is still in" in out, out
    assert "codex-swap use" in out, "슬롯이 있으니 전환 한 번이면 된다"


# ── 순서 3: 앱을 먼저 쓰던 기계 ────────────────────────────────────────────


def test_app_first_then_swap_never_touches_the_app_home(machine: Machine, capsys) -> None:
    """앱이 먼저 있던 기계에서는 처음부터 비켜선 채로 시작한다."""
    machine.install_app()
    machine.install_codex()
    s = machine.settings
    assert s.default_home.name == ".codex-cli"

    machine.register("work", "personal")
    _, out = machine.init(capsys)
    assert "ready" in out, out
    # 앱의 자리에 우리 활성 자격증명을 만들지 않았다.
    assert not (machine.home / ".codex/auth.json").exists()


# ── 순서 4: 앱을 **지운다** (놓치기 쉬운 역방향) ───────────────────────────


def test_removing_the_app_brings_the_home_back(machine: Machine) -> None:
    machine.install_codex()
    machine.install_app()
    assert machine.settings.default_home.name == ".codex-cli"
    machine.remove_app()
    assert machine.settings.default_home.name == ".codex"


def test_removing_the_app_does_not_silently_lose_the_login(machine: Machine, capsys) -> None:
    """**앱을 지웠을 뿐인데 codex 가 로그아웃된다.**

    분리해 쓰던 자격증명은 `~/.codex-cli` 에 남고 홈은 `~/.codex` 로 돌아간다. 앞 방향만
    보면 이 자리가 비고, 사용자는 앱을 지운 것과 로그아웃을 잇지 못한다.
    """
    machine.install_codex()
    machine.install_app()
    machine.register("work", "personal")  # 분리된 홈에 로그인
    machine.remove_app()

    _, out = machine.init(capsys)
    assert "your login is still in" in out, out
    assert ".codex-cli" in out, out


# ── 순서 5: 앱을 켰다 껐다 해도 같은 곳으로 돌아오는가 ─────────────────────


def test_the_split_is_decided_every_time_not_stored(machine: Machine) -> None:
    """저장해 두면 그 값이 낡는다. 매번 다시 정하므로 어느 순서로 오가도 같은 답이다."""
    machine.install_codex()
    seen = []
    for _ in range(2):
        machine.install_app()
        seen.append(machine.settings.default_home.name)
        machine.remove_app()
        seen.append(machine.settings.default_home.name)
    assert seen == [".codex-cli", ".codex", ".codex-cli", ".codex"], seen


# ── 어느 순서로 와도 계정은 한 자리에 있는가 ───────────────────────────────


@pytest.mark.parametrize("app_first", [True, False])
def test_accounts_live_in_one_place_whatever_the_order(machine: Machine, app_first: bool) -> None:
    """**여기가 갈리면 사용자는 계정을 두 번 등록하게 된다.**

    앱을 언제 깔았느냐에 따라 계정이 다른 곳에 쌓이면, 앱을 깐 뒤 등록한 계정과 그 전에
    등록한 계정이 서로 보이지 않는다.
    """
    machine.install_codex()
    if app_first:
        machine.install_app()
        machine.register("work")
    else:
        machine.register("work")
        machine.install_app()
    assert machine.settings.accounts_dir == machine.home / ".codex/accounts"
