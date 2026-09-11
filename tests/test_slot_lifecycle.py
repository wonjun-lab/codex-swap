"""계정 슬롯 이름 변경·삭제가 CLI 와 TUI 에서 같은 경계를 지키는가."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

from codex_swap import cli, tui
from codex_swap.core import cache, config, identity, paths, store


def _write_auth(path: Path, email: str) -> None:
    """테스트 슬롯에 email 을 읽을 수 있는 최소 자격증명을 쓴다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode())
    token = payload.decode().rstrip("=")
    path.write_text(json.dumps({"tokens": {"id_token": f"h.{token}.s"}}))


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> config.Settings:
    """두 표면이 매번 똑같은 격리 상태에서 시작하게 한다."""
    home = tmp_path / "home"
    accounts = home / ".codex/accounts"
    accounts.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_ACCOUNT_DEFAULT_HOME", str(home / ".codex"))
    monkeypatch.setenv("CODEX_ACCOUNTS_DIR", str(accounts))
    settings = config.load()
    _write_auth(store.active_auth(settings), "a@example.com")
    _write_auth(store.slot_auth(settings, "master"), "a@example.com")
    _write_auth(store.slot_auth(settings, "shared"), "b@example.com")
    return settings


def _snapshot(root: Path) -> tuple[tuple[str, str, bytes], ...]:
    """거부 전후의 디스크를 파일 종류와 바이트까지 비교한다."""
    found: list[tuple[str, str, bytes]] = []
    for current, directories, files in os.walk(root, followlinks=False):
        here = Path(current)
        for name in sorted([*directories, *files]):
            path = here / name
            relative = str(path.relative_to(root))
            if path.is_symlink():
                found.append((relative, "symlink", os.readlink(path).encode()))
            elif path.is_dir():
                found.append((relative, "directory", b""))
            else:
                found.append((relative, "file", path.read_bytes()))
    return tuple(sorted(found))


def _rename(
    surface: str,
    settings: config.Settings,
    old: str,
    new: str,
    capsys: pytest.CaptureFixture[str],
) -> str:
    """같은 요청을 CLI 또는 순수 TUI 동작에 넣고 거부 문구를 돌려준다."""
    if surface == "cli":
        assert cli.main(["rename", old, new]) == 1
        output = capsys.readouterr().err
        assert output.startswith("codex-swap: ") and output.endswith("\n")
        return output.removeprefix("codex-swap: ")[:-1]
    return tui.do_rename(tui.build_view(settings), old, new).message


def _remove(
    surface: str,
    settings: config.Settings,
    label: str,
    capsys: pytest.CaptureFixture[str],
) -> str:
    """두 표면의 삭제 요청을 같은 확정 입력으로 실행한다."""
    if surface == "cli":
        assert cli.main(["remove", "--yes", label]) == 1
        output = capsys.readouterr().err
        assert output.startswith("codex-swap: ") and output.endswith("\n")
        return output.removeprefix("codex-swap: ")[:-1]
    return tui.do_remove(tui.build_view(settings), label, "y").message


def test_direct_cli_rename_keeps_cli_error_boundary(env: config.Settings) -> None:
    """공유 코어 거부가 직접 호출자에게 새 예외 타입으로 새지 않는다."""
    with pytest.raises(cli.CliError, match=r"^no such label: missing$"):
        cli.cmd_rename(env, "missing", "fresh")


def test_direct_cli_remove_keeps_cli_error_boundary(env: config.Settings) -> None:
    """삭제 직접 호출도 기존 CliError 계약을 유지한다."""
    with pytest.raises(cli.CliError, match=r"^no such label: missing$"):
        cli.cmd_remove(env, "missing", assume_yes=True)


BAD_LABELS = ("../evil", ".hidden", "a/b", "", "line\n", "x" * 65)


@pytest.mark.parametrize("surface", ["cli", "tui"])
@pytest.mark.parametrize("bad_label", BAD_LABELS)
def test_rename_rejects_a_bad_old_name_even_when_that_directory_exists(
    env: config.Settings,
    capsys: pytest.CaptureFixture[str],
    surface: str,
    bad_label: str,
) -> None:
    """디스크에 있더라도 문법 관문을 건너뛴 라벨은 움직이지 않는다."""
    if bad_label == ".hidden":
        _write_auth(env.accounts_dir / ".hidden/auth.json", "hidden@example.com")
    before = _snapshot(env.accounts_dir)

    said = _rename(surface, env, bad_label, "fresh", capsys)

    assert said.lower() == f"not a usable label: {bad_label}"
    assert _snapshot(env.accounts_dir) == before


