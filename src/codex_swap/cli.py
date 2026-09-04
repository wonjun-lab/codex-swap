"""CLI adapter.

이 층은 서식과 exit code 만 다룬다. 판단은 전부 core 가 하고, 여기서는 그 결과를
사람이 읽을 문자열로 옮긴다. bash 판에서 정책과 출력이 섞여 있던 것을 가르는 것이
이 이관의 목적 중 하나다.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from codex_swap import __version__

COMMANDS = ("adopt", "add", "list", "status", "use", "rotate", "remove", "clean")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-swap",
        description="Codex 계정을 여러 개 보관하고 사용량에 따라 갈아끼운다.",
    )
    parser.add_argument("--version", action="version", version=f"codex-swap {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p = sub.add_parser("adopt", help="지금 로그인된 계정을 슬롯에 등록")
    p.add_argument("label")

    p = sub.add_parser("add", help="새 슬롯에 로그인")
    p.add_argument("label")

    sub.add_parser("list", aliases=["ls"], help="등록된 계정과 캐시된 사용량")

    p = sub.add_parser("status", help="활성 계정과 사용량")
    p.add_argument("--fresh", action="store_true", help="캐시를 무시하고 다시 조회")

    p = sub.add_parser("use", aliases=["switch"], help="수동 전환")
    p.add_argument("label")

    p = sub.add_parser("rotate", help="정책 실행")
    p.add_argument("--dry-run", action="store_true", help="판단만 하고 바꾸지 않는다")

    p = sub.add_parser("remove", aliases=["rm"], help="슬롯 삭제")
    p.add_argument("label")

    sub.add_parser("clean", help="슬롯의 프로브 부산물 정리")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    raise NotImplementedError(f"{args.command} is not wired up yet")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
