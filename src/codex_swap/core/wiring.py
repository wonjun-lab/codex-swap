"""codex 가 **어느 홈으로 뜨는지**를 우리가 책임진다 — 양보는 우리가 한다.

`~/.codex` 의 주인은 공식 ChatGPT 데스크톱 앱이다. 그 앱은 자기 codex 를
`CODEX_HOME=~/.codex` 로 띄워 두고 그 안의 `auth.json` 을 **제자리에서 갱신하고, 자기 세션으로
되돌려 놓는다.** 우리는 서드파티이므로 같은 파일을 놓고 다투지 않는다.

다투면 두 가지가 동시에 망가진다. 사용자에게는 "로그인이 자꾸 풀린다" 로 보이고, 앱의 codex
로그에는 `401 Encountered invalidated oauth token` 이 수만 줄 쌓인다 — 같은 refresh token 을 두
곳이 쥐고 있다가 한쪽이 갱신하는 순간 다른 쪽 것이 무효가 되기 때문이다. 한 기기에서 실제로
확인했다.

**그래서 활성 자격증명 자리만 옮긴다.** 슬롯 저장소(`~/.codex/accounts`)는 앱이 모르므로
그대로 둔다. 그리고 codex 가 그 자리로 뜨도록 **배선**을 책임진다 — 우리가 놓는 wrapper 든,
사용자가 dotfiles 로 심어 둔 wrapper 든, 결국 codex 에게 `CODEX_HOME` 을 넘겨야 분리가 성립한다.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

ISOLATED_HOME = "$HOME/.codex-cli"
"""앱에게 `~/.codex` 를 넘겨주고 우리가 쓸 자리. 안내 문구에서 셸이 풀게 문자열로 둔다."""

APP_PATHS = (
    "/Applications/ChatGPT.app",
    "~/Applications/ChatGPT.app",
)
"""공식 앱이 설치되는 자리. 사용자별 설치도 있다."""


def app_installed(paths: tuple[str, ...] = APP_PATHS) -> bool:
    """ChatGPT 데스크톱 앱이 깔려 있나.

    **돌고 있는지가 아니라 깔려 있는지를 본다.** 앱은 껐다 켜지는 것이라, 마침 꺼져 있는
    순간에 판단하면 다음에 켰을 때 같은 다툼이 시작된다.
    """
    return any(Path(p).expanduser().exists() for p in paths)


def shell_of(path: str | None = None) -> str:
    """쓰고 있는 셸. 알 수 없으면 `bash` — 문법이 가장 널리 통한다."""
    name = Path(path or os.environ.get("SHELL") or "bash").name
    return name if name in {"bash", "zsh", "fish"} else "bash"


DEFAULT_HOME = Path("~/.codex")
"""분리하기 전의 자리. 공식 앱도 여기를 쓴다."""


ISOLATED_PATH = Path("~/.codex-cli")
"""분리했을 때 우리가 쓰는 자리."""


def _same(a: str | os.PathLike[str], b: str | os.PathLike[str]) -> bool:
    return os.path.realpath(os.path.expanduser(a)) == os.path.realpath(os.path.expanduser(b))


def apart_from_app(default_home: Path) -> bool:
    """앱이 깔려 있고, **실제로** 우리 홈이 앱의 홈과 다른가.

    앱이 있다는 것만으로 분리됐다고 말하면 안 된다. `CODEX_ACCOUNT_DEFAULT_HOME` 으로 앱의 홈을
    직접 가리킨 설정에서 `init` 이 "나눠 뒀다" 고 말한 적이 있다 — 실제로는 여전히 같은 파일을
    다투는 중이었다.
    """
    return app_installed() and not _same(default_home, DEFAULT_HOME)


def inherits_app_home(default_home: Path, environ: dict[str, str] | None = None) -> bool:
    """분리된 기기인데 **물려받은 `CODEX_HOME` 이 앱의 홈**을 가리키나.

    옛 셸 설정의 `export CODEX_HOME=~/.codex` 나 앱이 띄운 환경에서 흘러온 값이다. 그대로 두면
    codex 가 앱의 계정으로 뜨고, 우리 전환 가드는 "호출자가 다른 홈을 골랐다" 며 조용히 멈춘다.
    """
    table = os.environ if environ is None else environ
    raw = table.get("CODEX_HOME")
    return bool(raw) and apart_from_app(default_home) and _same(raw, DEFAULT_HOME)


def seed_source(default_home: Path) -> Path | None:
    """지금 홈은 비었는데 **다른 자리에 로그인이 남아 있으면** 그 경로. 아니면 None.

    홈이 바뀌는 순간에만 생기는 상태다. 자격증명은 따라오지 않으므로, 사용자에게는 잘
    쓰던 도구가 갑자기 로그아웃된 것처럼 보인다.

    **두 방향 다 본다.** 앱을 깔면 `~/.codex` → `~/.codex-cli` 로 옮겨 가지만, 앱을
    **지우면** 그 반대로 돌아온다. 뒤쪽을 빠뜨리기 쉬운데 사용자가 겪는 증상은 똑같고,
    오히려 더 당황스럽다 — 앱을 지웠을 뿐인데 codex 가 로그아웃되기 때문이다.

    **알려 주기만 한다. 복사하라고 하지 않는다.** 앱 쪽 `auth.json` 을 우리 자리로 베끼면 같은
    refresh token 을 앱과 우리가 나눠 쥐게 되고, 먼저 갱신하는 쪽이 다른 쪽을 로그아웃시킨다 —
    이 모듈이 막으려는 바로 그 다툼이다. 채우는 길은 슬롯에서의 전환이나 새 로그인뿐이다.
    """
    here = default_home.expanduser()
    if (here / "auth.json").is_file():
        return None  # 지금 자리에 이미 있다
    for other in (DEFAULT_HOME.expanduser(), ISOLATED_PATH.expanduser()):
        if other != here and (other / "auth.json").is_file():
            return other
    return None


WRAPPER = Path("~/.local/bin/codex")
"""우리가 놓는 wrapper 자리. **PATH 에 있기만 하면 셸이 무엇이든 통한다.**"""

MARKER = "# codex-swap wrapper"
"""우리가 놓은 것인지 알아보는 표시.

