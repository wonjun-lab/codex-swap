# codex-swap

Codex CLI 계정을 여러 개 보관하고, 활성 계정의 사용량이 임계를 넘으면 자동으로 갈아끼운다.

원래 bash 스크립트로 쓰던 것을 Python 으로 다시 구현했다. 상태 레이아웃과 정책은 그대로
유지하고 구현만 옮겼다.

## 왜 옮겼는가

bash 판은 481 줄의 정책 엔진을 담고 있었는데, 그 핵심인 사다리 판정이 프로브·파일 I/O 와
엉켜 있어 격리 테스트가 되지 않았다. Python 판은 정책을 순수 함수로 떼어내 네트워크 없이
전 조합을 검증한다.

부수 효과로 외부 의존이 사라졌다. bash 판은 `jq` · `base64` · `node` 를 요구했다.
`node` 는 사용량 프로브가 JavaScript 로 쓰여 있었기 때문인데, 프로브가 하는 일은
`codex app-server` 를 자식으로 띄워 NDJSON JSON-RPC 를 주고받는 것뿐이라 표준 라이브러리로
그대로 대체된다. 지금은 런타임 의존이 없다 — Python 3.11 이상이면 된다.

## 설치

```bash
uv tool install git+https://github.com/wonjun-lab/codex-swap.git
```

## 명령

| 명령 | 하는 일 |
| --- | --- |
| `codex-swap` (인자 없이) | TUI. 목록·사용량 바·정책 편집이 한 화면에 있다 |
| `codex-swap adopt <label>` | 지금 로그인된 계정을 `<label>` 슬롯에 등록 |
| `codex-swap add <label>` | 새 슬롯에 로그인 (브라우저 인증) |
| `codex-swap list` | 등록된 계정과 캐시된 사용량 |
| `codex-swap status [--fresh]` | 활성 계정과 사용량 |
| `codex-swap use <label>` | 수동 전환 |
| `codex-swap rotate [--dry-run]` | 정책 실행 |
| `codex-swap remove <label>` | 슬롯 삭제 |
| `codex-swap clean` | 슬롯의 프로브 부산물 정리 |

## 자동 전환 붙이기

`rotate` 는 **스스로 돌지 않는다.** codex 를 부르기 직전에 이 명령을 부르는 것은 호출자
몫이다 — 보통 `codex` 를 감싸는 래퍼 함수나 셸 훅에 한 줄 넣는다.

```bash
codex() {
  command codex-swap rotate >/dev/null 2>&1
  command codex "$@"
}
```

`rotate` 는 이 자리를 위해 **출력 규율**을 지킨다. 평상시에는 stdout·stderr 모두에
아무것도 쓰지 않고, 전환이 실제로 일어났을 때만 stderr 에 한 줄 쓴다. 설정이 깨져도
조용히 무동작으로 끝난다(fail-open) — 스위처 때문에 codex 가 안 뜨는 일은 없어야 한다.

자동 전환 끄기: `touch ~/.claude/.codex-rotate-off` (파일이 있으면 `rotate` 가 무동작한다)

## 정책

사다리(기본 `50,70,85,95`)를 넘을 때만 전환을 검토한다. 현재 관문은 **가장 덜 쓴 계정**의
사용량 바로 위 칸이다 — 활성 계정 기준으로 잡으면 앞선 쪽만 계속 올라가 번갈아 밟기가
성립하지 않는다. 대상이 마진(기본 5%p) 이상 낮아야 실제로 바꾸고, 전환 사이에는 쿨다운
(기본 900s)을 둔다.

TUI 의 `p` 화면에서 고치거나 환경변수로 덮을 수 있다. 저장 위치는
`~/.codex/accounts/config.json` 이고, 환경변수가 파일을 이긴다.

## 상태

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

## 개발

```bash
uv sync
uv run pytest
uv run ruff check
```

설계 근거와 "여기서 틀리기 쉬운 지점" 은 [`docs/design/`](docs/design/) 에 있다.

## 라이선스

MIT. [`LICENSE`](LICENSE) 참조.
