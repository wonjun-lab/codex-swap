"""배선 판정 — "홈을 넘기는가" 는 실행되는 줄로만 본다.

공식 앱과 한 기기에서 같이 살면서 실제로 본 것들이 이 파일의 출발점이다.

- 앱의 codex 가 `CODEX_HOME=~/.codex` 로 떠서 `auth.json` 을 제자리에서 갱신하고, 자기 세션으로
  되돌려 놓았다. 원장에는 `A -> B` 만 쌓이고 `B -> A` 는 없었다.
- dotfiles wrapper 는 전환을 하긴 했지만 codex 를 앱의 홈으로 띄웠고, 다른 기기의 wrapper 는
  아예 옛 bash 전환기를 부르고 있었다.

여기서 재는 것은 wrapper·셸 프로필을 읽고 그 각각을 **제대로 가려내는가**다. 실제 codex 도,
실제 토큰도 쓰지 않는다. 판정에 넣는 입력은 **제품 상수를 되받아 쓰지 않고 글자 그대로 적는다**
— 상수를 입력으로 쓰면 그 상수가 틀려도 테스트가 따라 틀려서 아무것도 못 잡는다.

임시 HOME `box` 는 `_coexist.py` 에 있다.
"""

from __future__ import annotations

import pytest

from _coexist import _on_path
from codex_swap.core import wiring

# 사용자에게 안내하는 한 줄. 상수가 아니라 글자로 적어 둔다.
HOME_LINE = 'export CODEX_HOME="$(codex-swap home)"'

# dotfiles 의 codex/codex.sh 가 실제로 쓰는 모양 (변수를 거쳐 넘긴다).
DOTFILES_WRAPPER = """#!/usr/bin/env bash
set -euo pipefail
rotate_cmd="$(command -v codex-swap 2> /dev/null || true)"
if [[ -x "$rotate_cmd" ]]; then
  swap_home="$("$rotate_cmd" home 2> /dev/null || true)"
  if [[ -n "$swap_home" ]]; then
    export CODEX_HOME="$swap_home"
  fi
fi
case "${1:-}" in
  login | logout | mcp-server) ;;
  *) CODEX_ACCOUNT_BIN="$real_codex" "$rotate_cmd" rotate > /dev/null || true ;;
esac
exec "$real_codex" "$@"
"""


# ── 배선 판정: "홈을 넘기는가" 는 실행되는 줄로만 본다 ──────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        HOME_LINE + "\ncodex-swap rotate >/dev/null || true\n",
        DOTFILES_WRAPPER,
        'exec codex-swap exec "$@"\n',
        'CODEX_HOME="$(codex-swap home)"\nexport CODEX_HOME\ncodex-swap rotate || true\n',
        'export CODEX_HOME="$("codex-swap" home)"\ncodex-swap rotate || true\n',
        'codex() { codex-swap exec "$@"; }\n',
        "# don't rotate before the home is set\n" + HOME_LINE + "\ncodex-swap rotate || true\n",
        HOME_LINE + "; codex-swap rotate || true\n",
        'command -v codex-swap >/dev/null && export CODEX_HOME="$(codex-swap home)"\n'
        "codex-swap rotate || true\n",
        'command -- codex-swap exec "$@"\n',
        "main() {\n  "
        + HOME_LINE
        + '\n  codex-swap rotate || true\n  exec real "$@"\n}\nmain "$@"\n',
        "alias codex='codex-swap exec'\n",
        "$'codex-swap' exec \"$@\"\n",
    ],
    ids=[
        "canonical-line",
        "dotfiles-via-variable",
        "delegates-to-exec",
        "export-on-its-own-line",
        "quoted-program-name",
        "shell-function",
        "apostrophe-in-a-comment",
        "one-line",
        "guarded-one-liner",
        "command-double-dash",
        "helper-function-that-is-called",
        "alias-named-codex",
        "ansi-c-quoted-program-name",
    ],
)
def test_real_ways_of_passing_the_home_are_recognised(body: str) -> None:
    """놓치면 멀쩡한 기기에 "배선이 반쪽" 이라고 말한다. 주석 속 `don't` 도 흔하다."""
    assert wiring.passes_home("#!/bin/sh\n" + body)


