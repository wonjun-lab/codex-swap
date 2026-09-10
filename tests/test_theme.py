"""밝은 터미널에서도 읽히는가.

배경은 예전부터 건드리지 않았다(`use_default_colors`). 문제는 **전경**이다 — 기본 8 색의
노랑은 흰 바탕에서 거의 사라진다. 그래서 여기서 재는 것은 "색이 예쁜가" 가 아니라
**밝은 바탕에서 밝은 색을 쓰지 않는가** 다.

터미널이 필요한 부분은 `ask_background` 하나뿐이고, 나머지는 전부 순수 함수다.
"""

from __future__ import annotations

import pytest

from codex_swap.core import theme

WHITE = (255, 255, 255)
BLACK = (0, 0, 0)


@pytest.mark.parametrize(
    ("rgb", "light"),
    [
        (WHITE, True),
        (BLACK, False),
        ((240, 240, 240), True),  # 흔한 밝은 테마
        ((30, 30, 30), False),  # 흔한 어두운 테마
        ((253, 246, 227), True),  # solarized light
        ((0, 43, 54), False),  # solarized dark
    ],
)
def test_the_background_decides_the_theme(rgb: tuple[int, int, int], light: bool) -> None:
    assert theme.is_light(*rgb) is light


def test_a_blue_background_is_not_called_light() -> None:
    """세 채널을 그냥 평균 내면 남색이 밝다고 나온다.

    사람 눈은 파랑에 둔해서, 순수한 파랑은 같은 수치의 초록보다 훨씬 어둡게 보인다.
    가중치 없이 재면 남색 바탕에 어두운 글자를 얹어 아무것도 안 보이게 된다.
    """
    assert not theme.is_light(0, 0, 255)
    assert theme.is_light(0, 255, 0), "초록은 반대로 밝게 읽혀야 한다"


@pytest.mark.parametrize(
    "reply",
    [
        "\033]11;rgb:ffff/ffff/ffff\033\\",
        "\033]11;rgb:ff/ff/ff\a",
        "\033]11;rgb:f/f/f\033\\",
    ],
)
def test_any_channel_width_is_understood(reply: str) -> None:
    """터미널마다 자릿수가 다르다 — `ffff/ffff/ffff` 도 있고 `ff/ff/ff` 도 있다."""
    assert theme.parse_osc11(reply) == WHITE


def test_a_black_background_survives_the_scaling() -> None:
    assert theme.parse_osc11("\033]11;rgb:0000/0000/0000\033\\") == BLACK


@pytest.mark.parametrize("junk", ["", "no reply", "\033]11;?\033\\", "rgb:zz/zz/zz"])
def test_garbage_is_not_guessed_at(junk: str) -> None:
    """못 읽은 것을 아무 색으로 접으면, 답하지 않는 터미널이 엉뚱한 테마를 받는다."""
    assert theme.parse_osc11(junk) is None


# ── 사람이 고른 것이 이긴다 ────────────────────────────────────────────────


@pytest.mark.parametrize("want", ["light", "LIGHT", " light "])
def test_the_env_var_wins_over_detection(want: str) -> None:
    """자동 감지는 터미널이 정직할 때만 맞다. 틀렸을 때 스스로 못 고치면 도구를 못 쓴다."""
    env = {theme.ENV_VAR: want, "COLORFGBG": "15;0"}
    assert theme.detect(env, background=BLACK) == theme.LIGHT


@pytest.mark.parametrize("value", ["auto", "", "nonsense"])
def test_an_unset_or_odd_value_falls_back_to_detection(value: str) -> None:
    assert theme.chosen({theme.ENV_VAR: value}) is None
    assert theme.detect({theme.ENV_VAR: value}, background=WHITE) == theme.LIGHT


def test_colorfgbg_is_used_when_the_terminal_did_not_answer() -> None:
    assert theme.detect({"COLORFGBG": "0;15"}, background=None) == theme.LIGHT
    assert theme.detect({"COLORFGBG": "15;0"}, background=None) == theme.DARK


def test_a_non_numeric_colorfgbg_is_not_read_as_light() -> None:
    """`15;default` 를 넣는 터미널이 있다. 숫자가 아니면 모르는 것이다."""
    assert theme.from_colorfgbg("15;default") is None


def test_knowing_nothing_means_dark() -> None:
    """**틀렸을 때의 손해가 작은 쪽으로 간다.**

    어두운 바탕에 어두운 글자를 쓰면 아무것도 안 보이지만, 밝은 바탕에 밝은 글자는
    흐릿할지언정 읽힌다.
    """
    assert theme.detect({}, background=None) == theme.DARK


# ── 팔레트가 실제로 다른가 ─────────────────────────────────────────────────


def test_the_light_palette_avoids_yellow() -> None:
    """**이 테마가 존재하는 이유다.** 노랑은 흰 바탕에서 글자가 사라진다."""
    assert theme.palette(theme.LIGHT)["warn"] != 3
    assert theme.palette(theme.LIGHT, colors=8)["warn"] != 3


def test_the_dark_palette_is_the_familiar_eight() -> None:
    """어두운 바탕에서는 오래 쓰인 조합을 그대로 둔다 — 어디서나 있는 색이다."""
    assert theme.palette(theme.DARK) == {"ok": 2, "warn": 3, "danger": 1, "accent": 6}


def test_a_light_terminal_without_256_colors_still_gets_something_readable() -> None:
    """256 색이 없다고 어두운 바탕용 색으로 되돌리면 경고가 안 보인다."""
    got = theme.palette(theme.LIGHT, colors=8)
    assert all(0 <= v < 8 for v in got.values()), got
    assert got != theme.palette(theme.DARK)


def test_every_tone_has_a_colour_in_every_palette() -> None:
    """하나라도 빠지면 그 자리에서 `KeyError` 다 — 화면이 아예 안 뜬다."""
    tones = {"ok", "warn", "danger", "accent"}
    for want in (theme.DARK, theme.LIGHT):
        for colors in (8, 256):
            assert set(theme.palette(want, colors)) == tones


def test_the_palette_cannot_be_mutated_through_a_caller() -> None:
    """사본을 안 주면 한 화면에서 고친 색이 다음 호출에 남는다."""
    got = theme.palette(theme.DARK)
    got["ok"] = 99
    assert theme.palette(theme.DARK)["ok"] != 99