`discovery.is_wrapper` 도 이 표시를 본다 — 못 보면 upstream 이 사라진 기기에서 이 wrapper 를
진짜 codex 로 집어 `exec` 가 자기 자신을 끝없이 다시 띄운다. 두 곳이 갈라지지 않는지는 테스트가
묶는다.
"""

WRAPPER_BODY = f"""#!/bin/sh
{MARKER} — do not edit; regenerate with: codex-swap init
exec codex-swap exec "$@"
"""
"""**얇게 둔다.** 판단은 전부 `codex-swap exec` 안에 있다."""

LEGACY_ROTATOR = "codex-account"
"""codex-swap 이전의 bash 전환기. 우리와 같은 원장·락·스탬프를 쓴다."""

HOME_LINE = 'export CODEX_HOME="$(codex-swap home)"'
"""외부 wrapper 에 넣으라고 안내할 한 줄. **`rotate` 보다 앞에** 둔다."""


# ── 셸 스크립트를 "실행되는 명령" 으로 읽는다 ────────────────────────────────────
#
# 문자열이 어딘가 들어 있는지로 판정하던 때가 있었다. 그러면 설명 주석 하나(`# CODEX_HOME …`),
# `unset CODEX_HOME`, `CODEX_HOME="$HOME/.codex"` 가 전부 "홈을 넘긴다" 로 읽혔고, 옛 전환기를
# 부르는 wrapper 에 `# codex-swap 으로 옮길 것` 이라는 주석만 붙어도 옛 전환기 판정이 사라졌다.
#
# 주석을 걷고 줄마다 정규식을 대는 것으로도 모자랐다. `export` 없는 대입, heredoc 본문, 뒤에서
# 다시 넣은 값, 같은 줄의 `; unset CODEX_HOME`, `echo "codex-swap exec"` 가 모두 "넘긴다" 로
# 통과했다 — 교차 검토가 하나씩 재현했다.
#
# 셸을 해석하는 것은 아니다. 따옴표·명령 치환·주석·heredoc 을 가려 **명령 단위로 끊고, 위에서부터
# 차례로 따라간다.** 파이프·`&` 로 서브셸에 들어간 명령은 변수를 남기지 않는 것으로, `if`·`case`
# 안이나 `&&`·`||` 뒤의 명령은 조건부로 본다.
#
# 기우는 방향은 하나다. 해석 못 한 것을 "됐다" 로 접으면 전환이 codex 에 안 닿는 기기를 ready 라고
# 부르게 된다. 예외는 조건부 export 하나 — dotfiles wrapper 가 "codex-swap 이 답했을 때만" export
# 하는 모양이라, 그걸 빼면 가장 흔한 배선을 못 알아본다. 덮어쓰기·unset 은 조건부여도 친다.
#
# 따라가지 **않는** 것: 조건이 실제로 참인지, 정의만 하고 부르지 않은 함수, `eval`·`source` 로
# 들어오는 코드, 배열에 담아 부르는 명령. 셸을 실제로 돌려야 알 수 있는 것들이다.

_RESERVED = frozenset(
    {"if", "then", "else", "elif", "fi", "do", "done", "while", "until", "esac", "time", "!"}
    | {"{", "}"}
)
"""명령어 자리 앞에 오는 예약어. 걷어내야 그 뒤의 명령이 보인다."""

_OPENERS = frozenset({"if", "while", "until", "for", "select", "case"})
_CLOSERS = frozenset({"fi", "done", "esac"})
"""조건부 구간을 열고 닫는 말. 그 안의 명령은 일어날 수도, 안 일어날 수도 있다."""

_PREFIXES = frozenset({"exec", "builtin", "nohup", "env"})
"""뒤의 명령을 그대로 실행하는 앞말. `command` 는 `-v` 가 붙으면 찾기만 하므로 따로 본다."""

_DECLARERS = frozenset({"export", "declare", "typeset", "local", "readonly"})

_ASSIGN = re.compile(r"([A-Za-z_]\w*)=(.*)", re.S)
_VAR = re.compile(r"\$\{?(\w+)\}?")
_SUBST = re.compile(r"\$\(.*\)|`.*`", re.S)
_FUNC_HEAD = re.compile(r"[\w.:-]+\(\)")
_HEREDOC = re.compile(r"<<-?\s*\\?(['\"]?)([\w.-]+)\1")

_SCRIPT_LIMIT = 1 << 20
"""wrapper 를 끝까지 읽는 상한. 이보다 큰 wrapper 는 없다고 본다."""


def _split(text: str) -> list[tuple[list[str], str, str]]:
    """명령마다 (단어들, 앞 구분자, 뒤 구분자)를 나온 순서대로.

    따옴표와 `$( )` 안은 한 단어로 둔다. 주석과 heredoc 본문은 명령이 아니므로 뺀다. 구분자는
    `;` · `&&` · `||` · `|` · `|&` · `&` · `;;` · 줄바꿈이다 — 무엇으로 이어졌는지가 뜻을 바꾼다.
    """
    out: list[tuple[list[str], str, str]] = []
    words: list[str] = []
    word: list[str] = []
    closers: list[str] = []  # 열려 있는 것이 무엇으로 닫히는가: '"' · ')' · '`'
    heredocs: list[str] = []  # 이 줄이 끝나면 건너뛸 본문의 끝 표시
    before = ""
    i, n = 0, len(text)

    def cut_word() -> None:
        if word:
            words.append("".join(word))
            word.clear()

    def cut_statement(sep: str) -> None:
        nonlocal before
        cut_word()
        if words:
            out.append((words.copy(), before, sep))
            words.clear()
            before = sep
        elif sep != "\n":
            before = sep  # `a ||` 다음 줄바꿈은 이음을 끊지 않는다

    while i < n:
        c = text[i]
        pair = text[i : i + 2]
        inside = closers[-1] if closers else None
        if c == "\\":
            if pair != "\\\n":  # 줄 잇기는 없던 것으로 친다
                word.append(pair)
            i += 2
            continue
        if inside == '"':
            word.append(c)
            if c == '"':
                closers.pop()
            elif pair == "$(":
                word.append("(")
                closers.append(")")
                i += 1
            elif c == "`":
                closers.append("`")
            i += 1
            continue
        if c == "'":
            end = text.find("'", i + 1)
            end = n - 1 if end < 0 else end
            word.append(text[i : end + 1])
            i = end + 1
            continue
        if c == '"':
            word.append(c)
            closers.append('"')
        elif pair == "$(":
            word.append(pair)
            closers.append(")")
            i += 1
        elif c == "`":
            word.append(c)
            if inside == "`":
                closers.pop()
            else:
                closers.append("`")
        elif inside is not None:
            word.append(c)
            if c == "(":
                closers.append(")")
            elif c == ")" and inside == ")":
                closers.pop()
        elif c == "#" and not word:
            end = text.find("\n", i)
            i = n if end < 0 else end
            continue
        elif c == "(":
            word.append(c)
            closers.append(")")
        elif c in " \t\r":
            cut_word()
        elif c == "\n":
            cut_statement("\n")
            if heredocs:
                i = _past_heredocs(text, i + 1, heredocs)
                heredocs.clear()
                continue
        elif c == "&" and (pair == "&>" or (word and word[-1][-1] in "<>")):
            word.append(c)  # `2>&1` · `&>` 는 리디렉션이다
        elif c in ";&|":
            op = pair if pair in ("&&", "||", ";;", "|&") else c
            cut_statement(op)
            i += len(op)
            continue
        elif text.startswith("<<<", i):
            word.append("<<<")
            i += 3
            continue
        elif pair == "<<" and (doc := _HEREDOC.match(text, i)):
            cut_word()
            heredocs.append(doc.group(2))
            i = doc.end()
            continue
        else:
            word.append(c)
        i += 1
    cut_statement("")
    return out


def _flow(text: str) -> Iterator[tuple[list[str], bool, bool]]:
    """명령마다 (단어들, 조건부인가, 서브셸에서 도는가).

    `if`·`while`·`for`·`case` 안이거나 `&&`·`||` 뒤면 조건부다. 파이프로 이어졌거나 `&` 로 뒤에
    보낸 명령은 서브셸에서 돌아, 거기서 바꾼 변수가 셸에 남지 않는다.
    """
    depth = 0
    for words, before, after in _split(text):
        closes = 0
        for w in words:
            if w in _OPENERS:
                depth += 1
            elif w in _CLOSERS:
                closes += 1
            elif w not in _RESERVED:
                break
        conditional = depth > 0 or before in ("&&", "||")
        transient = after in ("|", "|&", "&") or before in ("|", "|&")
        yield words, conditional, transient
        depth = max(0, depth - closes)


def _past_heredocs(text: str, i: int, ends: list[str]) -> int:
    """heredoc 본문을 건너뛴 자리. 본문은 데이터라 그 안의 글자는 명령이 아니다."""
    for end in ends:
        while i < len(text):
            stop = text.find("\n", i)
            line = text[i:] if stop < 0 else text[i:stop]
            i = len(text) if stop < 0 else stop + 1
            if line.strip() == end:
                break
    return i


def _substitutions(word: str) -> list[str]:
    """단어 안의 명령 치환 본문(`$( )` · 백틱). 작은따옴표 안의 것은 글자일 뿐이다."""
    found: list[str] = []
    quoted = False  # 큰따옴표 안인가 — 그 안의 작은따옴표는 글자다
    i = 0
    while i < len(word):
        c = word[i]
        if c == "\\":
            i += 2
        elif c == '"':
            quoted = not quoted
            i += 1
        elif c == "'" and not quoted:
            end = word.find("'", i + 1)
            i = len(word) if end < 0 else end + 1
        elif word.startswith("$(", i):
            depth, j, mark = 1, i + 2, ""
            while j < len(word) and depth:
                ch = word[j]
                if mark:
                    mark = "" if ch == mark else mark
                elif ch in "'\"":
                    mark = ch
                elif ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                j += 1
            found.append(word[i + 2 : j - 1 if depth == 0 else j])
            i = j
        elif c == "`":
            end = word.find("`", i + 1)
            end = len(word) if end < 0 else end
            found.append(word[i + 1 : end])
            i = end + 1
        else:
            i += 1
    return found


def _commands(text: str, depth: int = 0) -> Iterator[tuple[list[str], bool, bool]]:
    """실행되는 명령 전부와 (조건부인가, 셸에 남는가) — 명령 치환·서브셸 안의 것까지.

    치환은 바깥 명령보다 먼저 돌고, 그 안에서 바꾼 변수는 바깥에 남지 않는다.
    """
    for words, conditional, transient in _flow(text):
        if depth < 3:
            for w in words:
                bodies = _substitutions(w)
                if w.startswith("(") and w.endswith(")"):
                    bodies.append(w[1:-1])  # `( … )` 서브셸
                for body in bodies:
                    for inner, inner_conditional, _ in _commands(body, depth + 1):
                        yield inner, conditional or inner_conditional, False
        yield words, conditional, depth == 0 and not transient


def _parts(words: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
    """(명령 앞에 붙은 대입들, 나머지 단어들). 예약어·case 패턴·함수 머리는 걷어낸다.

    나머지가 비면 그 대입은 셸에 남는다. 나머지가 있으면 **그 명령에만** 걸리는 대입이다.
    """
    i = 0
    while i < len(words):
        w = words[i]
        if w == "function":
            i += 2
        elif w in _RESERVED or _FUNC_HEAD.fullmatch(w) or (w.endswith(")") and "(" not in w):
            i += 1
        else:
            break
    assigns: list[tuple[str, str]] = []
    while i < len(words) and (pair := _ASSIGN.fullmatch(words[i])):
        assigns.append((pair.group(1), pair.group(2)))
        i += 1
    return assigns, words[i:]


def _program(words: list[str]) -> list[str]:
    """명령어부터의 단어들. `exec` · `command` · `env …` · `timeout 20` 같은 앞말은 걷어낸다."""
    i = 0
    while i < len(words):
        w = words[i]
        if w == "command":
            if words[i + 1 : i + 2] in (["-v"], ["-V"]):
                break  # `command -v x` 는 찾기만 한다 — 부르는 것이 아니다
            i += 1
            while i < len(words) and words[i].startswith("-"):
                i += 1  # `command -p` · `command --`
        elif w in _PREFIXES:
            i += 1
            while w == "env" and i < len(words):
                if words[i] in ("-u", "--unset", "-C", "--chdir"):
                    i += 2
                elif words[i].startswith("-") or _ASSIGN.fullmatch(words[i]):
                    i += 1
                else:
                    break
        elif w in ("timeout", "gtimeout"):
            i += 1
            while i < len(words) and words[i].startswith("-"):
                i += 1
            i += 1  # 시간
        else:
            break
    return words[i:]


def _bare(word: str) -> str:
    """바깥 따옴표 한 겹을 벗긴 글자. `$'…'` 도 벗긴다."""
    if len(word) >= 3 and word.startswith("$'") and word.endswith("'"):
        return word[2:-1]
    if len(word) >= 2 and word[0] == word[-1] and word[0] in "'\"":
        return word[1:-1]
    return word


def _mentions(tool: str, text: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(tool)}(?![\w-])", text) is not None


def _names(word: str, tool: str, held: set[str]) -> bool:
    """이 명령어 자리가 그 도구인가 — 경로로든, 담아 둔 변수로든, `$(command -v …)` 로든."""
    bare = _bare(word)
    if re.fullmatch(rf"(?:\S*/)?{re.escape(tool)}", bare):
        return True
    var = _VAR.fullmatch(bare)
    if var:
        return var.group(1) in held
    return _SUBST.fullmatch(bare) is not None and _mentions(tool, bare)


def _stored(words: list[str]) -> list[tuple[str, str]]:
    """셸에 **남는** 대입들. 명령 앞에만 붙은 대입은 그 명령과 함께 사라지므로 뺀다."""
    assigns, rest = _parts(words)
    if not rest:
        return assigns
    if rest[0] in _DECLARERS:
        return [(m.group(1), m.group(2)) for w in rest[1:] if (m := _ASSIGN.fullmatch(w))]
    return []


def _calls(text: str, tool: str) -> list[str]:
    """그 도구를 **명령으로 부르는** 자리마다 첫 인자(`rotate` 등). 안 부르면 빈 목록.

    변수에 담아 부르는 것도 센다 — `rotate_cmd="$(command -v codex-swap)"` 뒤의
    `"$rotate_cmd" rotate`. 조건부로 다시 담으면 둘 다 담긴 것으로(`[[ -n "$c" ]] || c=…`), 조건
    없이 다시 담으면 앞의 것은 사라진 것으로 본다. alias 는 이름이 `codex` 일 때만 센다 — 다른
    이름은 codex 를 칠 때 쓰이지 않는다.
    """
    held: set[str] = set()
    found: list[str] = []
    for words, conditional, persistent in _commands(text):
        if persistent:
            for name, value in _stored(words):
                if _mentions(tool, value):
                    held.add(name)
                elif not conditional:
                    held.discard(name)
        head = _program(_parts(words)[1])
        if not head:
            continue
        if head[0] == "alias":
            for w in head[1:]:
                alias = _ASSIGN.fullmatch(w)
                if alias and alias.group(1) == "codex":
                    found += _calls(_bare(alias.group(2)), tool)
        elif _names(head[0], tool, held):
            found.append(_bare(head[1]) if len(head) > 1 else "")
    return found


def _home_output(value: str, from_home: set[str], held: set[str]) -> bool:
    """그 값이 **통째로** `codex-swap home` 의 출력인가.

    뒤에 경로를 덧붙이거나, 파이프로 고치거나, 다른 출력을 이어 붙이면 다른 값이다. 실패했을
    때의 `|| true` 는 괜찮다. 작은따옴표 안의 `$( )` 는 명령이 아니라 글자다.
    """
    if value.startswith(("'", "$'")):
        return False
    bare = value[1:-1] if len(value) >= 2 and value[0] == value[-1] == '"' else value
    var = _VAR.fullmatch(bare)
    if var:
        return var.group(1) in from_home
    inner = _substitutions(bare) if _SUBST.fullmatch(bare) else []
    if len(inner) != 1:
        return False
    steps = _split(inner[0])
    if not steps or steps[0][2] not in ("", "\n", "||"):
        return False
    if any(before != "||" for _, before, _ in steps[1:]):
        return False
    head = _program(_parts(steps[0][0])[1])
    return len(head) > 1 and _bare(head[1]) == "home" and _names(head[0], "codex-swap", held)


def _drops_home(prefix: list[str]) -> bool:
    """`env -i` · `env -u CODEX_HOME` — 그 명령의 자식은 `CODEX_HOME` 을 못 받는다."""
    if "env" not in prefix:
        return False
    tail = prefix[prefix.index("env") + 1 :]
    for k, w in enumerate(tail):
        if w in ("-i", "-", "--ignore-environment", "-uCODEX_HOME", "--unset=CODEX_HOME"):
            return True
        if w in ("-u", "--unset") and tail[k + 1 : k + 2] == ["CODEX_HOME"]:
            return True
    return False


def passes_home(text: str) -> bool:
    """이 스크립트가 codex 에게 **codex-swap 이 정한 홈**을 넘기는가.

    인정하는 모양은 둘이다.

    - `codex-swap exec` 로 통째로 넘긴다 — 명령으로 부를 때만. 글자로 찍는 것은 아니다.
    - `CODEX_HOME` 을 `codex-swap home` 의 출력으로 정하고 **export 한다.** 직접이든
      (`export CODEX_HOME="$(codex-swap home)"`), 변수를 거치든(`h="$("$swap" home)"` →
      `export CODEX_HOME="$h"`). 그 상태가 첫 `rotate` 때도, 스크립트 끝에서도 살아 있어야 한다.

    위에서부터 차례로 따라가므로 뒤의 재대입·`unset`·`export -n` 이 앞의 설정을 지운다. 어느
    명령에든 다른 `CODEX_HOME=…` 을 앞에 붙이거나 `env -i`·`env -u CODEX_HOME` 으로 띄우면 그 자식은
    다른 홈을 받으므로 인정하지 않는다. `${CODEX_HOME:=…}` 도 인정하지 않는다 — 물려받은 앱의 홈을
    그대로 둔다.

    순서를 보는 이유가 있다. 홈을 정하기 전에 `rotate` 를 부르면, 물려받은 `CODEX_HOME` 이 앱의
    홈일 때 전환 가드가 "다른 홈을 골랐다" 며 멈추고 그 뒤로 전환이 한 번도 안 일어난다.
    """
    from_home: set[str] = set()  # 지금 값이 `codex-swap home` 에서 온 변수
    held: set[str] = set()  # codex-swap 을 담은 변수
    exported = False  # CODEX_HOME 이 자식에게 넘어가는가
    overridden = False  # 어느 명령엔가 다른 홈을 쥐여 줬나
    at_rotate: bool | None = None  # 첫 rotate 때 넘길 준비가 돼 있었나

    def ready() -> bool:
        return exported and "CODEX_HOME" in from_home

    def keep(name: str, value: str, conditional: bool) -> None:
        # 홈에서 왔다는 쪽도, 덮였다는 쪽도 조건부여도 믿는다(모듈 머리말의 예외).
        if _home_output(value, from_home, held):
            from_home.add(name)
        else:
            from_home.discard(name)
        if _mentions("codex-swap", value):
            held.add(name)
        elif not conditional:
            held.discard(name)

    for words, conditional, transient in _flow(text):
        assigns, rest = _parts(words)
        head = _program(rest)
        verb = _bare(head[0]) if head else ""
        args = head[1:]
        if not rest:
            if not transient:  # 파이프 속 대입은 서브셸과 함께 사라진다
                for name, value in assigns:
                    keep(name, value, conditional)
        elif verb in _DECLARERS:
            if transient:
                continue
            flags = [a for a in args if a[0] in "-+"]
            marks = verb == "export" or any(f[0] == "-" and "x" in f for f in flags)
            drops = any(
                (f[0] == "+" and "x" in f) or (verb == "export" and f[0] == "-" and "n" in f)
                for f in flags
            )
            for a in args:
                if a in flags:
                    continue
                pair = _ASSIGN.fullmatch(a)
                name = pair.group(1) if pair else _bare(a)
                if pair:
                    keep(name, pair.group(2), conditional)
                if name == "CODEX_HOME":
                    exported = False if drops else exported or marks
        elif verb == "unset":
            if transient:
                continue
            for a in args:
                from_home.discard(_bare(a))
                if _bare(a) == "CODEX_HOME":
                    exported = False
        elif verb == "alias":
            for a in args:
                alias = _ASSIGN.fullmatch(a)
                body = _bare(alias.group(2)) if alias and alias.group(1) == "codex" else ""
                if "exec" in _calls(body, "codex-swap"):
                    return True
        elif head:
            prefix = rest[: len(rest) - len(head)]
            given = assigns + [
                (m.group(1), m.group(2)) for w in prefix if (m := _ASSIGN.fullmatch(w))
            ]
            if _drops_home(prefix) or any(
                name == "CODEX_HOME" and not _home_output(value, from_home, held)
                for name, value in given
            ):
                overridden = True
            if len(head) > 1:
                sub = _bare(head[1])
                if sub == "exec" and _names(head[0], "codex-swap", held):
                    return True
                names_swap = _names(head[0], "codex-swap", held) or _VAR.fullmatch(_bare(head[0]))
                if sub == "rotate" and at_rotate is None and names_swap:
                    at_rotate = ready()
    return ready() and at_rotate is not False and not overridden


def calls_us(text: str) -> bool:
    """codex-swap 으로 **전환을** 부르는가 — `rotate` 나 `exec`.

    글자로 찍거나 주석에 적은 것, `home` 만 묻는 것은 치지 않는다. 홈만 받아 쓰는 wrapper 로는
    codex 를 칠 때 전환이 일어나지 않는다.
    """
    return bool({"rotate", "exec"} & set(_calls(text, "codex-swap")))


def uses_legacy_rotator(text: str) -> bool:
    """**옛 bash 전환기**를 명령으로 부르고, codex-swap 으로는 전환하지 않는가."""
    return bool(_calls(text, LEGACY_ROTATOR)) and not calls_us(text)


def _head(path: Path) -> str:
    """스크립트 본문. 앞 두 바이트가 `#!` 일 때만 읽는다 — 진짜 바이너리는 통째로 읽지 않는다.

    앞 8KB 만 보던 때는 뒤쪽의 `unset CODEX_HOME` 이 잘려 나가 "넘긴다" 로 읽혔다.
    """
    try:
        with path.open("rb") as fh:
            if fh.read(2) != b"#!":
                return ""
            raw = b"#!" + fh.read(_SCRIPT_LIMIT)
    except OSError:
        return ""
    return raw.decode("utf-8", "replace")


def wrapper_here(path: Path | None = None) -> bool:
    """그 자리에 **우리가 놓은** wrapper 가 있나."""
    target = (path or WRAPPER).expanduser()
    return MARKER in _head(target)


def occupied_by_other(path: Path | None = None) -> bool:
    """그 자리에 **남의 것**이 있나. 덮으면 안 되는 상태다."""
    target = (path or WRAPPER).expanduser()
    return target.exists() and not wrapper_here(target)


OURS = "ours"
EXTERNAL = "external"
LEGACY = "legacy"
PROFILE = "profile"
FOREIGN = "foreign"
NONE = "none"


@dataclass(frozen=True)
class Wiring:
    """codex 가 지금 어떻게 뜨는가."""

    kind: str
    path: Path | None = None
    passes_home: bool = False

    def complete(self, isolate: bool) -> bool:
        """이 배선으로 전환이 **실제로 codex 에 닿는가.**

        앱과 나뉘어 있지 않으면(`isolate` 가 거짓) 전환만 되면 된다. 나뉘어 있으면 홈까지 넘겨야
        한다.
        """
        if self.kind == OURS:
            return True
        if self.kind in (EXTERNAL, PROFILE):
            return self.passes_home or not isolate
        return False


def inspect(shell: str) -> Wiring:
    """codex 가 **실제로 어떤 경로로 뜨는지** 본다.

    PATH 를 먼저 본다. 셸 프로필만 뒤지면 dotfiles 로 심어 둔 wrapper 를 못 보고, 멀쩡히
    돌아가는 기기에 "배선이 빠졌다" 고 말하게 된다 — 실제로 그렇게 오탐했다.
    """
    found = shutil.which("codex")
    if found:
        target = Path(found)
        head = _head(target)
        if MARKER in head:
            return Wiring(OURS, target, passes_home=True)
        if uses_legacy_rotator(head):
            return Wiring(LEGACY, target)
        if calls_us(head):
            return Wiring(EXTERNAL, target, passes_home=passes_home(head))

    for name in (profile_for(shell), "~/.bashrc", "~/.zshrc", "~/.profile"):
        path = Path(name).expanduser()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if calls_us(text):
            return Wiring(PROFILE, path, passes_home=passes_home(text))

    own = WRAPPER.expanduser()
    if own.exists() and not wrapper_here(own):
        return Wiring(LEGACY if uses_legacy_rotator(_head(own)) else FOREIGN, own)
    return Wiring(NONE)


def already_wired(shell: str) -> Path | None:
    """전환이 걸려 있는 배선의 위치. 없으면 None. (홈까지 넘기는지는 `inspect` 가 가린다.)"""
    state = inspect(shell)
    return state.path if state.kind in (OURS, EXTERNAL, PROFILE) else None


def install_wrapper(path: Path | None = None) -> Path:
    """wrapper 를 놓는다. 이미 남의 것이 있으면 건드리지 않고 예외를 올린다."""
    target = (path or WRAPPER).expanduser()
    if occupied_by_other(target):
        raise FileExistsError(str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(WRAPPER_BODY, encoding="utf-8")
    target.chmod(0o755)
    return target


def on_path(path: Path | None = None) -> bool:
    """그 자리가 PATH 에 들어 있나. 놓았는데 PATH 에 없으면 아무 일도 일어나지 않는다."""
    target = (path or WRAPPER).expanduser()
    entries = [Path(p).expanduser() for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    return target.parent in entries


def profile_for(shell: str) -> str:
    """그 셸이 읽는 파일. 안내에만 쓴다 — **우리가 쓰지는 않는다.**"""
    return {
        "zsh": "~/.zshrc",
        "fish": "~/.config/fish/config.fish",
    }.get(shell, "~/.bashrc")