@pytest.mark.parametrize("surface", ["cli", "tui"])
@pytest.mark.parametrize("bad_label", BAD_LABELS)
def test_rename_rejects_a_bad_new_name_before_touching_the_source(
    env: config.Settings,
    capsys: pytest.CaptureFixture[str],
    surface: str,
    bad_label: str,
) -> None:
    """새 이름도 경로가 되므로 옛 이름과 같은 문법을 통과해야 한다."""
    before = _snapshot(env.accounts_dir)

    said = _rename(surface, env, "shared", bad_label, capsys)

    assert said.lower() == f"not a usable label: {bad_label}"
    assert _snapshot(env.accounts_dir) == before


@pytest.mark.parametrize("surface", ["cli", "tui"])
def test_rename_rejects_a_missing_source(
    env: config.Settings,
    capsys: pytest.CaptureFixture[str],
    surface: str,
) -> None:
    """없는 소스는 OS 오류가 아니라 라벨 거부로 설명한다."""
    before = _snapshot(env.accounts_dir)

    said = _rename(surface, env, "missing", "fresh", capsys)

    assert said.lower() == "no such label: missing"
    assert _snapshot(env.accounts_dir) == before


@pytest.mark.parametrize("surface", ["cli", "tui"])
def test_rename_rejects_a_symlink_source(
    env: config.Settings,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    surface: str,
) -> None:
    """슬롯처럼 보이는 심링크로 루트 밖 디렉토리를 움직이지 않는다."""
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, env.accounts_dir / "linked")
    before = _snapshot(env.accounts_dir)

    said = _rename(surface, env, "linked", "fresh", capsys)

    assert said.lower() == "no such label: linked"
    assert _snapshot(env.accounts_dir) == before
    assert outside.is_dir()


@pytest.mark.parametrize("surface", ["cli", "tui"])
def test_rename_rejects_a_regular_file_source(
    env: config.Settings,
    capsys: pytest.CaptureFixture[str],
    surface: str,
) -> None:
    """존재만 하는 파일은 OS 이동까지 보내지 않고 슬롯 아님으로 거부한다."""
    source = env.accounts_dir / "file-slot"
    source.write_bytes(b"keep this file")
    before = _snapshot(env.accounts_dir)

    said = _rename(surface, env, "file-slot", "fresh", capsys)

    assert said.lower() == "no such label: file-slot"
    assert _snapshot(env.accounts_dir) == before


@pytest.mark.parametrize("surface", ["cli", "tui"])
def test_rename_rejects_an_empty_existing_destination(
    env: config.Settings,
    capsys: pytest.CaptureFixture[str],
    surface: str,
) -> None:
    """빈 디렉토리도 계정 이름의 소유자라 덮어쓰지 않는다."""
    (env.accounts_dir / "fresh").mkdir()
    before = _snapshot(env.accounts_dir)

    said = _rename(surface, env, "shared", "fresh", capsys)

    assert said.lower() == "label already exists: fresh (use remove to drop it first)"
    assert _snapshot(env.accounts_dir) == before


@pytest.mark.parametrize("surface", ["cli", "tui"])
def test_rename_rejects_an_existing_destination_symlink(
    env: config.Settings,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    surface: str,
) -> None:
    """실재 디렉토리를 가리키는 목적지 심링크도 명시적 기존 라벨로 거부한다."""
    outside = tmp_path / "outside-destination"
    outside.mkdir()
    os.symlink(outside, env.accounts_dir / "fresh")
    before = _snapshot(env.accounts_dir)

    said = _rename(surface, env, "shared", "fresh", capsys)

    assert said.lower() == "label already exists: fresh (use remove to drop it first)"
    assert _snapshot(env.accounts_dir) == before
    assert outside.is_dir()


