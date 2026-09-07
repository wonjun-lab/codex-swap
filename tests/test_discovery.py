"""탐색 순서가 바뀌면 사용자가 고른 실행 파일도 바뀐다 — 후보를 함께 놓고 겨룬다."""

from pathlib import Path
from types import MappingProxyType

import pytest

from codex_swap.core import discovery


def binary(path: Path, content: bytes = b"#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o700)
    return path


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return {"HOME": str(tmp_path), "PATH": str(tmp_path / "path")}


@pytest.mark.parametrize("winner", range(6))
def test_tier_precedence(tmp_path: Path, env: dict[str, str], winner: int) -> None:
    env["CODEX_HOME"] = str(tmp_path / "custom")
    candidates = [
        tmp_path / "override",
        tmp_path / "path/codex",
        tmp_path / "custom" / discovery.STANDALONE_TAIL,
        tmp_path / ".codex" / discovery.STANDALONE_TAIL,
        tmp_path / ".nvm/versions/node/v20/bin/codex",
        tmp_path / ".nvm/versions/node/v9/bin/codex",
    ]
    for candidate in candidates[winner:]:
        binary(candidate)
    if winner == 0:
        env["CODEX_REAL_BIN"] = str(candidates[0])
    # 승자만 놓으면 순서를 뒤집어도 통과한다. 뒤 티어도 전부 실행 가능해야 한다.
    assert discovery.find_upstream(MappingProxyType(env)) == candidates[winner]


@pytest.mark.parametrize(
    "tier",
    [
        "override",
        "path",
        "standalone",
    ],
)
def test_success_does_not_enumerate_later_tiers(
    tmp_path: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch, tier: str
) -> None:
    candidate = binary(
        tmp_path / ".codex" / discovery.STANDALONE_TAIL
        if tier == "standalone"
        else tmp_path / "path/codex"
    )
    if tier == "override":
        env["CODEX_REAL_BIN"] = str(candidate)

    def forbidden(*args: object, **kwargs: object) -> list[Path]:
        raise AssertionError("앞 티어가 성공했는데 뒤 티어를 열거했다")

    monkeypatch.setattr(discovery, "_nvm_candidates", forbidden)
    if tier != "standalone":
        monkeypatch.setattr(discovery, "_standalone_candidates", forbidden)
    if tier == "override":
        monkeypatch.setattr(discovery.shutil, "which", forbidden)
    assert discovery.find_upstream(env) == candidate


@pytest.mark.parametrize("kind", ["missing", "directory", "not-executable", "wrapper"])
def test_invalid_override_never_falls_back(tmp_path: Path, env: dict[str, str], kind: str) -> None:
    binary(tmp_path / "path/codex")
    override = tmp_path / "explicit"
    if kind == "directory":
        override.mkdir()
    elif kind != "missing":
        binary(override, b"#!/bin/sh\n. lib/codex-path.sh\n" if kind == "wrapper" else b"real")
        if kind == "not-executable":
            override.chmod(0o600)
    env["CODEX_REAL_BIN"] = str(override)
    with pytest.raises(discovery.UpstreamNotFound, match="CODEX_REAL_BIN"):
        discovery.find_upstream(env)


def test_empty_override_is_unset(tmp_path: Path, env: dict[str, str]) -> None:
    expected = binary(tmp_path / "path/codex")
    env["CODEX_REAL_BIN"] = ""
    assert discovery.find_upstream(env) == expected


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b"#!/bin/sh\nlib/codex-path.sh", True),
        (b"#!/bin/sh\nexit 0", False),
        (b"binary lib/codex-path.sh", False),
        (b"#!" + b"x" * (8192 - 2 - len(b"lib/codex-path.sh")) + b"lib/codex-path.sh", True),
        (b"#!" + b"x" * (8192 - 2) + b"lib/codex-path.sh", False),
    ],
)
def test_wrapper_requires_both_signals_inside_window(
    tmp_path: Path, content: bytes, expected: bool
) -> None:
    assert discovery.is_wrapper(binary(tmp_path / "codex", content)) is expected


