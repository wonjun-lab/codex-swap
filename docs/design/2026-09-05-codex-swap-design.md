# codex-swap 설계

작성 2026-09-05. 대상은 `~/dotfiles/codex/codex-account` (bash) 의 Python 재구현.

---

## 1. 무엇을 옮기는가

bash 판은 세 파일이다.

| 파일 | 크기 | 역할 |
| --- | --- | --- |
| `codex/codex-account` | 165 행 | CLI 진입점 |
| `codex/lib/codex-account.sh` | 508 행 | 정책 엔진 |
| `codex/lib/codex-rate-limits.mjs` | 133 행 | 사용량 프로브 (node) |

**상태 레이아웃과 정책은 옮기지 않는다. 구현만 옮긴다.** 파일 위치·이름·의미가 모두
동일하므로 두 판이 같은 상태를 공유하고, 그래서 나란히 두고 옮겨갈 수 있다.

## 2. 왜 옮기는가

### 2.1 정책이 테스트되지 않는다

사다리 판정이 프로브·파일 I/O 와 한 함수 안에 엉켜 있다. bash 테스트는 가짜 프로브
`.mjs` 를 `CODEX_ACCOUNT_LIB_DIR` 로 주입하고 슬롯마다 `.fakepct` 파일을 심어야
한 케이스를 돌린다. 판정 자체를 격리해 부를 방법이 없다.

Python 판은 `policy` 를 순수 함수로 뗀다 — `(활성 사용량, 후보들, 설정, 시계) → Decision`.
부수 효과가 없으므로 사다리·마진·쿨다운·busy 의 전 조합을 파일도 네트워크도 없이 돌린다.

### 2.2 외부 의존이 오히려 줄어든다

| bash 판 | Python 판 |
| --- | --- |
| `jq` (JSON 파싱 전반) | `json` |
| `base64` (JWT payload 디코드) | `base64` |
| `node` (프로브 `.mjs` 실행) | 없음 |
| `codex` 바이너리 | 동일 |

`node` 가 필요했던 이유는 프로브가 JavaScript 로 쓰여 있었기 때문이지, codex 를 띄우는 데
node 가 필요해서가 아니다. `.mjs` 가 하는 일은 `spawn(codexBin, ["app-server"])` 로 자식을
띄우고 NDJSON JSON-RPC 를 주고받는 것뿐이라, Python `subprocess` 가 그대로 대신한다.

> 단, **§6.2 의 PATH 보정은 반드시 함께 옮겨야 한다.** node 의존이 사라지는 것은 우리
> 코드에서지, 시스템에서가 아니다.

### 2.3 이식성 결함이 사라진다

| bash | 문제 | Python |
| --- | --- | --- |
| `find -newermt` | BSD 에 없다. 락 stale 판정이 macOS 에서 항상 참이 되어 정상 락도 회수한다 | `stat` 비교 |
| `find -mmin` 폴백 | `(window+59)/60` 정수 나눗셈이 61 초를 2 분으로 올림 | 초 단위 그대로 |
| `date -Iseconds` | BSD 와 출력이 다르다 | `datetime` |
| `sort -rV` | 버전 정렬. 단순 문자열 역정렬은 v9 를 v20 보다 앞에 둔다 | 튜플 비교 |

## 3. 확정된 결정

| 항목 | 결정 | 근거 |
| --- | --- | --- |
| repo | `wonjun-lab/codex-swap` (private) | |
| 명령어 | `codex-swap` | |
| v1 범위 | CLI 패리티 (로테이션 정책 포함) | 작업량과 리스크가 정책 엔진에 몰려 있다. TUI 는 그 위의 얇은 층이므로 v2 |
| 기능 범위 | 현행 패리티 + 이관 중 발견된 결함 수정 | YAGNI. 안 쓸 기능이 정책 복잡도를 키우지 않는다 |
| bash 처리 | 병행 유지 후 별도 PR 로 제거 | rotate 는 wrapper 안에서 도는 안전 임계 경로다 |
| 진입점 | `codex-swap` **하나만** | §7.1 |
| Python | `>=3.11`, 런타임 의존 없음 | `uv tool` 이 인터프리터를 provision 한다 |

