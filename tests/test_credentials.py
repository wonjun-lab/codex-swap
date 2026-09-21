"""Codex 자식과 자격증명 파일이 같은 저장 백엔드를 보는가."""

import json
from pathlib import Path

from codex_swap import cli
from codex_swap.core import config, credentials


def _refresh_auth(home: Path, token: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(json.dumps({"tokens": {"refresh_token": token}}))


def test_configured_store_reads_only_the_top_level_backend(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    (home / "config.toml").write_text(
        'cli_auth_credentials_store = "keyring"\n'
        "[profile.work]\n"
        'cli_auth_credentials_store = "file"\n'
    )

    assert credentials.configured_store(home) == "keyring"


def test_configured_store_is_unknown_when_config_is_missing_or_invalid(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    assert credentials.configured_store(home) is None

    (home / "config.toml").write_text("cli_auth_credentials_store = [not valid")
    assert credentials.configured_store(home) is None

    (home / "config.toml").write_bytes(b"\xff\xfe")
    assert credentials.configured_store(home) is None


def test_managed_argv_forces_file_store_and_preserves_the_command() -> None:
    assert credentials.managed_argv("/opt/codex", ("login", "--device-auth")) == (
        "/opt/codex",
        "-c",
        'cli_auth_credentials_store="file"',
        "login",
        "--device-auth",
    )


def test_explicit_file_store_is_not_duplicated() -> None:
    args = ("-c", 'cli_auth_credentials_store="file"', "exec", "hello")
    assert credentials.managed_argv("/opt/codex", args) == ("/opt/codex", *args)
    assert credentials.explicit_store(args) == "file"


def test_explicit_other_store_is_left_to_the_caller() -> None:
    args = ("--config=cli_auth_credentials_store=keyring", "exec", "hello")
    assert credentials.explicit_store(args) == "keyring"
    assert credentials.managed_argv("/opt/codex", args) == ("/opt/codex", *args)


def test_config_looking_prompt_after_double_dash_is_not_an_override() -> None:
    args = ("exec", "--", "-c", 'cli_auth_credentials_store="keyring"')
    assert credentials.explicit_store(args) is None
    assert credentials.managed_argv("/opt/codex", args) == (
        "/opt/codex",
        "-c",
        'cli_auth_credentials_store="file"',
        *args,
    )


def test_managed_exec_forces_the_file_store(box) -> None:
    plan = cli.exec_plan(config.load(), ("exec", "hello"), "/opt/codex", {"PATH": "/bin"})
    assert plan.argv == (
        "/opt/codex",
        "-c",
        'cli_auth_credentials_store="file"',
        "exec",
        "hello",
    )
    assert plan.rotate is True


def test_explicit_non_file_store_bypasses_rotation(box) -> None:
    args = ("-c", 'cli_auth_credentials_store="keyring"', "exec", "hello")
    plan = cli.exec_plan(config.load(), args, "/opt/codex", {"PATH": "/bin"})
    assert plan.argv == ("/opt/codex", *args)
    assert plan.rotate is False


def test_custom_home_keeps_the_callers_backend_untouched(box) -> None:
    plan = cli.exec_plan(
        config.load(),
        ("exec", "hello"),
        "/opt/codex",
        {"PATH": "/bin", "CODEX_HOME": "/work/custom-codex"},
    )
    assert plan.argv == ("/opt/codex", "exec", "hello")
    assert plan.rotate is False


def test_shared_app_login_predicate_distinguishes_refresh_tokens(box) -> None:
    box.app.mkdir(parents=True)
    app_home = box.home / ".codex"
    slot_home = box.home / ".codex/accounts/shared"
    _refresh_auth(app_home, "same-refresh")
    _refresh_auth(slot_home, "same-refresh")

    assert credentials.shares_app_login(slot_home)

    _refresh_auth(slot_home, "independent-refresh")
    assert not credentials.shares_app_login(slot_home)
