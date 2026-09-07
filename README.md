# codex-swap

Keep several Codex CLI accounts side by side and swap to the next one as usage climbs
toward the limit. No logging out and back in every time you want a different account.

```
codex-swap    gate 70% · margin 5%p

   LABEL     EMAIL                     USED                            CRED  RESET
 >*work      account.name@gmail.com     58%  ██████████████▒▒▒┆▒▒▒▒▒▒     1  09-13 02:00 (in 5h)
   personal  other.name@gmail.com       72%  █████████████████╪▒▒▒▒▒▒     0  09-14 07:00 (in 1d)
   spare     third.name@gmail.com         ?  ────────────────────────     -  -

                                                         ┴    ┻  ┴  ┴
                                                         50   70 85 95  ┻ = current gate

  ^v move   enter switch   r usage   a adopt   p policy   o auto   q quit
  Auto switch: on   (o to turn off)
```

`*` is the account in use, `>` is the cursor. On a bar, `┆` is the gate you have to
cross next and `╪` means you already crossed it. `CRED` is how many usage-reset credits
the account has left — when an account is exhausted and still has a credit, spending it
is an alternative to switching.

## What you need

| | |
| --- | --- |
| Python | 3.11 or newer. No runtime dependencies |
| [Codex CLI](https://github.com/openai/codex) | installed, and logged in to at least one account |
| [uv](https://docs.astral.sh/uv/) | for installing. There is a pip path below |

Usage is read by running `codex app-server` as a child process, so the Codex CLI has to
actually be runnable — check that `codex --version` works first.

## Install

```bash
uv tool install git+https://github.com/wonjun-lab/codex-swap.git
```

Without uv, pip works too (a virtualenv is recommended):

```bash
pip install git+https://github.com/wonjun-lab/codex-swap.git
```

## First run

Automatic switching needs **two or more** accounts. With one, `rotate` stops quietly at
`only one account registered`.

```bash
# 1. Keep the account you are logged in as
codex-swap adopt work

# 2. Add a second one — a browser opens.
#    Log in with an account *different* from the current one.
codex-swap add personal

# 3. Check
codex-swap list
```

`adopt` only **copies** the current credentials into a slot, so you stay logged in.
`add` logs in against the new slot's own home, so it does not disturb the account you
are using.

Then run it with no arguments to get the TUI:

```bash
codex-swap
```

`enter` switches, `r` refreshes usage, `p` edits the policy.

## Wiring up automatic switching

`rotate` **does not run itself.** Calling it right before codex is the caller's job —
usually a shell function.

<details>
<summary>bash · zsh</summary>

In `~/.bashrc` or `~/.zshrc`:

```bash
codex() {
  command codex-swap rotate >/dev/null || true   # see the two notes below
  command codex "$@"
}
```
</details>

<details>
<summary>fish</summary>

In `~/.config/fish/functions/codex.fish`:

```fish
function codex
    command codex-swap rotate >/dev/null; or true
    command codex $argv
end
```
</details>

Two things about that line.

**`|| true`.** `rotate` exits non-zero whenever it did *not* switch, which is the normal
case. That is a deliberate contract — a caller can test the exit code to find out whether
the account changed — but it means the command "fails" almost every time. Inside a script
running under `set -e`, that aborts before codex ever starts.

**`>/dev/null`, not `2>&1`.** `rotate` keeps a strict output discipline for exactly this
spot: normally it writes nothing at all, to either stream, and only when a switch
actually happened does it put one line on stderr. Swallowing stderr too means you never
see that your account changed under you. If the configuration is broken it exits quietly without doing anything
(fail-open) — the switcher will not be the reason codex fails to start.

A codex session that is already running keeps the old token. The new account takes
effect from the next codex invocation.

### Turning it off

```bash
mkdir -p ~/.claude && touch ~/.claude/.codex-rotate-off   # off
rm ~/.claude/.codex-rotate-off                            # on
```

The `o` key in the TUI toggles the same file. To skip a single run, set
`CODEX_ROTATE_SKIP=1`.

> The path sits under `~/.claude` because an earlier implementation used that directory
> for state. Installations already have the switch under that name, so it was not moved.

## Why didn't it switch?

`rotate` is silent by design, so to see the reasoning use `--dry-run`. It changes
nothing and prints the conclusion.

```bash
$ codex-swap rotate --dry-run
no switch: active 41% below first rung 50%
```

Answers you will see most:

| Output | Meaning | What to do |
| --- | --- | --- |
| `active N% below first rung M%` | There is still headroom | Nothing. Lower the first rung on the `p` screen if you want |
| `active usage unreadable` | Could not read the usage | See "When usage shows `?`" below |
| `no candidate answered a probe` | Could not read any of the other accounts | Same |
| `only one account registered` | Nothing to switch to | `codex-swap add <label>` |
| `active account is not a registered slot` | The current account is in no slot | `codex-swap adopt <label>` to keep it |
| `margin (N% + 5 > M%)` | The candidate is not enough lower | Lower the margin, or wait |
| `cooldown` | You just switched | Wait (15 min by default) |
| `throttled` | Inside the window that batches decisions | 60 s by default |
| `ladder exhausted` · `reached but no lighter account` | Every account is spent | Wait for a reset, or spend a credit |
| `off switch (…)` · `CODEX_ROTATE_SKIP` | You turned it off | See "Turning it off" |
| `recent job activity` | The busy gate | See the environment table below |
| `CODEX_HOME points elsewhere` | You called it with a different home set | Deliberate protection. Unset `CODEX_HOME` |

Switches that did happen are recorded in `~/.codex/accounts/rotate.log` (tokens are
never written there).

## When usage shows `?`

| Shown | Meaning |
| --- | --- |
| `58%` | Fresh reading |
| `~58%` | Cached value past its TTL (5 min by default). Correct, but maybe not current |
| `?` | Never read successfully |

If `?` persists, find out why:

```bash
codex-swap status --fresh
```

`usage: probe failed` means either `codex app-server` could not be started or the
credentials are no longer valid. Check `codex --version` and `codex login status` first.
If discovery is the problem, point `CODEX_ACCOUNT_BIN` straight at the binary.

`list` **never touches the network** — it only reads the cache. For fresh numbers use
`status --fresh` or `r` in the TUI.

## Policy

A switch is only considered when usage crosses a rung of the ladder (`50,70,85,95` by
default). The current gate is the rung just above the **least-used** account — anchoring
it on the active account instead would let whichever account is ahead keep climbing, and
alternating would never happen. The candidate has to be at least the margin (5%p by
default) lower before anything changes, and there is a cooldown (15 min) between
switches.

Edit it on the `p` screen of the TUI or override it with environment variables. Values
live in `~/.codex/accounts/config.json`, and **environment variables win.**

## Commands

| Command | What it does |
| --- | --- |
| `codex-swap` (no arguments) | TUI: list, usage bars and policy editor on one screen |
| `codex-swap adopt <label>` | Store the account you are logged in as under `<label>` |
| `codex-swap add <label>` | Log in to a new slot (opens a browser) |
| `codex-swap list` | Stored accounts and cached usage (no probing) |
| `codex-swap status [--fresh]` | Active account and its usage. `--fresh` probes now |
| `codex-swap use <label>` | Switch by hand |
| `codex-swap rotate [--dry-run]` | Run the policy |
| `codex-swap remove <label>` | Delete a slot (the live credentials are untouched) |
| `codex-swap clean` | Clear probe leftovers from the slots (`auth.json` is kept) |

`ls`, `switch` and `rm` exist as aliases.

## Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `CODEX_ROTATE_LADDER` | `50,70,85,95` | Switch gates |
| `CODEX_ROTATE_MARGIN` | `5` | Candidate must be this much (%p) lower |
| `CODEX_ROTATE_COOLDOWN` | `900` | Minimum seconds between automatic switches |
| `CODEX_ROTATE_CACHE_TTL` | `300` | How long a usage reading stays fresh |
| `CODEX_ROTATE_CHECK_INTERVAL` | `60` | Throttles the decision itself |
| `CODEX_ROTATE_BUSY_WINDOW` | `180` | See below |
| `CODEX_ROTATE_SKIP` | — | Any value makes `rotate` do nothing |
| `CODEX_ROTATE_STATE_ROOT` | (below) | Directory the busy gate watches |
| `CODEX_ACCOUNTS_DIR` | `~/.codex/accounts` | Where slots live |
| `CODEX_ACCOUNT_DEFAULT_HOME` | `~/.codex` | Home of the active account. **Set this if you moved codex's home with `CODEX_HOME`** — see below |
| `CODEX_ACCOUNT_BIN` · `CODEX_REAL_BIN` | — | Point at the codex binary directly (skips discovery) |

**If you set `CODEX_HOME`, set `CODEX_ACCOUNT_DEFAULT_HOME` to match.** codex-swap does
not follow `CODEX_HOME` on purpose: `add` hands a slot to its child through that same
variable, and if the default home followed it the recursion guard in `rotate` would stop
firing. Without the extra variable, `adopt` will tell you that you are not logged in
even though you are — it now says so and names the variable to set.

**The busy gate is effectively off by default.** It defers a switch when some `*.log`
under `CODEX_ROTATE_STATE_ROOT` has been touched recently — the idea being "you are in
the middle of a conversation". The default path belongs to one particular agent runtime,
so on a machine without that directory the check is always false. To use it, point the
variable at a log directory of your own.

## How credentials are handled

This tool moves OAuth tokens around, so here is what it does and does not do.

- **It makes no outbound network requests.** The package has no HTTP client. Usage is
  read by spawning `codex app-server` and exchanging JSON-RPC over its stdio; the actual
  API calls are the Codex CLI's. There is no telemetry and no auto-update.
- **Tokens are never logged.** `rotate.log` holds a timestamp, labels and a reason.
  A test asserts that no token leaks into error output.
- **File permissions.** Slot directories are `0700`; `auth.json` and the cache are
  `0600`, and they are created that way rather than fixed up afterwards.
- **Switching is atomic.** It takes a lock, writes a temp file and renames it, so a
  half-written `auth.json` never exists.
- **Labels become paths.** Names starting with `../` or `.`, names with slashes, and
  anything over 64 characters are refused, and an `auth.json` inside a slot that
  symlinks outside the root is not accepted.

## State

```
~/.codex/auth.json                    active account (the email inside it *is* "which account am I on")
~/.codex/accounts/<label>/auth.json   per-slot credentials (mode 600, directory 700)
~/.codex/accounts/config.json         policy (written by the `p` screen)
~/.codex/accounts/.usage-cache.json   per-label usage cache (TTL)
~/.codex/accounts/.last-rotate        last automatic switch (cooldown)
~/.codex/accounts/.last-check         last decision (throttle)
~/.codex/accounts/rotate.log          switch ledger (never holds tokens)
```

Not recording "which account am I on" as a separate field is the heart of the design.
The active account is always decided by the email inside `~/.codex/auth.json`. Log in
again by hand with `codex login` and the bookkeeping still cannot disagree with reality —
there is no field for it to disagree with.

## Why it exists

It started as a bash script: 481 lines with a policy engine whose core — the ladder
decision — was tangled up with probing and file I/O and could not be tested in
isolation. This version pulls the policy out as pure functions and checks every
combination without touching the network.

A side effect was losing dependencies. The bash version needed `jq`, `base64` and
`node`. `node` was there because the usage probe was written in JavaScript, but all the
probe does is spawn app-server and exchange NDJSON JSON-RPC, which the standard library
covers directly.

## Development

```bash
uv sync
uv run pytest
uv run ruff check
uv run ruff format --check
```

The design notes — the contracts, the places this port was easy to get wrong, and the
divergences that were deliberate — are in [`docs/design/`](docs/design/). They are
written in Korean, as are the code comments.

## License

MIT. See [`LICENSE`](LICENSE).
