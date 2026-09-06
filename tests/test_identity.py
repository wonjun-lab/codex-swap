"""깨진 자격증명 하나가 뒤 슬롯의 신원 해석이나 터미널 출력을 오염시키면 안 된다."""

import base64
import json
from pathlib import Path

import pytest

from codex_swap.core import identity


def encoded(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def auth(path: Path, token: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tokens": {"id_token": token}}))
    return path


@pytest.mark.parametrize(
    "token",
    [
        None,
        False,
        42,
        {},
        "",
        "not-a-jwt",
        "secret.A.signature",
        "secret.!!!.signature",
        "secret.\ud800.signature",
        "secret.\udfff.signature",
        f"secret.{encoded(b'not json')}.signature",
        f"secret.{encoded(bytes([255]))}.signature",
        *[
            f"secret.{encoded(json.dumps(value).encode())}.signature"
            for value in [
                [],
                None,
                7,
                {},
                {"email": None},
                {"email": ""},
                {"email": False},
                {"email": 42},
            ]
        ],
    ],
)
def test_invalid_token_is_silent_unknown(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    token: object,
) -> None:
    # 반환뿐 아니라 출력도 본다 — 실패 경로의 로그 한 줄도 토큰 누출이 된다.
    assert identity.email_of(auth(tmp_path / "auth.json", token)) is None
    assert capsys.readouterr() == ("", "")
    assert caplog.text == ""


@pytest.mark.parametrize("raw", [b"\xff", b"{", b"[]", b"null", b"{}", b'{"tokens":[]}'])
def test_invalid_document_has_no_identity(tmp_path: Path, raw: bytes) -> None:
    path = tmp_path / "auth.json"
    path.write_bytes(raw)
    assert identity.email_of(path) is None


@pytest.mark.parametrize("kind", ["missing", "directory", "unreadable"])
def test_unreadable_auth_is_unknown(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "auth.json"
    if kind == "directory":
        path.mkdir()
    elif kind == "unreadable":
        auth(path, "secret.token.signature").chmod(0o000)
    try:
        assert identity.email_of(path) is None
    finally:
        if kind == "unreadable":
            path.chmod(0o600)


@pytest.mark.parametrize("email", ["a@b.co", "ab@b.co", "abc@b.co", "abcd@b.co"])
def test_valid_payload_padding_returns_only_email(tmp_path: Path, email: str) -> None:
    token = f"secret.{encoded(json.dumps({'email': email}).encode())}.signature"
    assert identity.email_of(auth(tmp_path / "auth.json", token)) == email


def test_non_alphabet_cannot_be_discarded(tmp_path: Path) -> None:
    # 네 글자를 끼워 패딩 길이를 유지한다. 하나면 검증을 꺼도 패딩 오류로 거부된다.
    payload = encoded(b'{"email":"a@example.com"}')
    assert (
        identity.email_of(auth(tmp_path / "auth.json", f"h.{payload[:4]}!!!!{payload[4:]}.s"))
        is None
    )


def test_dotless_payload_preserves_cut_semantics(tmp_path: Path) -> None:
    # JWT 구조 검증기는 아니다. 점이 없으면 전체를 payload 로 쓰는 bash 계약을 보존한다.
    assert (
        identity.email_of(auth(tmp_path / "auth.json", encoded(b'{"email":"a@b.co"}'))) == "a@b.co"
    )


def test_active_email_reads_live_home(tmp_path: Path) -> None:
    auth(tmp_path / "auth.json", "h." + encoded(b'{"email":"live@b.co"}') + ".s")
    auth(tmp_path / "accounts/a/auth.json", "h." + encoded(b'{"email":"stale@b.co"}') + ".s")
    assert identity.active_email(tmp_path) == "live@b.co"


def test_first_matching_label_wins_after_corrupt_slot(tmp_path: Path) -> None:
    auth(tmp_path / "broken/auth.json", "h.\ud800.s")
    for label in ("a", "b"):
        auth(tmp_path / label / "auth.json", "h." + encoded(b'{"email":"same@b.co"}') + ".s")
    assert identity.active_label(tmp_path, "same@b.co", iter(["", "broken", "b", "a"])) == "b"
    assert identity.active_label(tmp_path, "same@b.co", ["a", "b"]) == "a"
    assert identity.active_label(tmp_path, "unknown@b.co", ["broken", "a"]) is None
    assert identity.active_label(tmp_path, "", ["a"]) is None
