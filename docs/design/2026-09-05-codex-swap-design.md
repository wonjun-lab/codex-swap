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
| `date -Iseconds` | BSD 와 출력이 다르다 | `datetime` |
| `sort -rV` | 버전 정렬. 단순 문자열 역정렬은 v9 를 v20 보다 앞에 둔다 | 튜플 비교 |

### 2.3.1 busy 창은 이식성 문제가 아니라 패리티 선택이다

처음에 `find -mmin` 폴백을 "BSD 전용 결함" 으로 적었는데 **틀렸다.** 폴백은 첫
`-newermt` 결과가 비면 **무조건** 실행되고 GNU find 도 `-mmin` 을 지원한다. 그래서
Linux 에서도 실효 busy 창은 언제나 둘 중 거친 쪽 — `ceil(window/60)*60` 초다.

초 단위로 구현하면 `window=100` · 나이 110 초에서 bash 는 busy(전환 차단), Python 은
not-busy(전환)로 갈려 §7.3 차등이 이 축에서 **상시** 불일치한다. 그래서 선택해야 한다.

**결정: 패리티를 택한다.** `effective_window = ceil(window / 60) * 60`. 차등 테스트가
살아 있는 동안 이 축에서 잡음이 나면 안 되기 때문이다. bash 를 제거하는 PR 에서
초 단위로 좁히고, 그때 이 문단을 지운다.

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
| 2 | **출력 규율** — rotate 경로는 stdout 을 비우고, stderr 에는 의도된 한 줄 통지만 쓴다 | §5.1 |
| 3 | 프로브 exit 3(인증실패) ↔ 1(기타) 구분 | 전환이 가장 절실한 순간(토큰 사망)에 스위처가 손을 놓는다 |
| 4 | 슬롯 디렉토리를 프로브용 `CODEX_HOME` 으로 재사용 | 보관 토큰이 갱신되지 않아 썩는다 |
| 5 | 떠나기 전 현재 자격증명을 자기 슬롯에 되쓴다 | 돌아올 때 만료 토큰을 집는다 |
| 6 | `auth.json` 교체는 같은 파일시스템 내 temp+rename | 반쪽 쓰인 `auth.json` 이 생긴다 |
| 7 | 원장에 토큰을 남기지 않는다 | 자격증명 유출 |
| 8 | 디렉토리 0700 · 자격증명 0600 | 자격증명 노출 |
| 9 | 소진·인증실패면 사다리·마진·쿨다운·busy 를 **전부** 건너뛴다 | 양쪽에 여유가 남았는데 전부 막힌 채 끝난다 |
| 10 | 로그아웃 복구는 사다리 밖 경로이며 슬롯 1 개로도 성립한다 | `codex login` 이 중간에 끊기면 손으로 고칠 때까지 401 이 계속된다 |
| 11 | **`.last-check` 스탬프는 판단 *전에* 찍는다** | §5.2 |
| 12 | ~~`auth.json` 의 mtime 을 보존한다~~ → **활성 자리의 mtime 은 전환 시각으로 찍는다** | §5.3 |
| 13 | **`CODEX_HOME` 가드는 물리 경로 비교** | 문자열 비교하면 `/home/x/.codex/` 처럼 슬래시 하나로 가드가 뚫린다 |

### 5.1 계약 2 — 처음 적은 근거가 틀렸다

초안에 "wrapper 안에서 죽으면 codex 자체가 죽는다" 고 적었다. **HEAD 에서 거짓이다.**
`codex.sh` 는 라이브러리를 source 하지 않고 CLI 를 **자식 프로세스로** 띄운 뒤
`|| true` 로 결과를 버린다. 우리가 죽어도 codex 는 산다.

진짜 이유는 다른 데 있다. wrapper 는 `rotate > /dev/null` 로 **stdout 만** 버리고
**stderr 는 사용자 터미널로 그대로 흘린다.** 그래서 stderr 는 매 codex 호출에 노출되는
채널이고, 자격증명을 다루는 코드가 여기에 트레이스백을 뱉으면 토큰이 화면에 실린다.