@pytest.mark.parametrize("kind", ["missing", "directory", "unreadable"])
def test_unreadable_wrapper_is_not_classified(tmp_path: Path, kind: str) -> None:
    candidate = tmp_path / "codex"
    if kind == "directory":
        candidate.mkdir()
    elif kind == "unreadable":
        binary(candidate, b"#!/bin/sh\nlib/codex-path.sh")
        candidate.chmod(0o000)
    try:
        assert discovery.is_wrapper(candidate) is False
    finally:
        if kind == "unreadable":
            candidate.chmod(0o600)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("a:", "a"), (":a", "a"), ("::a", "a"), ("a::b", "a::b"), ("", ""), (":", "")],
)
def test_path_empty_fields_follow_bash(env: dict[str, str], raw: str, expected: str) -> None:
    assert discovery.path_without_local_bin({**env, "PATH": raw}) == expected


def test_local_bin_aliases_are_all_removed(tmp_path: Path, env: dict[str, str]) -> None:
    local = tmp_path / ".local/bin"
    local.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(local, target_is_directory=True)
    env["PATH"] = f"{local}:{local}///:{alias}:{tmp_path}/keep"
    assert discovery.path_without_local_bin(env) == str(tmp_path / "keep")


def test_local_bin_is_excluded_from_discovery(tmp_path: Path, env: dict[str, str]) -> None:
    binary(tmp_path / ".local/bin/codex")
    expected = binary(tmp_path / ".codex" / discovery.STANDALONE_TAIL)
    env["PATH"] = str(tmp_path / ".local/bin")
    assert discovery.find_upstream(env) == expected


def test_path_wrapper_falls_back(tmp_path: Path, env: dict[str, str]) -> None:
    binary(tmp_path / "path/codex", b"#!/bin/sh\nlib/codex-path.sh")
    expected = binary(tmp_path / ".codex" / discovery.STANDALONE_TAIL)
    assert discovery.find_upstream(env) == expected


def test_nvm_numeric_order_and_hidden_exclusion(tmp_path: Path, env: dict[str, str]) -> None:
    root = tmp_path / ".nvm/versions/node"
    candidates = [binary(root / version / "bin/codex") for version in ("v9", "v20", ".v99")]
    assert discovery._nvm_candidates(env) == [candidates[1], candidates[0]]
    candidates[1].chmod(0o600)
    assert discovery.find_upstream(env) == candidates[0]
    candidates[0].unlink()
    with pytest.raises(discovery.UpstreamNotFound, match="could not find the codex binary"):
        discovery.find_upstream(env)


def test_version_key_mixed_parts_are_comparable() -> None:
    assert sorted(
        ["v9", "v20", "v20rc2", "v20rc10", "alpha", "2"], key=discovery._version_key, reverse=True
    ) == ["v20rc10", "v20rc2", "v20", "v9", "alpha", "2"]


def test_missing_nvm_root_is_empty(env: dict[str, str]) -> None:
    assert discovery._nvm_candidates(env) == []


