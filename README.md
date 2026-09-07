# codex-swap

Codex CLI 계정을 여러 개 보관하고, 쓰던 계정의 사용량이 한도에 가까워지면 다음 계정으로
갈아끼운다. 계정을 바꾸려고 매번 로그아웃·로그인 하지 않아도 된다.

```
codex-swap    현재 관문 70% · 마진 5%p

   LABEL     EMAIL                     USED                            쿠폰  RESET
 >*work      account.name@gmail.com     58%  ██████████████▒▒▒┆▒▒▒▒▒▒     1  09-13 02:00 (5시간 뒤)
   personal  other.name@gmail.com       72%  █████████████████╪▒▒▒▒▒▒     0  09-14 07:00 (1일 뒤)
   spare     third.name@gmail.com         ?  ────────────────────────     -  -

                                                         ┴    ┻  ┴  ┴
                                                         50   70 85 95  ┻ = 현재 관문

  ^v 이동   enter 전환   r 사용량   a 등록   p 정책   o 자동전환   q 종료
  자동 전환: 켜짐   (o 로 끄기)
```

`*` 가 지금 쓰는 계정, `>` 가 커서다. 바의 `┆` 는 지금 넘어야 하는 관문이고 `╪` 는 이미
넘었다는 뜻이다. `쿠폰` 은 남은 사용량 리셋 크레딧 수 — 소진된 계정에 쿠폰이 남아 있으면
전환하는 대신 그것을 쓰는 선택지가 있다.

## 필요한 것

