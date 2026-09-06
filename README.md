# codex-swap

Codex CLI 계정을 여러 개 보관하고, 활성 계정의 사용량이 임계를 넘으면 자동으로 갈아끼운다.

`~/dotfiles/codex/codex-account` (bash) 의 Python 재구현이다. 상태 레이아웃과 정책은
그대로 유지하고 구현만 옮겼다. **이관은 끝났고 bash 판은 2026-09-06 에 제거됐다.**

## 왜 옮겼는가

bash 판은 481 줄의 정책 엔진을 담고 있었는데, 그 핵심인 사다리 판정이 프로브·파일 I/O 와
엉켜 있어 격리 테스트가 되지 않았다. Python 판은 정책을 순수 함수로 떼어내 네트워크 없이
전 조합을 검증한다.

부수 효과로 외부 의존이 사라졌다. bash 판은 `jq` · `base64` · `node` 를 요구했다.
`node` 는 사용량 프로브가 JavaScript 로 쓰여 있었기 때문인데, 프로브가 하는 일은
`codex app-server` 를 자식으로 띄워 NDJSON JSON-RPC 를 주고받는 것뿐이라 표준 라이브러리로
그대로 대체된다.

## 설치

```bash
uv tool install git+ssh://git@github.com/wonjun-lab/codex-swap.git
```

## 명령

| 명령 | 하는 일 |
| --- | --- |
| `codex-swap adopt <label>` | 지금 로그인된 계정을 `<label>` 슬롯에 등록 |
| `codex-swap add <label>` | 새 슬롯에 로그인 (브라우저 인증) |
| `codex-swap list` | 등록된 계정과 캐시된 사용량 |
| `codex-swap status [--fresh]` | 활성 계정과 사용량 |
| `codex-swap use <label>` | 수동 전환 |
| `codex-swap rotate [--dry-run]` | 정책 실행 (wrapper 가 매 호출 부른다) |
| `codex-swap remove <label>` | 슬롯 삭제 |
| `codex-swap clean` | 슬롯의 프로브 부산물 정리 |

자동 전환 끄기: `touch ~/.claude/.codex-rotate-off`

## 상태

bash 판이 쓰던 **파일을 같은 자리에** 그대로 쓴다. 병행 기간에 둘을 나란히 두려고 맞춘
것인데, bash 가 사라진 뒤에도 바꾸지 않는다 — 이미 배포된 기기의 디스크에 이 이름으로
상태가 있다.

```
~/.codex/auth.json                    활성 계정 (여기 담긴 email 이 곧 "지금 어느 계정인가")
~/.codex/accounts/<label>/auth.json   슬롯별 자격증명 (mode 600, 디렉토리 700)
~/.codex/accounts/.usage-cache.json   라벨별 사용량 캐시 (TTL)
~/.codex/accounts/.last-rotate        마지막 자동 전환 시각 (쿨다운)
~/.codex/accounts/.last-check         마지막 판단 시각 (스로틀)
~/.codex/accounts/rotate.log          전환 원장 (토큰은 남기지 않는다)
```

"지금 어느 계정인가" 를 별도 필드로 적어두지 않는 것이 이 설계의 핵심이다. 활성 계정은
언제나 `~/.codex/auth.json` 안의 email 로 판정한다. 사용자가 `codex login` 으로 직접
재로그인해도 표식과 실제가 어긋나지 않는다 — 어긋날 필드 자체가 없다.

## 진입점 이름

이 패키지는 `codex-swap` **하나만** 노출한다. 호환 이름 `codex-account` 를 함께 노출하면
`uv tool install` 이 dotfiles 가 소유한 `~/.local/bin/codex-account` 심링크와 충돌해 설치가
통째로 실패한다 — uv 는 자신이 관리하지 않는 실행 파일을 덮지 않고, 충돌을 먼저 검사한다.
그 이름은 마이그레이션이 끝날 때까지 dotfiles 가 계속 소유한다.

## 개발

```bash
uv sync
uv run pytest
uv run ruff check
```