def test_middle_empty_path_yields_absolute_binary(
    tmp_path: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = binary(tmp_path / "cwd/codex")
    monkeypatch.chdir(expected.parent)
    env["PATH"] = f"{tmp_path}/absent::{tmp_path}/also-absent"
    assert discovery.find_upstream(env) == expected
    assert discovery.path_with_bin_dir(discovery.find_upstream(env), env).startswith(
        f"{expected.parent}:"
    )


@pytest.mark.parametrize("given", ["missing", "directory", "wrapper"])
def test_account_bin_is_trusted_even_if_unusable(
    tmp_path: Path, env: dict[str, str], given: str
) -> None:
    candidate = tmp_path / given
    if given == "directory":
        candidate.mkdir()
    elif given == "wrapper":
        binary(candidate, b"#!/bin/sh\nlib/codex-path.sh")
    env.update(CODEX_ACCOUNT_BIN=str(candidate), CODEX_REAL_BIN=str(tmp_path / "invalid"))
    assert discovery.resolve_codex_bin(env) == candidate


@pytest.mark.parametrize("given", [None, ""])
def test_missing_account_bin_discovers(
    tmp_path: Path, env: dict[str, str], given: str | None
) -> None:
    if given is not None:
        env["CODEX_ACCOUNT_BIN"] = given
    expected = binary(tmp_path / "path/codex")
    assert discovery.resolve_codex_bin(env) == expected


def test_child_path_uses_symlink_parent_without_mutating_env(tmp_path: Path) -> None:
    target = binary(tmp_path / "package/codex")
    link = tmp_path / "bin/codex"
    link.parent.mkdir()
    link.symlink_to(target)
    env = {"PATH": "/original", "SENTINEL": "preserve"}
    expected = {"PATH": f"{link.parent}:/original", "SENTINEL": "preserve"}
    assert discovery.path_with_bin_dir(link, env) == expected["PATH"]
    assert discovery.env_with_bin_dir(link, MappingProxyType(env)) == expected
    assert env == {"PATH": "/original", "SENTINEL": "preserve"}


@pytest.mark.parametrize("env", [{}, {"PATH": ""}, {"PATH": "/original"}])
def test_bare_name_does_not_inject_cwd(env: dict[str, str]) -> None:
    assert discovery.path_with_bin_dir("codex", env) == env.get("PATH", "")
    assert discovery.env_with_bin_dir("codex", env) == env


def test_parent_supplies_missing_path(tmp_path: Path) -> None:
    assert discovery.env_with_bin_dir(tmp_path / "codex", {}) == {"PATH": str(tmp_path)}


# ── `~/.local/bin` 은 배제 대상이 아니라 마지막 후보다 ──────────────────────
#
# 그 디렉토리를 PATH 에서 통째로 빼는 것은 wrapper 재귀를 막으려는 장치였다. 그런데
# 재귀를 실제로 막는 것은 `is_wrapper()`(shebang 과 marker 의 곱)이고, 디렉토리 배제는 그
# 정밀한 검사가 **도달하기 전에** 후보를 없앤다. 그 결과 codex 를 거기에 평범하게
# 설치한 사용자(npm 전역 prefix · pipx · 수동 설치)는 도구를 아예 쓸 수 없었다.


def test_a_plain_codex_in_local_bin_is_found(tmp_path: Path, env: dict[str, str]) -> None:
    """PATH 조회가 그 디렉토리를 빼므로, 마지막 티어에서 직접 본다."""
    candidate = binary(tmp_path / ".local/bin/codex")
    env["PATH"] = str(tmp_path / ".local/bin")
    assert discovery.find_upstream(MappingProxyType(env)) == candidate


def test_a_wrapper_in_local_bin_is_still_refused(tmp_path: Path, env: dict[str, str]) -> None:
    """재귀 방어는 그대로다. 배제 이유였던 그 경우만 정확히 거른다."""
    binary(tmp_path / ".local/bin/codex", b"#!/bin/sh\n. lib/codex-path.sh\n")
    env["PATH"] = str(tmp_path / ".local/bin")
    with pytest.raises(discovery.UpstreamNotFound):
        discovery.find_upstream(MappingProxyType(env))


def test_local_bin_never_outranks_a_real_tier(tmp_path: Path, env: dict[str, str]) -> None:
    """마지막 티어여야 한다. 앞으로 오면 wrapper 가 놓인 기기에서 우선순위가 뒤집힌다."""
    binary(tmp_path / ".local/bin/codex")
    nvm = binary(tmp_path / ".nvm/versions/node/v20/bin/codex")
    env["PATH"] = str(tmp_path / ".local/bin")
    assert discovery.find_upstream(MappingProxyType(env)) == nvm