## 4. 레이어

```
codex_swap/
  cli.py          argparse 어댑터 — 서식과 exit code 만
  core/
    config.py     환경변수 → Settings
    paths.py      상태 파일 경로
    discovery.py  upstream codex 해석 (codex-path.sh 포트)
    identity.py   auth.json → email
    store.py      슬롯 CRUD · 라벨 검증 · 원자적 교체 · 락
    probe.py      app-server JSON-RPC 클라이언트 → Usage
    cache.py      사용량 캐시 (TTL)
    policy.py     순수 함수 — 부수 효과 없음
    rotate.py     조립 + fail-open 경계
    log.py        전환 원장
```

경계의 핵심은 `policy.py` 가 아무것도 읽지도 쓰지도 않는다는 것이다. `rotate.py` 가
상태를 모아 `policy` 에 넘기고, 돌아온 `Decision` 을 `store` 에 집행시킨다.

## 5. 깨면 안 되는 계약

| # | 계약 | 깨지면 |
| --- | --- | --- |
| 1 | 활성 계정은 `~/.codex/auth.json` 의 email 로만 판정. 별도 상태 필드 없음 | 사용자가 직접 `codex login` 하면 표식과 실제가 어긋난다 |
| 2 | fail-open — 어떤 실패도 원래 호출을 막지 않는다 | wrapper 안에서 죽으면 `codex` 자체가 죽는다 |
| 3 | 프로브 exit 3(인증실패) ↔ 1(기타) 구분 | 전환이 가장 절실한 순간(토큰 사망)에 스위처가 손을 놓는다 |
| 4 | 슬롯 디렉토리를 프로브용 `CODEX_HOME` 으로 재사용 | 보관 토큰이 갱신되지 않아 썩는다 |
| 5 | 떠나기 전 현재 자격증명을 자기 슬롯에 되쓴다 | 돌아올 때 만료 토큰을 집는다 |
| 6 | `auth.json` 교체는 같은 파일시스템 내 temp+rename | 반쪽 쓰인 `auth.json` 이 생긴다 |
| 7 | 원장에 토큰을 남기지 않는다 | 자격증명 유출 |
| 8 | 디렉토리 0700 · 자격증명 0600 | 자격증명 노출 |
| 9 | 소진·인증실패면 사다리·마진·쿨다운·busy 를 **전부** 건너뛴다 | 양쪽에 여유가 남았는데 전부 막힌 채 끝난다 |
| 10 | 로그아웃 복구는 사다리 밖 경로이며 슬롯 1 개로도 성립한다 | `codex login` 이 중간에 끊기면 손으로 고칠 때까지 401 이 계속된다 |

## 6. 이관에서 틀리기 쉬운 지점

### 6.1 환경변수의 빈 문자열

bash `:=` 와 `:-` 는 **미설정뿐 아니라 빈 문자열도** 기본값으로 바꾼다. Python 의
`os.getenv(name, default)` 는 빈 문자열을 그대로 돌려준다.

| 입력 | bash | 순진한 Python |
| --- | --- | --- |
| `CODEX_ACCOUNTS_DIR=""` | 기본 경로 | `Path("")` → cwd |
| `CODEX_ROTATE_CHECK_INTERVAL=""` | `60` | `int("")` → 예외 |

`CODEX_ROTATE_SKIP` 은 불리언이 아니라 **비어 있지 않음**이다. `CODEX_ROTATE_SKIP=0` 은
bash 에서 회전을 **끈다**. 일반적인 불리언 파서는 이를 false 로 읽는다.

### 6.2 프로브의 PATH 보정 (검증됨)

이 기기에서 `codex_find_upstream` 이 돌려주는 경로는 네이티브 바이너리가 아니라
`#!/usr/bin/env node` 스크립트다.

```
$ file -L ~/.nvm/versions/node/v24.14.0/bin/codex
... a /usr/bin/env node script, ASCII text executable

$ 축소 PATH(/usr/bin:/bin)로 Popen([codex_bin, "app-server"])
returncode=127   stderr="/usr/bin/env: 'node': No such file or directory"
```