| | |
| --- | --- |
| Python | 3.11 이상. 런타임 의존 패키지는 없다 |
| [Codex CLI](https://github.com/openai/codex) | 이미 설치돼 있고 최소 한 계정으로 로그인된 상태 |
| [uv](https://docs.astral.sh/uv/) | 설치에 쓴다. 아래 대안도 있다 |

사용량은 `codex app-server` 를 자식 프로세스로 띄워 읽는다. 그래서 Codex CLI 가 실제로
실행 가능해야 한다 — `codex --version` 이 되는지 먼저 확인하라.

## 설치

```bash
uv tool install git+https://github.com/wonjun-lab/codex-swap.git
```

uv 를 쓰지 않으면 pip 로도 된다(가상환경 권장):

```bash
pip install git+https://github.com/wonjun-lab/codex-swap.git
```

## 처음 쓰기

계정 **둘 이상**이 등록돼야 자동 전환이 성립한다. 하나만 있으면 `rotate` 는
`only one account registered` 로 조용히 끝난다.

```bash
# 1. 지금 로그인된 계정을 슬롯으로 보관한다
codex-swap adopt work

# 2. 두 번째 계정을 등록한다 — 브라우저 인증이 뜬다.
#    지금 계정과 **다른** 계정으로 로그인하라.
codex-swap add personal

# 3. 확인
codex-swap list
```

`adopt` 은 지금 자격증명을 슬롯에 **복사**할 뿐이라 로그인 상태가 그대로 유지된다.
`add` 는 새 슬롯 홈으로 로그인시키므로 지금 계정을 잃지 않는다.

그다음 인자 없이 실행하면 TUI 가 뜬다.

```bash
codex-swap
```

여기서 `enter` 로 수동 전환, `r` 로 사용량 갱신, `p` 로 정책 편집을 한다.

## 자동 전환 붙이기

`rotate` 는 **스스로 돌지 않는다.** codex 를 부르기 직전에 이 명령을 부르는 것은 호출자
몫이다 — 보통 셸 함수로 감싼다.

<details>
<summary>bash · zsh</summary>

`~/.bashrc` 또는 `~/.zshrc` 에:

```bash
codex() {
  command codex-swap rotate >/dev/null   # stdout 만 버린다 (아래 참조)
  command codex "$@"
}
```
</details>

<details>
<summary>fish</summary>

`~/.config/fish/functions/codex.fish` 에:

```fish
function codex
    command codex-swap rotate >/dev/null
    command codex $argv
end
```
</details>

`rotate` 는 이 자리를 위해 **출력 규율**을 지킨다. 평상시에는 stdout·stderr 모두에
아무것도 쓰지 않고, 전환이 실제로 일어났을 때만 stderr 에 한 줄 쓴다. 그래서 `2>&1` 로
stderr 까지 버리면 전환 통지를 영영 못 본다. 설정이 깨져도 조용히 무동작으로 끝나므로
(fail-open) 스위처 때문에 codex 가 안 뜨는 일은 없다.

전환 직후에는 떠 있던 codex 세션이 아직 옛 토큰을 들고 있다. 새 계정이 적용되는 것은
다음 codex 실행부터다.

### 잠깐 끄기

```bash
mkdir -p ~/.claude && touch ~/.claude/.codex-rotate-off   # 끄기
rm ~/.claude/.codex-rotate-off                            # 켜기
```

TUI 의 `o` 키가 같은 파일을 토글한다. 한 번만 건너뛰려면 `CODEX_ROTATE_SKIP=1` 을 준다.

> 경로가 `~/.claude` 아래인 것은 선행 구현이 그 디렉토리를 상태 자리로 쓰던 흔적이다.
> 이미 이 이름으로 스위치를 둔 설치가 있어 옮기지 않았다.

## 왜 안 바뀌나

`rotate` 는 평상시 침묵하므로, 판단 이유를 보려면 `--dry-run` 을 쓴다. 아무것도 바꾸지
않고 결론만 출력한다.

```bash
$ codex-swap rotate --dry-run
no switch: active 41% below first rung 50%
```

자주 보게 되는 답:

| 출력 | 뜻 | 어떻게 |
| --- | --- | --- |
| `active N% below first rung M%` | 아직 한도에 여유가 있다 | 정상. 사다리 첫 칸을 낮추려면 `p` 화면 |
| `only one account registered` | 갈아끼울 상대가 없다 | `codex-swap add <label>` |
| `active account is not a registered slot` | 지금 계정이 어느 슬롯과도 안 맞는다 | `codex-swap adopt <label>` 로 보관 |
| `margin (N% + 5 > M%)` | 후보가 충분히 낮지 않다 | 마진을 낮추거나 기다린다 |
| `cooldown` | 방금 바꿨다 | 기본 15분 기다린다 |
| `throttled` | 판단 자체를 묶는 창 안이다 | 기본 60초 |
| `ladder exhausted` · `reached but no lighter account` | 전 계정이 다 소진됐다 | 리셋을 기다리거나 쿠폰을 쓴다 |
| `off switch (…)` · `CODEX_ROTATE_SKIP` | 꺼 놨다 | 위 "잠깐 끄기" 참조 |
| `recent job activity` | busy 관문 | 아래 환경변수 표 참조 |
| `CODEX_HOME points elsewhere` | 다른 홈을 지정한 채로 불렀다 | 의도된 보호다. `CODEX_HOME` 을 지우고 부른다 |

전환이 실제로 일어난 기록은 `~/.codex/accounts/rotate.log` 에 남는다 (토큰은 남기지 않는다).

## 사용량이 `?` 로 보일 때

| 표시 | 뜻 |
| --- | --- |
| `58%` | 신선한 값 |
| `~58%` | 캐시가 낡았다(기본 TTL 5분). 값은 맞지만 지금 값은 아닐 수 있다 |
| `?` | 한 번도 읽지 못했다 |

`?` 가 계속이면 이유를 이렇게 본다:

```bash
codex-swap status --fresh
```

`사용량: 조회 실패` 가 나오면 `codex app-server` 를 띄우지 못했거나 인증이 끊긴 것이다.
`codex --version` 과 `codex login status` 를 먼저 확인하라. 바이너리 탐색이 문제라면
`CODEX_ACCOUNT_BIN` 으로 경로를 직접 지정할 수 있다.

`list` 는 **네트워크를 타지 않는다**(캐시만 읽는다). 신선한 값이 필요하면 `status --fresh`
나 TUI 의 `r` 을 쓴다.

## 정책

사다리(기본 `50,70,85,95`)를 넘을 때만 전환을 검토한다. 현재 관문은 **가장 덜 쓴 계정**의
사용량 바로 위 칸이다 — 활성 계정 기준으로 잡으면 앞선 쪽만 계속 올라가 번갈아 밟기가
성립하지 않는다. 대상이 마진(기본 5%p) 이상 낮아야 실제로 바꾸고, 전환 사이에는 쿨다운
(기본 15분)을 둔다.

TUI 의 `p` 화면에서 고치거나 환경변수로 덮을 수 있다. 저장 위치는
`~/.codex/accounts/config.json` 이고 **환경변수가 파일을 이긴다.**

## 명령

| 명령 | 하는 일 |
| --- | --- |
| `codex-swap` (인자 없이) | TUI. 목록·사용량 바·정책 편집이 한 화면에 있다 |
| `codex-swap adopt <label>` | 지금 로그인된 계정을 `<label>` 슬롯에 등록 |
| `codex-swap add <label>` | 새 슬롯에 로그인 (브라우저 인증) |
| `codex-swap list` | 등록된 계정과 캐시된 사용량 (프로브 없음) |
| `codex-swap status [--fresh]` | 활성 계정과 사용량. `--fresh` 는 지금 조회한다 |
| `codex-swap use <label>` | 수동 전환 |
| `codex-swap rotate [--dry-run]` | 정책 실행 |
| `codex-swap remove <label>` | 슬롯 삭제 (활성 자격증명은 건드리지 않는다) |
| `codex-swap clean` | 슬롯의 프로브 부산물 정리 (`auth.json` 은 보존) |

`ls`·`switch`·`rm` 별칭이 있다.

## 환경변수

| 환경변수 | 기본값 | 뜻 |
| --- | --- | --- |
| `CODEX_ROTATE_LADDER` | `50,70,85,95` | 전환 관문 |
| `CODEX_ROTATE_MARGIN` | `5` | 대상이 이만큼(%p) 낮아야 바꾼다 |
| `CODEX_ROTATE_COOLDOWN` | `900` | 자동 전환 사이 최소 간격(초) |
| `CODEX_ROTATE_CACHE_TTL` | `300` | 사용량 캐시 수명(초) |
| `CODEX_ROTATE_CHECK_INTERVAL` | `60` | 판단 자체를 묶는 스로틀(초) |
| `CODEX_ROTATE_BUSY_WINDOW` | `180` | 아래 참조 |
| `CODEX_ROTATE_SKIP` | — | 값이 있으면 `rotate` 가 무동작 |
| `CODEX_ROTATE_STATE_ROOT` | (아래) | busy 판정이 볼 로그 디렉토리 |
| `CODEX_ACCOUNTS_DIR` | `~/.codex/accounts` | 슬롯 저장소 |
| `CODEX_ACCOUNT_DEFAULT_HOME` | `~/.codex` | 활성 계정의 홈 |
| `CODEX_ACCOUNT_BIN` · `CODEX_REAL_BIN` | — | codex 바이너리를 직접 지정(탐색 우회) |

**`busy` 관문은 기본적으로 꺼져 있는 것과 같다.** `CODEX_ROTATE_STATE_ROOT` 아래 `*.log`
의 mtime 이 최근이면 "지금 대화 중" 으로 보고 전환을 미루는 장치인데, 기본값이 특정
에이전트 런타임의 데이터 경로라 그 디렉토리가 없는 기기에서는 판정이 언제나 거짓이다.
쓰려면 자기 환경의 로그 디렉토리를 이 변수로 가리켜야 한다.

## 자격증명을 어떻게 다루나

이 도구가 다루는 것은 OAuth 토큰이므로, 무엇을 하고 무엇을 하지 않는지 밝혀 둔다.

- **바깥으로 나가는 네트워크 요청이 없다.** 이 패키지는 HTTP 클라이언트를 쓰지 않는다.
  사용량은 `codex app-server` 를 자식으로 띄워 표준입출력으로 JSON-RPC 를 주고받아 읽고,
  실제 API 호출은 Codex CLI 가 한다. 텔레메트리도 자동 업데이트도 없다.
- **토큰을 로그에 남기지 않는다.** `rotate.log` 에는 시각·라벨·사유만 적는다. 오류
  메시지에 토큰이 섞여 나가지 않는지 검증하는 테스트가 있다.
- **파일 권한.** 슬롯 디렉토리는 `0700`, `auth.json` 과 캐시는 `0600` 으로 만든다.
  생성 경로가 하나로 모여 있어 어느 명령으로 만들어도 같다.
- **전환은 원자적이다.** 락(mkdir)을 잡고 임시 파일에 쓴 뒤 rename 한다. 반쯤 쓰인
  `auth.json` 이 남지 않는다.
- **라벨은 경로가 된다.** `../` 나 `.` 로 시작하는 이름, 슬래시, 64자 초과를 거부한다.
  슬롯 안 `auth.json` 이 바깥을 가리키는 심링크면 통과시키지 않는다.

## 상태

```
~/.codex/auth.json                    활성 계정 (여기 담긴 email 이 곧 "지금 어느 계정인가")
~/.codex/accounts/<label>/auth.json   슬롯별 자격증명 (mode 600, 디렉토리 700)
~/.codex/accounts/config.json         정책 (TUI 의 `p` 가 여기 쓴다)
~/.codex/accounts/.usage-cache.json   라벨별 사용량 캐시 (TTL)
~/.codex/accounts/.last-rotate        마지막 자동 전환 시각 (쿨다운)
~/.codex/accounts/.last-check         마지막 판단 시각 (스로틀)
~/.codex/accounts/rotate.log          전환 원장 (토큰은 남기지 않는다)
```

"지금 어느 계정인가" 를 별도 필드로 적어두지 않는 것이 이 설계의 핵심이다. 활성 계정은
언제나 `~/.codex/auth.json` 안의 email 로 판정한다. `codex login` 으로 직접 재로그인해도
표식과 실제가 어긋나지 않는다 — 어긋날 필드 자체가 없다.

## 왜 만들었나

원래 bash 스크립트였다. 481 줄의 정책 엔진을 담고 있었는데, 그 핵심인 사다리 판정이
프로브·파일 I/O 와 엉켜 있어 격리 테스트가 되지 않았다. Python 판은 정책을 순수 함수로
떼어내 네트워크 없이 전 조합을 검증한다.

부수 효과로 외부 의존이 사라졌다. bash 판은 `jq` · `base64` · `node` 를 요구했다. `node`
는 사용량 프로브가 JavaScript 로 쓰여 있었기 때문인데, 프로브가 하는 일은 app-server 를
자식으로 띄워 NDJSON JSON-RPC 를 주고받는 것뿐이라 표준 라이브러리로 그대로 대체된다.

## 개발

```bash
uv sync
uv run pytest          # 534 건
uv run ruff check
uv run ruff format --check
```

설계 근거와 "여기서 틀리기 쉬운 지점" 은 [`docs/design/`](docs/design/) 에 있다 — 특히
계약(§5), 이관에서 틀리기 쉬운 지점(§6), 의도된 divergence(§7.5).

## 라이선스

MIT. [`LICENSE`](LICENSE) 참조.