@pytest.mark.parametrize("surface", ["cli", "tui"])
def test_remove_rejects_a_bad_name_even_when_that_directory_exists(
    env: config.Settings,
    capsys: pytest.CaptureFixture[str],
    surface: str,
) -> None:
    """삭제는 디스크에서 발견한 숨김 디렉토리도 라벨로 인정하지 않는다."""
    bad = env.accounts_dir / ".hidden"
    _write_auth(bad / "auth.json", "hidden@example.com")
    before = _snapshot(env.accounts_dir)

    said = _remove(surface, env, ".hidden", capsys)

    assert said.lower() == "not a usable label: .hidden"
    assert _snapshot(env.accounts_dir) == before


@pytest.mark.parametrize("surface", ["cli", "tui"])
def test_remove_rejects_a_missing_source(
    env: config.Settings,
    capsys: pytest.CaptureFixture[str],
    surface: str,
) -> None:
    """없는 슬롯은 rmtree 의 오류 문구까지 떨어지지 않는다."""
    before = _snapshot(env.accounts_dir)

    said = _remove(surface, env, "missing", capsys)

    assert said.lower() == "no such label: missing"
    assert _snapshot(env.accounts_dir) == before


@pytest.mark.parametrize("surface", ["cli", "tui"])
def test_remove_rejects_a_symlink_source(
    env: config.Settings,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    surface: str,
) -> None:
    """삭제 대상 심링크는 바깥 디렉토리와 함께 그대로 둔다."""
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, env.accounts_dir / "linked")
    before = _snapshot(env.accounts_dir)

    said = _remove(surface, env, "linked", capsys)

    assert said.lower() == "no such label: linked"
    assert _snapshot(env.accounts_dir) == before
    assert outside.is_dir()


@pytest.mark.parametrize("surface", ["cli", "tui"])
def test_remove_rejects_a_regular_file_source(
    env: config.Settings,
    capsys: pytest.CaptureFixture[str],
    surface: str,
) -> None:
    """파일을 rmtree 에 넘겨 생기는 OS 오류를 슬롯 거부로 오인하지 않는다."""
    source = env.accounts_dir / "file-slot"
    source.write_bytes(b"keep this file")
    before = _snapshot(env.accounts_dir)

    said = _remove(surface, env, "file-slot", capsys)

    assert said.lower() == "no such label: file-slot"
    assert _snapshot(env.accounts_dir) == before


def test_rename_moves_the_row_and_carries_its_usage(env: config.Settings) -> None:
    """이름만 바뀐 같은 계정의 직전 사용량은 새 라벨 아래 이어 보인다."""
    cache.write(env, "shared", {"usedPercent": 41, "resetsAt": None}, now=1)
    before = tui.build_view(env)

    after = tui.do_rename(before, "shared", "personal")

    assert [row.label for row in after.rows] == ["master", "personal"]
    renamed = next(row for row in after.rows if row.label == "personal")
    assert renamed.email == "b@example.com"
    assert renamed.used == "~41%"
    assert not store.slot_dir(env, "shared").exists()
    assert cache.read_stale(env, "shared") is None


def test_rename_refuses_while_the_switch_lock_is_held(
    env: config.Settings,
) -> None:
    """다른 전환과 겹치면 슬롯을 움직이지 않고 다시 시도할 이유를 말한다."""
    paths.lock_path(env).mkdir()
    before = _snapshot(env.accounts_dir)

    after = tui.do_rename(tui.build_view(env), "shared", "personal")

    assert "Another switch is in progress" in after.message
    assert _snapshot(env.accounts_dir) == before