따라서 **탐색된 경로의 lexical parent 를 PATH 앞에 붙여야 한다.**
`Path(codex_bin).resolve().parent` 를 쓰면 안 된다 — 심링크를 따라가면
`node_modules/@openai/codex/bin` 으로 가고 거기에는 node 가 없다.

```
located_dir  = ~/.nvm/versions/node/v24.14.0/bin                       node 있음
resolved_dir = ~/.nvm/.../lib/node_modules/@openai/codex/bin           node 없음
```

`.mjs` 는 이 보정을 하지 않는다. 지금까지 문제가 없었던 것은 대화형 PATH 에 node 가
이미 있었기 때문이고, 축소 PATH 에서는 실제로 실패한다.

### 6.3 프로브 핸드셰이크 (검증됨)

`initialize` 는 **응답을 기다린 뒤에** `initialized` 를 보낸다. 프레이밍은 NDJSON 이며
`Content-Length` 헤더는 쓰지 않는다.

```
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"clientInfo":{...}}}\n
  ← id=1 응답 대기
{"jsonrpc":"2.0","method":"initialized","params":{}}\n
{"jsonrpc":"2.0","id":2,"method":"account/read","params":{}}\n
  ← id=2 응답 대기
{"jsonrpc":"2.0","id":3,"method":"account/rateLimits/read","params":{}}\n
```

주의: `initialize` 응답의 `error` 유무는 검사하지 않는다. id 만 맞으면 다음으로 간다.
타임아웃은 전역 20 초가 아니라 **요청당** 20 초다.

인증 실패(exit 3) 판정은 자식의 exit code 나 stderr 가 아니라 **JSON-RPC 오류 메시지**에
대한 정규식이다. 자식 stderr 는 버려진다.

```
/\b401\b|token_revoked|token_expired|invalid_grant|unauthorized|sign in again|logged out/i
```

Python 이 stderr 의 `401` 이나 exit 127 을 인증 실패로 분류하면, bash 가 1 을 돌려줄
상황에서 Python 만 계정을 갈아끼운다.

### 6.4 JSON 값의 수용 범위

bash 는 `jq -r` 로 문자열화한 뒤 숫자 정규식으로 판정한다. 그래서 숫자 `95` 와 문자열
`"95"` 를 **둘 다** 받고 float·음수·boolean 을 거부한다.

Python 에서 `isinstance(x, int)` 는 `bool` 이 `int` 의 하위 타입이라 `True` 를 통과시킨다.
`jq` 의 `// empty` 는 missing·null·**false** 를 접지만 `0` 은 `"0"` 으로 남긴다.
`reached: 0` 은 bash 에서 non-empty 라 소진으로 처리되지만 `if payload.get("reached")` 는
false 로 처리한다.

### 6.5 라벨 순서와 이메일 중복

후보 동률은 strict `<` 때문에 **최초 열거 라벨**이 이긴다. bash glob 순서와 Python
`iterdir()` 순서는 같지 않다. 같은 이메일을 여러 라벨로 등록하는 것을 막지 않으므로
`active_label` 이 최초 일치 라벨로 임의 결정되고, 자격증명 sync-back 대상이 달라진다.

### 6.6 라벨 검증 (2026-09-05 확정)

dotfiles PR #75·#76 에서 확정한 계약을 그대로 재현한다.

```
단일 경로 요소:  ^[A-Za-z0-9_][A-Za-z0-9._-]*$
길이:            <= 64
슬롯이 심링크:   거부
```

**사용자 인자와 디스크 열거 양쪽에 적용한다.** #75 는 인자만 막았는데, 열거 경로로
심링크 슬롯이 들어와 `use` 는 거부하는 것을 `rotate` 가 골랐다(실측: 루트 밖 자격증명으로
실제 전환). 개행이 든 디렉토리 이름은 라벨 하나를 둘로 갈랐다.

## 7. 병행 운용

### 7.1 진입점 이름은 하나뿐이다

