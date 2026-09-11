"""공식 앱과 한 기기에서 같이 사는가 — 그걸 재는 테스트 파일들이 함께 딛는 바닥.

임시 HOME(`box`) 과 그 안에 가짜 로그인·스크립트를 까는 도구만 둔다. 실제 codex 도, 실제 토큰도
쓰지 않는다. 한 파일에서만 쓰는 준비 코드는 그 파일에 둔다.

`box` 는 픽스처라 테스트 파일에서 이름으로 import 하면 쓰지 않은 import 로 읽힌다(F401·F811).
그렇다고 파일마다 `pytest_plugins` 로 부르면, 도구 함수를 먼저 import 한 탓에 assert 재작성을
못 한다는 경고가 뜬다. 그래서 `conftest.py` 가 이 모듈을 플러그인으로 한 번 등록하고, 테스트
파일은 도구 함수만 import 한다.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

from codex_swap import cli
from codex_swap.core import discovery, wiring


def _auth(path: Path, email: str, refresh: str = "rt-default") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    claims = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    body = {"tokens": {"id_token": f"h.{claims}.s", "refresh_token": refresh}}
    path.write_text(json.dumps(body))


@pytest.fixture
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """임시 HOME 하나. 앱 설치 여부는 테스트가 정한다."""
    home = tmp_path / "home"
    (home / ".local/bin").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{home / '.local/bin'}{os.pathsep}{os.environ['PATH']}")
    for var in ("CODEX_ACCOUNT_DEFAULT_HOME", "CODEX_ACCOUNTS_DIR", "CODEX_HOME"):
        monkeypatch.delenv(var, raising=False)
    app = home / "Applications/ChatGPT.app"
    monkeypatch.setattr(wiring, "APP_PATHS", (str(app),))
    monkeypatch.setattr(wiring, "DEFAULT_HOME", home / ".codex")
    monkeypatch.setattr(wiring, "ISOLATED_PATH", home / ".codex-cli")
    monkeypatch.setattr(wiring, "WRAPPER", home / ".local/bin/codex")
    monkeypatch.setattr(wiring.shutil, "which", lambda _: None)
    monkeypatch.setattr(discovery, "resolve_codex_bin", lambda: Path("/bin/true"))
    monkeypatch.setattr(cli.discovery, "resolve_codex_bin", lambda: Path("/bin/true"))

    class Box:
        pass

    b = Box()
    b.home = home
    b.app = app
    b.mp = monkeypatch
    return b


def _script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def _on_path(box, body: str) -> Path:
    w = _script(box.home / "bin/codex", body)
    box.mp.setattr(wiring.shutil, "which", lambda _: str(w))
    return w
