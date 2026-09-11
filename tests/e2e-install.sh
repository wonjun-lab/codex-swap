#!/usr/bin/env bash
# **처음 설치해서 codex 를 쳐 보는 것**까지를 매번 확인한다.
#
# 단위 테스트 1180건이 전부 통과하는 상태에서 실기기가 이렇게 죽었다.
#
#   Error finding codex home: CODEX_HOME points to "~/.codex-cli", but that path does not exist
#   Switch failed: [Errno 2] No such file or directory: '~/.codex-cli/auth.json.tmp.59753'
#
# 공식 앱에게 `~/.codex` 를 비켜 주는 순간 활성 홈은 codex 가 한 번도 본 적 없는 경로가 되는데,
# 그 디렉토리를 만드는 코드가 없었다. 가짜 홈으로 재는 테스트는 미리 만들어 놓고 시작하므로
# 아무도 그 구멍을 밟지 않는다. 그래서 여기서는 **진짜 설치본과 진짜 codex** 로,
# 아무것도 없는 빈 HOME 에서 시작한다.
#
# 실제 홈·실제 설치본·실제 계정은 건드리지 않는다. 토큰은 전부 가짜이고, codex 는
# `login status` 만 부른다 — wrapper 가 그 서브커맨드 앞에서는 전환을 부르지 않으므로
# 네트워크도 프로브도 타지 않는다.
#
#   bash tests/e2e-install.sh [설치 소스]     기본값: 이 저장소
set -uo pipefail

SRC="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
REAL_HOME="$HOME"
pass=0
fail=0
ok() { printf '  \033[32mok\033[0m   %s\n' "$1"; pass=$((pass + 1)); }
no() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; fail=$((fail + 1)); }

# macOS 에는 `timeout` 이 없다. 없으면 시간 제한 없이 그냥 부른다 — 하네스가 127 로 죽어
# **검사 대상이 아니라 러너가** 실패하는 것을 막는다.
tmo() {
  local secs="$1"
  shift
  if command -v timeout > /dev/null 2>&1; then
    timeout "$secs" "$@"
  elif command -v gtimeout > /dev/null 2>&1; then
    gtimeout "$secs" "$@"
  else
    "$@"
  fi
}

