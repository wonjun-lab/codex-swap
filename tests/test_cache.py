"""사용량 캐시 — **적대적 입력**과 쓰기 실패.

이 파일이 재는 것은 캐시의 행복한 경로가 아니다. 그건 `test_rotate.py` · `test_cli.py` 가
이미 지난다. 여기는 캐시 파일이 **사용자가 고칠 수 있는 파일**이라는 사실에서 나오는 자리다.

`_seconds` 는 그 사실 때문에 존재한다. 그런데 정수 분기 말고는 아무도 안 재고 있었다 —
`int(value)` 로 단순화하는 리팩터가 `"²"`(예외를 던진다)와 `5.5`(잘린다)를 조용히 깬다.
그리고 ts 파싱이 틀리면 낡은 값이 신선한 것으로 읽혀 **전환 판단이 틀린다.** 화면에는
아무 경고도 없다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_swap.core import cache, config
from codex_swap.core.cache import _seconds


@pytest.fixture
def settings(_isolated_home: Path) -> config.Settings:
    return config.load()


# ── ts 파싱 ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "want"),
    [
        (None, 0),  # 키가 없는 옛 항목 — 나이 0 으로 보고 TTL 이 판단한다
        (0, 0),
        (5, 5),
        (-1, None),  # 미래의 ts 는 시계가 틀렸다는 뜻이라 믿지 않는다
        (5.0, 5),  # jq 를 지난 값이 float 로 오는 일이 있다
        (5.5, None),
        (-2.0, None),
        ("5", 5),
        ("0", 0),
        ("-1", None),
        ("5.0", None),
        ("", None),
        ("abc", None),
        ([], None),
        ({}, None),
        (float("nan"), None),
        (float("inf"), None),
    ],
)
def test_seconds_reads_only_what_it_can_trust(value: object, want: int | None) -> None:
    assert _seconds(value) == want


@pytest.mark.parametrize("text", ["²", "٣", "½", "Ⅳ"])
def test_seconds_refuses_digits_that_int_would_reject(text: str) -> None:
    """`str.isdigit()` 은 참인데 `int()` 는 던지는 글자들.

    `isascii()` 를 함께 걸지 않으면 여기서 트레이스백이 stderr 로 나간다. 캐시 파일은
    사용자가 고칠 수 있으므로 그건 사용자 잘못이 아니라 이쪽 결함이다.
    """
    assert _seconds(text) is None


def test_seconds_never_says_true_is_a_timestamp() -> None:
    """`True` 는 `isinstance(x, int)` 가 참이라 1 초로 읽힐 수 있다.

    `False` 만 0 으로 받는 것은 bash 의 빈 값과 맞추기 위해서다.
    """
    assert _seconds(True) is None
    assert _seconds(False) == 0


# ── 읽기가 못 믿을 파일을 만났을 때 ────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "{ not json",
        "[]",  # 객체가 아니다
        '"just a string"',
        "null",
        '{"master": "not an object"}',
        '{"master": {}}',  # ts 가 없다
        '{"master": {"ts": "abc"}}',
        '{"master": {"ts": true}}',
    ],
)
def test_a_file_we_cannot_trust_reads_as_a_miss(settings: config.Settings, text: str) -> None:
    """캐시 미스로 접어야 한다. 던지면 rotate 가 매 codex 호출에서 죽는다."""
    (settings.accounts_dir / ".usage-cache.json").write_text(text)
    assert cache.read(settings, "master") is None


def test_a_missing_file_reads_as_a_miss(settings: config.Settings) -> None:
    assert cache.read(settings, "master") is None


def test_a_good_entry_survives_a_broken_neighbour(settings: config.Settings) -> None:
    """항목 하나가 깨졌다고 나머지를 버리면 멀쩡한 계정까지 다시 조회한다."""
    cache.write(settings, "master", {"usedPercent": 40}, now=1000)
    doc = json.loads((settings.accounts_dir / ".usage-cache.json").read_text())
    doc["shared"] = "garbage"
    (settings.accounts_dir / ".usage-cache.json").write_text(json.dumps(doc))

    assert cache.read(settings, "shared") is None
    assert cache.read_stale(settings, "shared") is None
    entry = cache.read(settings, "master", now=1030)
    assert entry is not None and entry["usedPercent"] == 40


# ── 쓰기가 실패할 때 ────────────────────────────────────────────────────────


def test_writing_something_json_cannot_hold_returns_false(settings: config.Settings) -> None:
    """직렬화되지 않는 payload 도 조용히 거절해야 한다 — 예외로 새면 rotate 가 죽는다."""
    assert cache.write(settings, "master", {"bad": object()}, now=1000) is False


def test_a_failed_write_leaves_no_temp_behind(settings: config.Settings) -> None:
    """temp 를 남기면 다음 실행이 그것을 덮어쓰며 mode 를 잃는다."""
    cache.write(settings, "master", {"bad": object()}, now=1000)
    assert not list(settings.accounts_dir.glob(".usage-cache.json.tmp.*"))


def test_a_failed_write_does_not_destroy_what_was_there(settings: config.Settings) -> None:
    """문서를 통째로 읽고 다시 쓰는 구조라, 실패가 기존 항목을 지우면 안 된다."""
    cache.write(settings, "master", {"usedPercent": 40}, now=1000)
    cache.write(settings, "shared", {"bad": object()}, now=1000)
    entry = cache.read(settings, "master", now=1000)
    assert entry is not None and entry["usedPercent"] == 40


def test_a_write_into_a_root_we_cannot_make_returns_false(
    settings: config.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """루트를 못 만드는 경우(권한·읽기전용). 캐시를 못 쓰는 것은 치명적이 아니다."""

    def boom(_: config.Settings) -> None:
        raise OSError(13, "Permission denied")

    monkeypatch.setattr("codex_swap.core.paths.ensure_root", boom)
    assert cache.write(settings, "master", {"usedPercent": 40}, now=1000) is False


def test_the_cache_file_is_not_world_readable(settings: config.Settings) -> None:
    """계정 email 이 담긴다. rename 뒤에 chmod 하면 그 사이 창이 열린다."""
    cache.write(settings, "master", {"email": "a@example.com"}, now=1000)
    path = settings.accounts_dir / ".usage-cache.json"
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_the_ttl_boundary_is_where_the_two_readers_split(settings: config.Settings) -> None:
    """`read` 는 TTL 을 지키고 `read_stale` 은 무시한다. 그 경계가 정확히 어디인가.

    정책 경로가 `read_stale` 을 쓰면 TTL 예산이 통째로 사라진다. 두 함수가 같은 값을
    돌려주기 시작하면 그 구분이 사라진 것이므로 여기서 난다.
    """
    ttl = settings.cache_ttl
    cache.write(settings, "master", {"usedPercent": 40}, now=1000)

    assert cache.read(settings, "master", now=1000 + ttl) is not None, "경계 안인데 미스다"
    assert cache.read(settings, "master", now=1000 + ttl + 1) is None, "경계를 넘었는데 히트다"

    stale = cache.read_stale(settings, "master", now=1000 + ttl + 1)
    assert stale is not None and stale[1] == ttl + 1