따라서 계약은 이렇게 다시 쓴다 — rotate 진입점은 **어떤 예외도 밖으로 내보내지 않고**
0/1 로만 끝나며, stderr 에는 의도된 두 줄(로그아웃 복구 통지, 전환 통지)만 쓴다.

### 5.2 계약 11 — 스탬프는 성공 경로가 아니라 진입 직후

`.last-check` 는 `CODEX_HOME` 가드 직후, **로그아웃 복구보다도 먼저** 찍힌다. 판단 결과와
무관하다. 이것을 성공 경로로 옮기면 실패가 반복될 때 스로틀이 걸리지 않아 매 codex
호출이 프로브를 돈다. `--dry-run` 은 읽기와 쓰기를 **둘 다** 건너뛴다.

### 5.3 계약 12 — 뒤집혔다 (2026-09-06)

**초안은 틀렸다.** "`cp -p` 가 mtime 을 보존하니 우리도 보존한다" 고 적으면서 bash 충실성만
근거로 들었고, **보존이 옳은지를 묻지 않았다.** 그래서 버그를 계약으로 승격시켰다.

훅은 `broker 시작 < auth.json mtime` 으로 낡은 broker 를 판정한다. 그런데 전환의 원본은
슬롯에 보관된 며칠 전 사본이므로, mtime 을 보존하면 전환 직후에도 그 값이 과거로 남아
**조건이 사실상 항상 거짓**이 된다. 실측(2026-09-06): 그날 다섯 번 전환했는데
`~/.codex/auth.json` mtime 은 이틀 전이었고 떠 있던 broker 넷이 전부 "안 낡음" 으로
판정됐다. 훅 주석은 이 비교가 "옛 토큰을 든 broker 를 같은 실행에서 내린다" 고 적고
있지만 작동한 적이 없다.

무해하지도 않다. codex 0.153.0 은 auth 재읽기를 갖고 있지만 **계정 ID 가 다르면 새 인증을
채택하지 않고 오류를 반환한다** — 낡은 broker 는 조용히 넘어가는 것이 아니라 실패하거나
옛 계정을 계속 쓴다.

**고쳐진 계약**: 전환의 두 효과가 mtime 을 다르게 다룬다.

| 효과 | 방향 | mtime |
| --- | --- | --- |
| (A) sync-back | 활성 → 떠나는 슬롯 | 보존. 슬롯 mtime 을 판정에 쓰는 코드가 없음을 확인했다 |
| (B) install | 대상 슬롯 → 활성 | **전환 시각으로 찍는다.** 이 값의 뜻은 "활성 자격증명이 마지막으로 바뀐 시각" 이다 |

(B)는 **초 단위로 올림**한다. 훅은 `stat %Y` 와 `date +%s` 로 정수 초를 견주므로, 같은 초
안에서 broker 가 먼저 뜨고 전환이 뒤따르면(broker 1000.1, 전환 1000.9) 정수로는
`1000 < 1000` 이라 그 broker 를 놓친다. 올림하면 잡힌다. 대가는 그 1 초 안에 뒤에 뜬
broker 를 불필요하게 죽일 수 있다는 것인데, 그 비용은 재시작 한 번이고 놓치는 비용은
옛 계정으로 계속 요청하는 것이다.

bash 도 같은 결함이지만 고치지 않는다 — 곧 지울 코드이고 활성 경로는 Python 이다.

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

숫자 노브는 bash 가 **파싱하지 않는다** — `(( ))` 안에 그대로 보간한다. 그래서 값이
숫자가 아니면 bash 는 그것을 **변수 이름으로** 해석하고, `set -u` 아래서는 unbound
variable 로 프로세스가 죽는다. 어느 노브가 먼저 터지는지는 어떤 가드가 먼저 단락되는지에
달려 있고, 일부는 명령 치환 안에서 죽어 **서브셸만** 죽고 rotate 는 0 으로 전환까지 한다.

