"""`init` — 앱과 같이 사는 기기의 배선 판정을 사람에게 어떻게 말하나.

공식 앱과 한 기기에서 같이 살면서 실제로 본 것들이 이 파일의 출발점이다.

- 같은 refresh token 을 두 곳이 쥐어, 앱의 codex 로그에 `401 Encountered invalidated oauth
  token` 이 수만 줄 쌓였다.
- dotfiles wrapper 는 전환을 하긴 했지만 codex 를 앱의 홈으로 띄웠고, 다른 기기의 wrapper 는
  아예 옛 bash 전환기를 부르고 있었다.

여기서 재는 것은 `init` 이 그런 기기를 **준비됐다고 말하지 않는가**, 그리고 고치라는 안내가 그
일을 다시 일으키지 않는가다. 실제 codex 도, 실제 토큰도 쓰지 않는다. 판정에 넣는 입력은 **제품
상수를 되받아 쓰지 않고 글자 그대로 적는다** — 상수를 입력으로 쓰면 그 상수가 틀려도 테스트가
따라 틀려서 아무것도 못 잡는다.

임시 HOME `box` 는 `_coexist.py` 에 있다.
"""

from __future__ import annotations

from _coexist import _auth, _on_path
from codex_swap import cli
from codex_swap.core import config


def _register_two(box) -> config.Settings:
    s = config.load()
    _auth(s.accounts_dir / "work/auth.json", "work@example.com", refresh="rt-work")
    _auth(s.accounts_dir / "personal/auth.json", "personal@example.com", refresh="rt-personal")
    _auth(s.default_home / "auth.json", "work@example.com", refresh="rt-work")
    return s


# ── init: 그 판정을 사람에게 어떻게 말하나 ─────────────────────────────────────


def test_init_flags_a_half_wired_machine_instead_of_calling_it_ready(box, capsys) -> None:
    """계정을 둘 등록해 둔다 — 종료 코드 1 이 **배선 때문**이라는 것을 가려내려고."""
    box.app.mkdir(parents=True)
    _register_two(box)
    _on_path(box, '#!/bin/bash\ncodex-swap rotate >/dev/null || true\nexec real "$@"\n')
    code = cli.main(["init"])
    out = capsys.readouterr().out
    assert code == 1, out
    assert "ready" not in out, "전환이 codex 에 안 닿는데 준비됐다고 했다"
    assert 'CODEX_HOME="$(codex-swap home)"' in out, out
    assert "before it runs codex-swap rotate" in out, out


def test_init_puts_the_home_line_before_the_rotate_line(box, capsys) -> None:
    box.app.mkdir(parents=True)
    _register_two(box)
    _on_path(box, '#!/bin/bash\n"$d/codex-account" rotate\nexec real "$@"\n')
    cli.main(["init"])
    out = capsys.readouterr().out
    assert "codex-account" in out, out
    assert out.index("codex-swap home") < out.index("codex-swap rotate"), out


def test_init_is_ready_on_a_machine_without_the_app(box, capsys) -> None:
    """ml-main 모양: 앱 없음, dotfiles wrapper 는 `rotate` 만 부른다. 이것은 **완성된** 배선이다."""
    _register_two(box)
    _on_path(box, '#!/bin/bash\ncodex-swap rotate >/dev/null || true\nexec real "$@"\n')
    assert cli.main(["init"]) == 0
    assert "ready" in capsys.readouterr().out


def test_init_does_not_claim_apart_when_pointed_at_the_apps_home(box, capsys) -> None:
    """**설치돼 있다와 나뉘어 있다는 다르다.**

    앱의 홈을 직접 가리킨 설정에 "나눠 뒀다" 고 말했었다.
    """
    box.app.mkdir(parents=True)
    box.mp.setenv("CODEX_ACCOUNT_DEFAULT_HOME", str(box.home / ".codex"))
    _register_two(box)
    code = cli.main(["init"])
    out = capsys.readouterr().out
    assert code == 1, out
    assert "kept apart" not in out, out
    assert "both using" in out, out


def test_init_flags_an_exported_app_home(box, capsys) -> None:
    box.app.mkdir(parents=True)
    _register_two(box)
    box.mp.setenv("CODEX_HOME", str(box.home / ".codex"))
    assert cli.main(["init"]) == 1
    assert "exports CODEX_HOME" in capsys.readouterr().out