@pytest.mark.parametrize(
    "body",
    [
        "# CODEX_HOME is set elsewhere\ncodex-swap rotate || true\n",
        "unset CODEX_HOME\ncodex-swap rotate || true\n",
        'export CODEX_HOME="$HOME/.codex"\ncodex-swap rotate || true\n',
        "codex-swap rotate || true\n" + HOME_LINE + "\n",
        HOME_LINE + "\ncodex-swap rotate || true\nunset CODEX_HOME\n",
        "# " + HOME_LINE + "\ncodex-swap rotate || true\n",
        'CODEX_HOME="$(codex-swap home)"\ncodex-swap rotate || true\n',
        'export CODEX_HOME\nCODEX_HOME="$(codex-swap home)" codex-swap rotate\nexec real "$@"\n',
        "cat <<'EOF'\n" + HOME_LINE + "\nEOF\ncodex-swap rotate || true\n",
        HOME_LINE + "\nCODEX_HOME=/somewhere/else\ncodex-swap rotate || true\n",
        'h="$(codex-swap home)"\nh=/somewhere/else\nexport CODEX_HOME="$h"\ncodex-swap rotate\n',
        HOME_LINE + "; unset CODEX_HOME\ncodex-swap rotate || true\n",
        'echo "codex-swap exec"\ncodex-swap rotate || true\nexec real "$@"\n',
        'export CODEX_HOME="$(codex-swap home)/sub"\ncodex-swap rotate || true\n',
        ': "${CODEX_HOME:=$(codex-swap home)}"\nexport CODEX_HOME\ncodex-swap rotate\n',
        'codex-swap rotate || true\nexec "$real_codex" exec "$@"\n',
        HOME_LINE + '\nunset CODEX_HOME\nCODEX_HOME="$(codex-swap home)"\ncodex-swap rotate\n',
        HOME_LINE + "\nexport -n CODEX_HOME\ncodex-swap rotate || true\n",
        "export CODEX_HOME='$(codex-swap home)'\ncodex-swap rotate || true\n",
        'export CODEX_HOME="$(codex-swap home | sed s,a,b,)"\ncodex-swap rotate\n',
        'helper=printf\nexport CODEX_HOME="$("$helper" home)"\ncodex-swap rotate\n',
        HOME_LINE + ' | cat\ncodex-swap rotate || true\nexec real "$@"\n',
        HOME_LINE + '\ncodex-swap rotate || true\nCODEX_HOME=/other exec real "$@"\n',
        HOME_LINE + '\ncodex-swap rotate || true\nexec env -i real "$@"\n',
        HOME_LINE + '\ncodex-swap rotate || true\nexec env -u CODEX_HOME real "$@"\n',
        'export CODEX_HOME="$(codex-swap home || true; echo /x)"\ncodex-swap rotate\n',
        'swap=codex-swap\nswap=printf\nexport CODEX_HOME="$("$swap" home)"\ncodex-swap rotate\n',
        "alias other='codex-swap exec'\ncodex-swap rotate || true\n",
        'h="$(codex-swap home)" | cat\nexport CODEX_HOME="$h"\ncodex-swap rotate\n',
    ],
    ids=[
        "comment",
        "unset",
        "old-home",
        "after-rotate",
        "unset-later",
        "commented-out",
        "never-exported",
        "only-for-one-command",
        "inside-a-heredoc",
        "overwritten",
        "variable-overwritten",
        "unset-on-the-same-line",
        "exec-only-printed",
        "path-appended",
        "default-keeps-an-inherited-home",
        "codex-own-exec-subcommand",
        "set-again-after-unset-without-export",
        "export-attribute-removed",
        "single-quoted-literal",
        "output-rewritten-by-a-pipe",
        "helper-is-not-codex-swap",
        "export-inside-a-pipeline",
        "child-given-another-home",
        "child-given-no-environment",
        "child-loses-the-home",
        "extra-output-after-the-fallback",
        "helper-reassigned-away",
        "alias-with-another-name",
        "assignment-inside-a-pipeline",
    ],
)
def test_lookalikes_do_not_count_as_passing_the_home(body: str) -> None:
    """설명 주석·`unset`·옛 홈을 명시한 줄이 전부 "넘긴다" 로 읽혔었다.

    줄 단위 정규식으로 고친 뒤에도 export 없는 대입·heredoc 본문·재대입·같은 줄의 unset·찍기만
    한 `exec` 가 통과했다(교차 검토 재현). 하나하나가 codex 를 앱의 홈으로 띄우는 wrapper 다.
    """
    assert not wiring.passes_home("#!/bin/sh\n" + body)


def test_a_wrapper_that_only_rotates_is_incomplete_when_apart(box) -> None:
    _on_path(box, '#!/bin/bash\ncodex-swap rotate >/dev/null || true\nexec real "$@"\n')
    state = wiring.inspect("bash")
    assert state.kind == wiring.EXTERNAL
    assert not state.complete(isolate=True)
    assert state.complete(isolate=False), "앱과 나뉘지 않았으면 전환만으로 충분하다"


def test_the_dotfiles_wrapper_is_complete(box) -> None:
    _on_path(box, DOTFILES_WRAPPER)
    assert wiring.inspect("bash").complete(isolate=True)