이 동작을 흉내내지 않는다. `config.py` 에 파서 하나를 두고, 미설정·빈 문자열은 기본값,
그 밖에 숫자가 아니면 예외를 올려 `rotate.py` 의 fail-open 경계에서 "전환 안 함" 으로
접는다. 대신 §7.3 차등에서 이 축은 **제외**한다 — bash 쪽 결과가 {전환됨, 조용한 rc=1,
중단된 rc=1, rc=0 인데 stderr 에 진단} 으로 갈려 비교 대상이 되지 않는다.

### 6.1.1 `~/.codex/.env` 가 실제 설정 채널이다

wrapper 가 rotate 를 부르기 전에 `set -a`(자동 export)로 이 파일을 source 한다. 이 기기에
실제로 존재한다(mode 600). 즉 사다리·마진·SKIP 을 여기서 설정할 수 있다.

경로마다 보이는 설정이 다르다.

| 호출 경로 | `.env` 를 보나 |
| --- | --- |
| 터미널에서 `codex` — `~/.local/bin/codex` 가 `codex.sh` 심링크다 | **본다** |
| wrapper 가 부르는 rotate | **본다** |
| 터미널에서 `codex-account` 직접 | 안 본다 |
| Claude 훅 (`SessionStart`) | 안 본다 |

그래서 `.env` 의 `CODEX_ROTATE_SKIP=1` 은 codex 경유 회전은 막지만 훅의 회전은 못 막는다.

`config.py` 는 `.env` 를 읽지 **않는다.** wrapper 가 살아 있는 동안 변수는 이미 export 되어
도착하고, 여기서 읽으면 오늘 `.env` 를 보지 못하는 두 경로(훅·직접 호출)에서 **새로**
유효해진다 — 그건 이관이 아니라 동작 변경이다. wrapper 를 걷어내는 PR 에서 결정한다.

> 부수 효과 하나: `codex.sh` 는 `set -euo pipefail` 이라 `.env` 의 마지막 명령이 non-zero 면
> wrapper 가 거기서 죽는다. 즉 `.env` 는 fail-**closed** 다 — 라이브러리의 fail-open 이
> 시작되기도 전이다.

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

bash 는 `jq -r` 로 문자열화한 뒤 `^[0-9]+$` 로 판정한다. 초안에 "float·음수·boolean 을
거부한다" 고 적었는데 **float 부분이 틀렸다.** jq 는 정수값 double 을 소수점 없이
렌더링하므로 `usedPercent: 4.0` 은 `"4"` 가 되어 **통과한다.** 문자열 `"95"` 도 통과한다.
실제로 거부되는 것은 비정수 실수·음수·boolean 이다.

수용 판정을 하나로 만든다.

```python
def accepts_pct(v):
    if isinstance(v, bool):  # bool 이 int 하위타입이라 반드시 먼저 거른다
        return None
    if isinstance(v, int):
        return v if v >= 0 else None
    if isinstance(v, float):
        return int(v) if v.is_integer() and v >= 0 else None
    if isinstance(v, str):
        return int(v) if v.isdigit() else None
    return None
```

`re.fullmatch(r'[0-9]+', str(v))` 로 쓰면 안 된다 — `str(4.0)` 은 `'4.0'` 이라 bash 가
통과시키는 값에서 실패한다.

`reached` 는 다른 술어다. `jq` 의 `// empty` 는 missing·null·**false** 를 접지만 `0` 은
`"0"` 으로 남긴다. 그래서 `reached: 0` 은 bash 에서 non-empty → **소진으로 처리**된다.
`if payload.get("reached")` 는 false 로, `is not None` 은 `reached: false` 를 참으로
처리한다. 둘 다 틀린다.