def test_init_never_tells_you_to_copy_the_apps_login(box, capsys) -> None:
    """**앱의 `auth.json` 을 베끼면 refresh token 을 앱과 나눠 쥔다.**

    예전 안내가 정확히 그 복사를 시켰다(`cp ~/.codex/auth.json …`).
    """
    box.app.mkdir(parents=True)
    _auth(box.home / ".codex/auth.json", "app@example.com")
    cli.main(["init"])
    out = capsys.readouterr().out
    assert "cp " not in out, out
    assert "adopt" not in out, "앱이 있는 기기에서 '지금 로그인' 을 가져오게 했다"
    assert "codex-swap add" in out, out


def _app_login_copied_into(box, *labels: str) -> None:
    """앱의 로그인을 베껴 등록한 슬롯. 활성 홈(`~/.codex-cli`)은 아직 비어 있다."""
    s = config.load()
    _auth(box.home / ".codex/auth.json", "app@example.com", refresh="rt-app")
    _auth(s.accounts_dir / "work/auth.json", "work@example.com", refresh="rt-app")
    _auth(s.accounts_dir / "personal/auth.json", "personal@example.com", refresh="rt-personal")
    for label in labels:
        _auth(s.accounts_dir / f"{label}/auth.json", f"{label}@example.com", refresh="rt-app")


def test_init_does_not_fill_the_home_from_a_slot_that_shares_the_apps_login(box, capsys) -> None:
    """**그 슬롯으로 `use` 하면 같은 refresh token 을 쥔 곳이 셋이 된다.**

    예전 안내는 슬롯이 있기만 하면 "한 번 전환해서 채워라" 였다. 앱 기기에서는 그 슬롯이 대개
    앱의 로그인을 베낀 것이라, 안내대로 하면 공유가 하나 더 늘었다.
    """
    box.app.mkdir(parents=True)
    _app_login_copied_into(box)
    code = cli.main(["init"])
    out = capsys.readouterr().out
    assert code == 1, out
    assert "codex-swap add work --force" in out, out
    assert "codex-swap use work" not in out, out
    assert "codex-swap use personal" in out, out
    assert "codex-swap add personal --force" not in out, out


def test_init_offers_no_switch_when_every_slot_shares_the_apps_login(box, capsys) -> None:
    box.app.mkdir(parents=True)
    _app_login_copied_into(box, "personal")
    cli.main(["init"])
    out = capsys.readouterr().out
    assert "codex-swap use" not in out, out
    assert "codex-swap add work --force" in out, out
    assert "codex-swap add personal --force" in out, out


def test_init_flags_a_shared_slot_even_when_the_home_is_already_filled(box, capsys) -> None:
    """홈이 채워져 있어도 **공유는 남는다** — 앱이나 그 슬롯 중 먼저 갱신하는 쪽이 이긴다."""
    box.app.mkdir(parents=True)
    _app_login_copied_into(box)
    _auth(config.load().default_home / "auth.json", "personal@example.com", refresh="rt-personal")
    _on_path(box, '#!/bin/bash\nexport CODEX_HOME="$(codex-swap home)"\nexec real "$@"\n')
    code = cli.main(["init"])
    out = capsys.readouterr().out
    assert code == 1, out
    assert "codex-swap add work --force" in out, out
    assert "ready" not in out, out


def test_init_does_not_mention_sharing_where_there_is_no_app(box, capsys) -> None:
    """앱이 없으면 `~/.codex` 는 codex-swap 의 활성 홈이다. 슬롯과 같은 토큰인 것이 정상이다."""
    _auth(box.home / ".codex/auth.json", "work@example.com", refresh="rt-work")
    _register_two(box)
    _on_path(box, '#!/bin/bash\ncodex-swap rotate >/dev/null || true\nexec real "$@"\n')
    assert cli.main(["init"]) == 0
    assert "--force" not in capsys.readouterr().out


def test_init_still_offers_adopt_where_there_is_no_app(box, capsys) -> None:
    _auth(box.home / ".codex/auth.json", "me@example.com")
    cli.main(["init"])
    assert "codex-swap adopt" in capsys.readouterr().out