@pytest.mark.parametrize(
    "body",
    [
        '#!/bin/bash\n"$script_dir/codex-account" rotate\nexec real "$@"\n',
        '#!/bin/bash\nrotate_cmd="$script_dir/codex-account"\n"$rotate_cmd" rotate\n',
        '#!/bin/bash\necho "moving to codex-swap"\n"$d/codex-account" rotate\n',
        "#!/bin/bash\necho '$(codex-swap rotate)'\n\"$d/codex-account\" rotate\n",
        '#!/bin/bash\ncmd=codex-swap\ncmd=printf\n"$cmd" exec\n"$d/codex-account" rotate\n',
        "#!/bin/bash\nalias other='codex-swap exec'\n\"$d/codex-account\" rotate\n",
        '#!/bin/bash\n( "$d/codex-account" rotate )\nexec real "$@"\n',
        '#!/bin/bash\nexport CODEX_HOME="$(codex-swap home)"\n"$d/codex-account" rotate\n',
    ],
    ids=[
        "direct",
        "through-a-variable",
        "codex-swap-only-printed",
        "inside-single-quotes",
        "variable-reassigned-away",
        "alias-with-another-name",
        "inside-a-subshell",
        "asks-codex-swap-only-for-the-home",
    ],
)
def test_the_old_bash_switcher_is_recognised(box, body: str) -> None:
    """글자로 찍힌 `codex-swap` 하나에 옛 전환기 판정이 사라졌었다(교차 검토 재현)."""
    _on_path(box, body)
    state = wiring.inspect("bash")
    assert state.kind == wiring.LEGACY
    assert not state.complete(isolate=False)


def test_a_wrapper_that_only_asks_for_the_home_does_not_switch(box) -> None:
    """홈만 받아 쓰고 `rotate` 를 안 부르면 codex 를 칠 때 전환이 일어나지 않는다 — 배선이 아니다.

    그런데도 "codex-swap 을 부른다" 로 읽어 완성된 배선으로 쳤다(교차 검토).
    """
    _on_path(box, '#!/bin/bash\nexport CODEX_HOME="$(codex-swap home)"\nexec real "$@"\n')
    assert wiring.inspect("bash").kind == wiring.NONE


def test_a_long_wrapper_is_read_to_the_end(box) -> None:
    """앞 8KB 만 읽던 때, 뒤쪽의 `unset CODEX_HOME` 이 잘려 나가 "넘긴다" 로 읽혔다."""
    _on_path(
        box,
        "#!/bin/bash\n"
        + HOME_LINE
        + "\ncodex-swap rotate || true\n# "
        + "x" * 9000
        + '\nunset CODEX_HOME\nexec real "$@"\n',
    )
    state = wiring.inspect("bash")
    assert state.kind == wiring.EXTERNAL
    assert not state.complete(isolate=True)


@pytest.mark.parametrize(
    "fallback",
    [
        '[[ -n "$rotate_cmd" ]] || rotate_cmd="$script_dir/codex-account"\n',
        'if [[ -z "$rotate_cmd" ]]; then\n  rotate_cmd="$script_dir/codex-account"\nfi\n',
    ],
    ids=["or-list", "if-block"],
)
def test_a_wrapper_that_falls_back_to_the_old_switcher_counts_as_ours(box, fallback: str) -> None:
    """codex-swap 이 있으면 그것을, 없을 때만 옛 전환기를 부른다 — dotfiles 의 한때 모양.

    조건부로 다시 담은 것을 "덮어썼다" 로 읽으면 codex-swap 쪽 갈래가 사라져 옛 전환기로 분류된다.
    """
    _on_path(
        box,
        '#!/bin/bash\nrotate_cmd="$(command -v codex-swap || true)"\n'
        + fallback
        + '"$rotate_cmd" rotate\nexec real "$@"\n',
    )
    assert wiring.inspect("bash").kind == wiring.EXTERNAL


def test_only_looking_up_the_old_switcher_is_not_calling_it(box) -> None:
    """`command -v` 는 찾기만 한다 — 부르는 것으로 치면 안내만 하는 wrapper 가 옛 배선이 된다."""
    _on_path(
        box,
        '#!/bin/bash\nif command -v codex-account >/dev/null; then echo "old switcher left"; fi\n'
        'exec real "$@"\n',
    )
    assert wiring.inspect("bash").kind == wiring.NONE


def test_a_profile_that_only_uses_codex_swap_is_not_wiring(box) -> None:
    """셸 프로필에서 codex-swap 을 부른다고 전환이 걸린 것은 아니다 — `rotate` 나 `exec` 여야 한다.

    배선이 없는데 있다고 읽으면 `init` 이 wrapper 를 놓지 않고 넘어간다.
    """
    (box.home / ".bashrc").write_text('alias cs="codex-swap"\ncodex-swap list >/dev/null || true\n')
    assert wiring.inspect("bash").kind == wiring.NONE


def test_a_profile_function_that_delegates_to_exec_is_complete(box) -> None:
    (box.home / ".bashrc").write_text('codex() { codex-swap exec "$@"; }\n')
    state = wiring.inspect("bash")
    assert state.kind == wiring.PROFILE
    assert state.complete(isolate=True)


def test_a_migration_comment_does_not_hide_the_old_switcher(box) -> None:
    """옛 전환기를 부르는 줄은 그대로인데, 주석 하나에 판정이 사라졌다.

    `# codex-swap 으로 옮길 것` 같은 설명만으로 옛 전환기가 아닌 것으로 읽혔다.
    """
    _on_path(box, '#!/bin/bash\n# migrate to codex-swap home someday\n"$d/codex-account" rotate\n')
    assert wiring.inspect("bash").kind == wiring.LEGACY