프로브의 `error` 게이트도 키 존재가 아니라 **JS 진리값**이다. `error: null`·`false`·`0`·`""`
는 전부 성공 경로로 흐르고, `error: {}` 와 `error: []` 는 오류로 잡힌다. `if 'error' in msg`
는 네 경우를 뒤집는다.

### 6.4.1 캐시는 3-상태이고 음성 캐싱이 없다

`codex_account_usage` 는 0 / 1 / 3 을 돌려주는데, rc=3(인증실패) 반환이 캐시 쓰기보다
**앞에** 있다. 그래서 인증 실패는 **절대 캐시되지 않는다.** 마찬가지로 캐시 HIT 는 구조상
rc=3 을 낼 수 없다 — 히트 경로가 프로브를 띄우기 전에 반환한다.

따라서 **죽은 토큰이 최대 한 TTL(기본 300 초) 동안 가려진다.** 이건 결함이 아니라 정해진
지연 예산이다. 히트할 때 재검증하거나 결과 variant 를 캐시에 함께 넣어 "개선" 하면 안 된다.

프로브 결과는 불리언이 아니라 3-variant 타입으로 모델링한다.

```
Ok(Usage) | AuthFailed | Unknown
```

`cache.py` 는 `Ok` 만 저장한다. `rotate.py` 는 이 enum 만 보고 분기하며 원시 exit code 를
보지 않는다.

### 6.5 라벨 순서와 이메일 중복

후보 동률은 strict `<` 때문에 **최초 열거 라벨**이 이긴다. bash glob 순서와 Python
`iterdir()` 순서는 같지 않다. 같은 이메일을 여러 라벨로 등록하는 것을 막지 않으므로
`active_label` 이 최초 일치 라벨로 임의 결정되고, 자격증명 sync-back 대상이 달라진다.

### 6.6 라벨 검증 (2026-09-05 확정)

dotfiles PR #75·#76 에서 확정한 계약을 그대로 재현한다.

```
단일 경로 요소:      ^[A-Za-z0-9_][A-Za-z0-9._-]*$
길이:                <= 64
슬롯이 심링크:       거부
슬롯 안 auth.json:   realpath 가 루트 밖이면 거부   ← bash 에 아직 없다
```

**사용자 인자와 디스크 열거 양쪽에 적용한다.** #75 는 인자만 막았는데, 열거 경로로
심링크 슬롯이 들어와 `use` 는 거부하는 것을 `rotate` 가 골랐다(실측: 루트 밖 자격증명으로
실제 전환). 개행이 든 디렉토리 이름은 라벨 하나를 둘로 갈랐다.

네 번째 줄은 bash 에 아직 없는 항목이다. 현재 심링크 가드는 `$root/$label` 만 보므로,
**진짜 디렉토리**인 슬롯 안의 `auth.json` 이 바깥을 가리키는 심링크면 통과한다. Python
판은 슬롯을 연 뒤 `os.path.realpath(root/label/'auth.json')` 이 루트 안인지 확인한다.

> 이건 의도된 divergence다. bash 에 백포트할지는 §7.5 에서 정한다.

### 6.7 라벨 검증은 로케일에 걸려 있다

bash 의 `[[ $label =~ ^[A-Za-z0-9_][A-Za-z0-9._-]*$ ]]` 는 glibc 로케일 정렬을 쓴다.
`LC_ALL=C` 가 아닌 보통의 UTF-8 로케일에서 `A-Za-z` 범위가 ASCII 밖 문자까지 포함할 수
있어, 한글이 든 라벨이 **통과**할 수 있다. `LC_ALL=C` 에서는 거부된다.

Python 의 `re` 는 이 문자 클래스를 항상 ASCII 로만 해석한다. 따라서 ASCII 전용 포트는
**bash 의 `LC_ALL=C` 가지를 영구화**한다. 이미 비-ASCII 라벨로 등록된 슬롯이 있으면
목록에서 사라지고 자동 전환 후보에서 빠진다 — 자격증명이 조용히 접근 불가가 된다.