def test_rename_holds_the_switch_lock_during_the_move(
    env: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """검사만 잠그고 실제 이동 전에 락을 놓는 회귀를 잡는다."""
    moved: list[tuple[Path, Path]] = []

    def observe(source: Path, destination: Path) -> None:
        assert paths.lock_path(env).is_dir(), "디렉토리를 움직일 때 락이 풀려 있었다"
        moved.append((source, destination))
        os.replace(source, destination)

    monkeypatch.setattr("codex_swap.core.account_slots.os.rename", observe)

    after = tui.do_rename(tui.build_view(env), "shared", "personal")

    assert after.message == "Renamed shared -> personal"
    assert len(moved) == 1


@pytest.mark.parametrize("answer", ["n", "", None])
def test_remove_cancellation_leaves_everything_untouched(
    env: config.Settings,
    answer: str | None,
) -> None:
    """n·빈 입력·EOF 는 전부 기본값 No 로 접힌다."""
    before = _snapshot(env.accounts_dir)

    after = tui.do_remove(tui.build_view(env), "shared", answer)

    assert after.message == "Left it alone"
    assert _snapshot(env.accounts_dir) == before


def test_remove_warning_names_identity_loss_and_active_consequence(
    env: config.Settings,
) -> None:
    """짧은 입력줄 전에 무엇을 잃고 자동 전환이 왜 멈추는지 전부 보여 준다."""
    view = tui.build_view(env)
    before = _snapshot(env.accounts_dir)

    warning = tui.remove_warning(view, "master")

    assert warning is not None
    assert "'master'" in warning
    assert "a@example.com" in warning
    assert "cannot be undone" in warning
    assert "Automatic switching stops" in warning
    assert "adopt" in warning
    assert _snapshot(env.accounts_dir) == before


@pytest.mark.parametrize("width", [80, 140])
def test_remove_warning_is_fully_rendered_before_the_short_prompt(
    env: config.Settings,
    width: int,
) -> None:
    """되돌릴 수 없음과 활성 결과가 한 줄 말줄임표 뒤로 숨지 않는다."""
    warning = tui.remove_warning(tui.build_view(env), "master")
    assert warning is not None
    warned = tui.replace(tui.build_view(env), confirmation=warning)
    rendered = " ".join(line.strip() for line in tui.render_lines(warned, width=width))

    assert "cannot be undone" in rendered
    assert "Automatic switching stops" in rendered
    assert "adopt" in rendered
    assert "…" not in " ".join(
        line for line in tui.render_lines(warned, width=width) if "cannot" in line
    )


def test_remove_warning_wraps_wide_identity_by_terminal_cells(
    env: config.Settings,
) -> None:
    """넓은 글자 이메일도 터미널 칸을 넘기거나 경고에서 사라지지 않는다."""
    _write_auth(store.slot_auth(env, "master"), "사용자@example.com")
    _write_auth(store.active_auth(env), "사용자@example.com")
    warning = tui.remove_warning(tui.build_view(env), "master")
    assert warning is not None

    lines = tui._wrapped_note(warning, 40)

    assert all(tui._width(line) <= 40 for line in lines)
    assert "사용자@example.com" in " ".join(line.strip() for line in lines)


def test_remove_yes_deletes_the_row_and_cache(env: config.Settings) -> None:
    """승인한 슬롯만 지우고 목록과 정책 입력 캐시를 즉시 다시 만든다."""
    cache.write(env, "shared", {"usedPercent": 41, "resetsAt": None})
    view = tui.build_view(env)

    after = tui.do_remove(view, "shared", "y")

    assert [row.label for row in after.rows] == ["master"]
    assert not store.slot_dir(env, "shared").exists()
    assert cache.read_stale(env, "shared") is None
    assert after.message == "Removed shared"


def test_remove_active_keeps_live_credentials_and_explains_what_stops(
    env: config.Settings,
) -> None:
    """슬롯 사본 삭제가 현재 로그인까지 지우지 않으며 다음 조치를 말한다."""
    live_before = store.active_auth(env).read_bytes()
    after = tui.do_remove(tui.build_view(env), "master", "y")

    assert store.active_auth(env).read_bytes() == live_before
    assert identity.email_of(store.active_auth(env)) == "a@example.com"
    assert [row.label for row in after.rows] == ["shared"]
    assert not after.active_registered
    assert "Automatic switching stops" in after.message
    assert "adopt" in after.message


def test_row_actions_refuse_menu_and_empty_selections(env: config.Settings) -> None:
    """행을 잃은 메뉴 커서와 빈 목록은 이름 입력이나 삭제로 이어지지 않는다."""
    menu = tui.replace(tui.build_view(env), cursor=2)
    assert tui.do_rename(menu, None, "new").message == "Move to an account first, then press n"
    assert tui.do_remove(menu, None, "y").message == "Move to an account first, then press d"

    for label in ("master", "shared"):
        store.slot_dir(env, label).rename(env.accounts_dir / f".gone-{label}")
    empty = tui.build_view(env)
    assert tui.do_rename(empty, None, "new").message == "No accounts yet"
    assert tui.do_remove(empty, None, "y").message == "No accounts yet"
