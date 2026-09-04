"""CLI 표면이 bash 판과 같은 명령·별칭을 노출하는지 본다.

이관의 성패는 "같은 명령이 같은 이름으로 있는가" 에서 먼저 갈린다. 별칭(ls·switch·rm)까지
확인하는 이유는, 그것들이 사용자의 손과 스크립트에 이미 박혀 있기 때문이다.
"""

from __future__ import annotations

import pytest

from codex_swap.cli import build_parser, main


@pytest.mark.parametrize(
    "command",
    ["adopt", "add", "list", "status", "use", "rotate", "remove", "clean"],
)
def test_command_is_exposed(command: str) -> None:
    parser = build_parser()
    args = parser.parse_args(
        [command, "x"] if command in {"adopt", "add", "use", "remove"} else [command]
    )
    assert args.command == command


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [("ls", "list"), ("switch", "use"), ("rm", "remove")],
)
def test_alias_is_accepted(alias: str, canonical: str) -> None:
    parser = build_parser()
    argv = [alias, "x"] if canonical in {"use", "remove"} else [alias]
    assert parser.parse_args(argv).command == alias


def test_bare_invocation_prints_help_and_succeeds(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "codex-swap" in capsys.readouterr().out