**결정: ASCII 전용을 유지하되, 설치 전 preflight 를 제공한다.** `codex-swap doctor` 가
검증을 통과하지 못하는 기존 슬롯을 읽기 전용으로 보고한다. 현재 이 기기의 라벨은
`master`·`shared` 라 해당 없다.

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

### 7.5 의도된 divergence — bash 의 결함은 옮기지 않는다

차등 테스트가 이것들을 불일치로 잡을 것이므로, **미리 이름을 붙여 예외 목록에 둔다.**
이름 없는 divergence 는 버그와 구별되지 않는다.

| # | bash 의 동작 | Python | 왜 안 옮기나 |
| --- | --- | --- | --- |
| D1 | `switch` 의 `trap … RETURN` 은 함수 스코프가 아니라 호출자 반환 때 **다시 발화**한다 | try/finally 로 **정확히 한 번** 해제 | 남의 락을 지우는 경로다 |
| D2 | `.lock` 이 디렉토리가 **아니면**(0바이트 파일·깨진 심링크) stale 판정 자체가 안 돌아 **영구 교착**. 복구 경로가 코드에 없다 | 디렉토리 아님을 3번째 갈래로 잡아 `Error(reason)` 로 보고 | 사람이 손으로 지울 때까지 자동 전환이 죽는다 |
| D3 | `status` 는 활성이 어느 슬롯에도 없으면 가짜 라벨 `__active__` 를 그대로 경로에 넣어 `CODEX_HOME=<root>/__active__` 로 프로브를 돌린다 | `label: str \| None` 로 시그니처를 나누고, `None` 이면 home 을 `~/.codex` 로 | 자격증명은 실제로 거기 있다. 파생 경로가 결함이다 |
| D4 | 슬롯 안 `auth.json` 이 바깥을 가리키는 심링크면 통과 (§6.6) | realpath 봉쇄 | #75·#76 이 닫은 구멍과 같은 종류다 |
| D5 | `codex_account_cache_write` 는 루트를 만들 때 `chmod 700` 을 하지 않는다 (`ensure_root` 만 한다). rotate 가 먼저 돌면 루트가 umask 모드로 생긴다 | `paths.ensure_root()` 하나를 모든 생성 경로가 지나게 한다 | 자격증명 디렉토리가 0755 로 생길 수 있다 |

반대로 **옮겨야 하는** 것도 하나 못박아 둔다. 전환 실패는 롤백되지 않는다 — sync-back 이
install 보다 먼저 돌고 되돌려지지 않으므로, install 이 실패해도 떠나려던 슬롯은 **이미
갱신돼 있다.** 이건 결함이 아니라 올바른 동작이다(그 바이트가 최신 토큰이다). prepare/commit
으로 감싸서 롤백하면 오히려 토큰을 잃는다. 두 효과는 **독립적으로 커밋**된다.

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

이 비용은 **`login`·`logout`·`mcp-server` 를 제외한** `codex` 프로세스 스폰마다, 그리고
Claude 세션 시작마다 실린다. 프롬프트마다가 아니다 — 훅은 `SessionStart` 에 등록돼 있다.
70 ms 증가는 감당 가능하며, 핫패스 때문에 bash 를 남길 이유는 없다.

wrapper 의 제외 목록은 spec 이 기술하는 **모든** 게이트보다 앞선 첫 관문이다. 그리고
그 분기에는 `-x "$script_dir/codex-account"` 검사가 함께 걸려 있어, 실행 권한이 빠지면
자동 전환이 **조용히** 꺼진다. `install.sh` 가 링크와 `chmod +x` 를 함께 거는 이유다.

## 10. 범위 밖

- Textual TUI (v2)
- cswap 의 편의 기능 — disable/enable, run-as, 디렉토리 매핑, alias, 슬롯 번호
- 락 프로토콜 교체 — 병행 기간에는 bash 와 **같은** mkdir 락을 써야 하므로 `fcntl` 이나
  owner 파일을 넣지 않는다
