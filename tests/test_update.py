"""`update` — **자기 자신을 갈아치우는** 명령.

여기서 재는 것 대부분은 성공 경로가 아니라 **멈추는 경로**다. 출처를 모르면 진행하지
않는다, 확인 없이는 실행하지 않는다, 이미 최신이면 아무것도 안 한다. 잘못 나가면 사용자의
설치가 다른 데서 온 것으로 바뀌는데, 그걸 되돌리려면 원래 어디서 깔았는지를 알아야 한다 —
방금 그 정보를 잃은 참이다.

실제 설치 명령은 이 파일 어디에서도 실행되지 않는다. `subprocess.run` 은 전부 가짜다.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from codex_swap import cli
from codex_swap.core import selfupdate

GIT_URL = "https://github.com/wonjun-lab/codex-swap.git"
OLD = "7a5bb22ea0cd3c1b70d4f4ecfdf42ba707c2f144"
NEW = "9b82edaa641739d0dfb255bf622bbc76eade8385"


def _direct_url(monkeypatch: pytest.MonkeyPatch, payload: dict | None) -> None:
    """설치 기록을 심는다. `None` 은 기록이 아예 없는 경우(PyPI 의 일반 설치 등)."""
    monkeypatch.setattr(
        selfupdate, "_dist_direct_url", lambda: json.loads(json.dumps(payload)) if payload else None
    )


def _git_install(commit: str = OLD, revision: str | None = None) -> dict:
    vcs = {"vcs": "git", "commit_id": commit}
    if revision is not None:
        vcs["requested_revision"] = revision
    return {"url": GIT_URL, "vcs_info": vcs}


# ── 어디서 깔렸는지 알아내는가 ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("prefix", "manager"),
    [
        ("/home/u/.local/share/uv/tools/codex-swap", "uv"),
        ("/home/u/.local/share/pipx/venvs/codex-swap", "pipx"),
        ("/usr", "pip"),
        ("/home/u/venv", "pip"),
    ],
)
def test_the_installer_is_read_from_the_path(
    monkeypatch: pytest.MonkeyPatch, prefix: str, manager: str
) -> None:
    """uv 로 깔린 것을 `pip install` 로 덮으면 그 도구가 쥔 실행 파일과 어긋난다."""
    _direct_url(monkeypatch, _git_install())
    install = selfupdate.detect(prefix)
    assert install is not None
    assert install.manager == manager


def test_a_git_install_carries_its_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    _direct_url(monkeypatch, _git_install())
    install = selfupdate.detect("/usr")
    assert install is not None
    assert install.is_git
    assert install.source == f"git+{GIT_URL}"
    assert install.commit == OLD


def test_a_pinned_branch_survives_the_update(monkeypatch: pytest.MonkeyPatch) -> None:
    """`@main` 을 빼고 다시 깔면 사용자가 고정해 둔 가지에서 조용히 벗어난다."""
    _direct_url(monkeypatch, _git_install(revision="main"))
    install = selfupdate.detect("/usr")
    assert install is not None
    assert install.source.endswith("@main")
    assert selfupdate.upgrade_command(install)[-1].endswith("@main")


def test_a_local_path_install_is_understood(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _direct_url(monkeypatch, {"url": f"file://{tmp_path}", "dir_info": {}})
    install = selfupdate.detect("/usr")
    assert install is not None
    assert not install.is_git
    assert install.local_path == tmp_path
    assert not install.editable


def test_no_install_record_means_we_do_not_know(monkeypatch: pytest.MonkeyPatch) -> None:
    """**모른다는 것은 실패가 아니다.** 다만 추측으로 진행하면 안 된다."""
    _direct_url(monkeypatch, None)
    assert selfupdate.detect("/usr") is None


@pytest.mark.parametrize("payload", [{}, {"url": ""}, {"url": "npm://weird"}])
def test_an_unusable_record_is_treated_as_unknown(
    monkeypatch: pytest.MonkeyPatch, payload: dict
) -> None:
    _direct_url(monkeypatch, payload)
    assert selfupdate.detect("/usr") is None


# ── 무엇을 실행할 것인가 ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("manager", "head"),
    [("uv", ["uv", "tool", "install", "--force"]), ("pipx", ["pipx", "install", "--force"])],
)
def test_the_command_forces_a_reinstall(manager: str, head: list[str]) -> None:
    """**재설치이지 업그레이드가 아니다.**

    깃에서 깔린 판은 버전 번호가 안 움직인다 — `0.1.0` 그대로 새 커밋이 온다. 번호를 견주는
    `uv tool upgrade`·`pipx upgrade` 는 그래서 "이미 최신" 이라며 아무것도 안 할 수 있다.
    """
    install = selfupdate.Install(manager=manager, source=f"git+{GIT_URL}")
    assert selfupdate.upgrade_command(install) == [*head, f"git+{GIT_URL}"]


def test_an_editable_install_is_sent_to_git_instead(tmp_path) -> None:
    """`pip install -e` 를 재설치로 덮으면 사용자의 작업 사본이 통째로 날아간다."""
    install = selfupdate.Install(
        manager="pip", source=str(tmp_path), editable=True, local_path=tmp_path
    )
    with pytest.raises(selfupdate.UpdateError, match="editable"):
        selfupdate.upgrade_command(install)


# ── 최신인지 견주는가 ───────────────────────────────────────────────────────


def test_being_unable_to_reach_the_remote_is_not_up_to_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """못 읽은 것을 "최신" 으로 접으면, 갱신이 있는데도 없다고 믿는다."""

    def boom(*_: object, **__: object):
        raise OSError("no network")

    monkeypatch.setattr(subprocess, "run", boom)
    install = selfupdate.Install(manager="uv", source=f"git+{GIT_URL}", commit=OLD)
    assert selfupdate.latest_commit(install) is None


def test_the_remote_head_is_read_from_ls_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake(args, **_: object):
        seen.append(list(args))
        return subprocess.CompletedProcess(args, 0, f"{NEW}\trefs/heads/main\n", "")

    monkeypatch.setattr(subprocess, "run", fake)
    install = selfupdate.Install(manager="uv", source=f"git+{GIT_URL}@main", commit=OLD)
    assert selfupdate.latest_commit(install) == NEW
    assert seen[0][:2] == ["git", "ls-remote"]
    assert seen[0][2] == GIT_URL, "`git+` 접두사를 그대로 넘기면 git 이 못 읽는다"
    assert seen[0][3] == "main", "고정해 둔 가지가 아니라 기본 가지를 봤다"


# ── 명령이 멈추는 자리 ──────────────────────────────────────────────────────


@pytest.fixture
def ran(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """실제로 실행된 명령. **비어 있어야 하는 테스트가 대부분이다.**"""
    calls: list[list[str]] = []

    def fake(args, **_: object):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(cli.subprocess, "run", fake)
    return calls


def test_an_unknown_install_prints_the_command_and_stops(
    monkeypatch: pytest.MonkeyPatch, ran: list[list[str]], capsys
) -> None:
    """출처를 모르는 채 기본 주소로 밀어 넣으면, 포크에서 깔아 쓰던 설치가 원본이 된다."""
    _direct_url(monkeypatch, None)
    assert cli.main(["update"]) == 1
    assert ran == []
    out = capsys.readouterr().out
    assert selfupdate.FALLBACK_SOURCE in out, out
    assert "will not guess" in out, out


def test_already_up_to_date_runs_nothing(
    monkeypatch: pytest.MonkeyPatch, ran: list[list[str]], capsys
) -> None:
    _direct_url(monkeypatch, _git_install(commit=NEW))
    monkeypatch.setattr(selfupdate, "latest_commit", lambda *_, **__: NEW)
    assert cli.main(["update", "--yes"]) == 0
    assert ran == []
    assert "already up to date" in capsys.readouterr().out


def test_check_only_never_installs(
    monkeypatch: pytest.MonkeyPatch, ran: list[list[str]], capsys
) -> None:
    _direct_url(monkeypatch, _git_install(commit=OLD))
    monkeypatch.setattr(selfupdate, "latest_commit", lambda *_, **__: NEW)
    assert cli.main(["update", "--check"]) == 0
    assert ran == []
    assert "would run:" in capsys.readouterr().out


@pytest.mark.parametrize("answer", ["", "n", "no", "later"])
def test_saying_anything_but_yes_installs_nothing(
    monkeypatch: pytest.MonkeyPatch, ran: list[list[str]], answer: str, capsys
) -> None:
    _direct_url(monkeypatch, _git_install(commit=OLD))
    monkeypatch.setattr(selfupdate, "latest_commit", lambda *_, **__: NEW)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _="": answer)
    assert cli.main(["update"]) == 1
    assert ran == []
    assert "Left it alone" in capsys.readouterr().out


def test_yes_runs_the_command(
    monkeypatch: pytest.MonkeyPatch, ran: list[list[str]], capsys
) -> None:
    # 설치 도구를 **고정한다.** 테스트가 도는 곳의 `sys.prefix` 는 그때그때 다르고, 그걸
    # 그대로 쓰면 "uv 든 pipx 든 뭐든 하나" 같은 헐거운 단언밖에 못 쓴다.
    monkeypatch.setattr(
        selfupdate,
        "detect",
        lambda *_, **__: selfupdate.Install(manager="uv", source=f"git+{GIT_URL}", commit=OLD),
    )
    monkeypatch.setattr(selfupdate, "latest_commit", lambda *_, **__: NEW)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _="": "y")
    assert cli.main(["update"]) == 0
    assert ran == [["uv", "tool", "install", "--force", f"git+{GIT_URL}"]], ran
    assert "updated" in capsys.readouterr().out


def test_a_failing_installer_is_reported_not_swallowed(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """조용히 0 을 돌려주면 사용자는 갱신된 줄 알고 옛 판을 계속 쓴다."""
    _direct_url(monkeypatch, _git_install(commit=OLD))
    monkeypatch.setattr(selfupdate, "latest_commit", lambda *_, **__: NEW)
    monkeypatch.setattr(
        cli.subprocess, "run", lambda args, **_: subprocess.CompletedProcess(args, 3, "", "")
    )
    assert cli.main(["update", "--yes"]) == 1
    assert "failed" in capsys.readouterr().err


def test_a_missing_installer_names_itself(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """`uv: command not found` 만 뜨면 무엇을 깔아야 하는지 알 수 없다."""
    _direct_url(monkeypatch, _git_install(commit=OLD))
    monkeypatch.setattr(selfupdate, "latest_commit", lambda *_, **__: NEW)

    def gone(*_: object, **__: object):
        raise FileNotFoundError

    monkeypatch.setattr(cli.subprocess, "run", gone)
    assert cli.main(["update", "--yes"]) == 1
    err = capsys.readouterr().err
    assert "not on PATH" in err, err


def test_upgrade_is_the_same_command(
    monkeypatch: pytest.MonkeyPatch, ran: list[list[str]], capsys
) -> None:
    """`update` 를 못 떠올린 사람이 `upgrade` 를 친다. 둘 다 되어야 한다."""
    _direct_url(monkeypatch, _git_install(commit=NEW))
    monkeypatch.setattr(selfupdate, "latest_commit", lambda *_, **__: NEW)
    assert cli.main(["upgrade", "--yes"]) == 0
    assert "already up to date" in capsys.readouterr().out


# ── 다른 방식으로 깔린 경우 ─────────────────────────────────────────────────


def test_a_homebrew_install_is_left_to_homebrew(monkeypatch: pytest.MonkeyPatch) -> None:
    """**pip 이 brew 의 Cellar 를 헤집으면 안 된다.**

    brew 는 자기 안의 가상환경을 스스로 관리한다. 그 안에서 `pip install --force-reinstall`
    을 돌리면 brew 가 아는 상태와 실제가 갈리고, 그 뒤로는 brew 쪽 명령이 전부 어긋난다.
    """
    _direct_url(monkeypatch, _git_install())
    install = selfupdate.detect("/opt/homebrew/Cellar/codex-swap/0.1.0/libexec")
    assert install is not None
    assert install.manager == "brew"
    with pytest.raises(selfupdate.UpdateError, match="brew upgrade"):
        selfupdate.upgrade_command(install)


@pytest.mark.parametrize(
    "prefix",
    [
        "/opt/homebrew/Cellar/codex-swap/0.1.0/libexec",
        "/usr/local/Cellar/codex-swap/0.1.0/libexec",
        "/home/linuxbrew/.linuxbrew/Cellar/codex-swap/0.1.0/libexec",
    ],
)
def test_homebrew_is_recognised_on_every_platform(
    monkeypatch: pytest.MonkeyPatch, prefix: str
) -> None:
    """intel mac · apple silicon · linuxbrew 가 각자 다른 자리에 깐다."""
    _direct_url(monkeypatch, _git_install())
    install = selfupdate.detect(prefix)
    assert install is not None and install.manager == "brew"


def test_the_installer_script_is_shipped_and_runnable() -> None:
    """`curl … | sh` 로 안내해 두고 파일이 없으면 그 한 줄이 404 를 내려받는다."""
    import subprocess as sp
    from pathlib import Path as P

    script = P(__file__).resolve().parent.parent / "install.sh"
    assert script.is_file(), "install.sh 가 없다"
    assert script.stat().st_mode & 0o111, "실행 권한이 없다"
    # 문법이 깨진 채 올라가면 사용자의 셸에서 터진다. 우리 쪽에서 먼저 본다.
    assert sp.run(["sh", "-n", str(script)]).returncode == 0