# 진짜 codex 를 찾는다. `~/.local/bin` 과 wrapper(codex-swap·dotfiles)는 건너뛴다 —
# wrapper 를 upstream 으로 집으면 `exec` 가 자기 자신을 다시 띄운다.
real_codex=""
while read -r c; do
  [ -n "$c" ] || continue
  case "$c" in "$REAL_HOME"/.local/bin/*) continue ;; esac
  if head -c 2 "$c" 2> /dev/null | grep -q '#!' && grep -qE 'codex-swap|dotfiles' "$c" 2> /dev/null; then
    continue
  fi
  real_codex="$c"
  break
done < <(which -a codex 2> /dev/null)
if [ -z "$real_codex" ]; then
  echo "upstream codex 가 PATH 에 없다 — npm install -g @openai/codex" >&2
  exit 2
fi
uv_bin="$(command -v uv)" || { echo "uv 가 없다" >&2; exit 2; }
echo "source : $SRC"
echo "codex  : $real_codex ($("$real_codex" --version 2>&1 | head -1))"

ROOT="$(mktemp -d)" && [ -d "$ROOT" ] || { echo "mktemp 실패 — 격리 자리를 못 만들었다" >&2; exit 2; }
trap 'rm -rf "$ROOT"' EXIT

H=""
sandbox() { # $1=이름 → 아무것도 없는 새 HOME
  H="$ROOT/$1"
  mkdir -p "$H"
}
run() { # 격리 HOME·격리 PATH·격리 uv 로 실행. 호출자의 CODEX_* 는 하나도 넘기지 않는다.
  # `tmo` 는 셸 함수라 `env -i` 가 실행할 수 없다 — 시간 제한이 바깥에 와야 한다.
  tmo 300 env -i HOME="$H" USER="${USER:-runner}" LANG="${LANG:-C.UTF-8}" TERM=dumb SHELL=/bin/bash \
    PATH="$H/tool-bin:$H/.local/bin:$(dirname "$real_codex"):$(dirname "$uv_bin"):/usr/local/bin:/usr/bin:/bin" \
    UV_TOOL_DIR="$H/tool" UV_TOOL_BIN_DIR="$H/tool-bin" \
    UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$REAL_HOME/.local/share/uv/python}" \
    UV_CACHE_DIR="${UV_CACHE_DIR:-$REAL_HOME/.cache/uv}" \
    "$@"
}

# 가짜 자격증명. 네트워크를 타지 않으므로 토큰 값은 아무 글자나 된다 — 다른 것은 **이메일**뿐이고,
# 그것으로 어느 슬롯이 활성 자리에 떨어졌는지 가린다.
make_auth() { # $1=path $2=email $3=refresh
  mkdir -p "$(dirname "$1")"
  chmod 700 "$(dirname "$1")"
  python3 - "$1" "$2" "$3" <<'PY'
import base64, json, os, sys
path, email, refresh = sys.argv[1:4]
claims = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
body = {
    "OPENAI_API_KEY": None,
    "auth_mode": "chatgpt",
    "tokens": {
        "id_token": f"h.{claims}.s",
        "access_token": "AT-fake",
        "refresh_token": refresh,
        "account_id": "acc",
    },
}
with open(path, "w") as fh:
    json.dump(body, fh)
os.chmod(path, 0o600)
PY
}
email_of() { # $1=auth.json → email (없으면 빈 문자열)
  [ -f "$1" ] || return 0
  python3 - "$1" <<'PY'
import base64, json, sys
try:
    raw = json.load(open(sys.argv[1]))["tokens"]["id_token"].split(".")[1]
    print(json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))["email"])
except Exception:
    pass
PY
}

install_swap() { # $1=시나리오
  if run uv tool install --force "$SRC" > "$H/install.log" 2>&1; then
    ok "$1: codex-swap 설치"
  else
    no "$1: 설치 실패 — $(tail -2 "$H/install.log" | tr '\n' ' ')"
    return 1
  fi
}
init_places_wrapper() { # $1=시나리오
  run codex-swap init < /dev/null > "$H/init.log" 2>&1
  if [ -x "$H/.local/bin/codex" ] && grep -q "codex-swap exec" "$H/.local/bin/codex"; then
    ok "$1: init 이 ~/.local/bin/codex wrapper 를 놓는다"
  else
    no "$1: wrapper 가 없다 — $(tail -3 "$H/init.log" | tr '\n' ' ')"
  fi
}
home_is() { # $1=시나리오 $2=기대 홈
  local got
  got="$(run codex-swap home 2>&1)"
  [ "$got" = "$2" ] && ok "$1: home = ${2#"$H"/}" || no "$1: home = $got (기대 $2)"
  [ -d "$2" ] && ok "$1: 그 홈 디렉토리가 실제로 있다" || no "$1: 그 홈 디렉토리가 없다 ($2)"
}
codex_starts() { # $1=시나리오 — wrapper 를 거친 진짜 codex 가 뜨는가
  local out
  out="$(cd "$H" && run codex login status < /dev/null 2>&1)"
  if printf '%s' "$out" | grep -qE "CODEX_HOME points to|Error finding codex home"; then
    no "$1: codex 가 뜨지 않는다 — $(printf '%s' "$out" | grep -m1 -E 'Error|CODEX_HOME')"
  else
    ok "$1: wrapper 를 거친 codex 가 뜬다 ($(printf '%s' "$out" | tail -1))"
  fi
}
seed_slots() { # 두 계정을 등록해 둔 상태를 만든다 (로그인은 사람이 하는 일이라 파일로 대신한다)
  make_auth "$H/.codex/accounts/work/auth.json" work@example.com rt-work
  make_auth "$H/.codex/accounts/personal/auth.json" personal@example.com rt-personal
}
use_lands() { # $1=시나리오 $2=라벨 $3=기대 email $4=활성 auth 경로
  if run codex-swap use "$2" < /dev/null > "$H/use-$2.log" 2>&1 && [ "$(email_of "$4")" = "$3" ]; then
    ok "$1: use $2 → ${4#"$H"/} 가 $3"
  else
    no "$1: use $2 실패 — $(tail -2 "$H/use-$2.log" | tr '\n' ' ')"
  fi
}

printf '\n== S1 앱 없는 새 기기 ==\n'
sandbox s1
if install_swap S1; then
  init_places_wrapper S1
  home_is S1 "$H/.codex"
  codex_starts S1
  seed_slots
  use_lands S1 work work@example.com "$H/.codex/auth.json"
  codex_starts S1
fi

printf '\n== S2 앱이 먼저 깔린 새 기기 ==\n'
sandbox s2
mkdir -p "$H/Applications/ChatGPT.app"
if install_swap S2; then
  init_places_wrapper S2
  home_is S2 "$H/.codex-cli"
  codex_starts S2
  seed_slots
  use_lands S2 work work@example.com "$H/.codex-cli/auth.json"
  [ ! -e "$H/.codex/auth.json" ] && ok "S2: 앱의 ~/.codex/auth.json 은 만들지 않는다" \
    || no "S2: 앱 자리에 auth.json 을 썼다"
  codex_starts S2
fi

printf '\n== S3 쓰던 기기에 앱을 나중에 설치 ==\n'
sandbox s3
if install_swap S3; then
  init_places_wrapper S3
  seed_slots
  use_lands S3 work work@example.com "$H/.codex/auth.json"
  mkdir -p "$H/Applications/ChatGPT.app"
  home_is S3 "$H/.codex-cli"
  codex_starts S3
  use_lands S3 personal personal@example.com "$H/.codex-cli/auth.json"
  [ "$(email_of "$H/.codex/auth.json")" = work@example.com ] \
    && ok "S3: 앱 쪽 ~/.codex/auth.json 은 그대로 둔다" || no "S3: 앱 쪽 auth.json 이 바뀌었다"

  printf '\n== S4 제거 후 재설치 (S3 기기에 이어서) ==\n'
  if run uv tool uninstall codex-swap > "$H/uninstall.log" 2>&1 && [ ! -e "$H/tool-bin/codex-swap" ]; then
    ok "S4: 제거"
  else
    no "S4: 제거 실패 — $(tail -2 "$H/uninstall.log" | tr '\n' ' ')"
  fi
  [ -f "$H/.codex/accounts/work/auth.json" ] && [ -f "$H/.codex/accounts/personal/auth.json" ] \
    && ok "S4: 제거해도 등록한 계정은 남는다" || no "S4: 계정이 사라졌다"
  if install_swap S4; then
    home_is S4 "$H/.codex-cli"
    codex_starts S4
    use_lands S4 work work@example.com "$H/.codex-cli/auth.json"
  fi
fi

printf '\npass=%d fail=%d\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