`uv tool install` 은 자신이 관리하지 않는 실행 파일을 덮지 않고, 충돌을 설치 **전에**
검사한다. 따라서 호환 이름 `codex-account` 를 이 패키지가 노출하면 dotfiles 소유의
`~/.local/bin/codex-account` 심링크와 충돌해 `codex-swap` 까지 설치가 실패한다.
`--force` 는 심링크를 교체할 뿐이고, 다음 `install.sh` 가 되돌린다.

그래서 이 패키지는 `codex-swap` 만 노출한다. `codex-account` 이름은 이관이 끝날 때까지
dotfiles 가 소유한다.

### 7.2 런타임 병행 검증은 성립하지 않는다

wrapper 는 PATH 가 아니라 **자기 형제 파일**을 부른다.

```bash
# codex/codex.sh
CODEX_ACCOUNT_BIN="$real_codex" "$script_dir/codex-account" rotate
```

PATH 에 무엇을 깔든 wrapper 의 핫패스는 bash 그대로다. 훅만 PATH 를 본다. 게다가
`rotate` 의 exit code 는 throttle·cooldown·busy·네트워크 실패·파싱 실패를 **전부 1 로
접기 때문에**, 두 구현이 똑같이 1 을 돌려줘도 동치인지 한쪽이 죽은 건지 알 수 없다.

### 7.3 대신 스냅샷 차등 테스트

상태·사용량·설정·시계를 **한 번** 캡처해 두 정책 엔진에 같은 입력을 넣고, 구조화된
`Decision` 을 비교한다. 실제 계정은 건드리지 않고, 결정적이며, 재현 가능하다.

```
snapshot = {슬롯 목록, 각 라벨의 usage, 활성 email, 설정값, now, 스탬프 mtime}
   ├── bash   정책 → Decision
   └── python 정책 → Decision
                 └── 비교
```

집행(mutator)은 언제나 하나만 돈다.

### 7.4 내부 결과는 구조화한다

공개 exit code 는 0/1 을 유지하되, 내부적으로는 다음을 구분한다.

```
Switched(from, to, reason)
NoOp(reason)            판단했고 바꾸지 않기로 했다
Indeterminate(reason)   상태를 모른다 (네트워크·파싱 실패)
Error(reason)           우리 잘못
```

`NoOp` 과 `Indeterminate` 를 가르지 않으면 차등 비교가 무의미해진다.

## 8. 바이너리 해석

| 호출 경로 | `CODEX_ACCOUNT_BIN` | discovery 필요 |
| --- | --- | --- |
| wrapper (`codex.sh`) | 넘겨준다 | 불필요 |
| Claude 훅 (`SessionStart`) | 안 넘긴다 | **필요** |
| 사람이 터미널에서 | 안 넘긴다 | **필요** |

따라서 `discovery.py` 는 보조가 아니라 1 급 컴포넌트다. `codex-path.sh` 의 탐색 순서를
그대로 옮긴다 — `CODEX_REAL_BIN` → `~/.local/bin` 을 뺀 PATH → standalone 설치 경로 →
nvm 버전 역정렬. wrapper 재귀 가드(shebang 검사 후 8KB marker grep)도 함께 옮긴다.

## 9. 성능

| 경로 | bash | Python (`uv tool` 진입점) |
| --- | --- | --- |
| throttle 히트 (평상시) | ~0.01 s | ~0.08 s |

이 비용은 `codex` 프로세스 스폰마다, 그리고 Claude 세션 시작마다 실린다. 프롬프트마다가
아니다 — 훅은 `SessionStart` 에 등록돼 있다. 70 ms 증가는 감당 가능하며, 핫패스 때문에
bash 를 남길 이유는 없다.

## 10. 범위 밖

- Textual TUI (v2)
- cswap 의 편의 기능 — disable/enable, run-as, 디렉토리 매핑, alias, 슬롯 번호
- 락 프로토콜 교체 — 병행 기간에는 bash 와 **같은** mkdir 락을 써야 하므로 `fcntl` 이나
  owner 파일을 넣지 않는다
