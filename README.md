# codex-swap

Keep several Codex CLI accounts side by side and swap to the next one as usage climbs
toward the limit. No logging out and back in every time you want a different account.

```
codex-swap    gate 70% · margin 5%p

   LABEL       EMAIL                     USED                                 RESETS    RENEWS
 > personal    other.name@gmail.com      72%      █████████████████╪▒▒▒▒▒▒    0         09-12 02:03 (in 1d)
   spare       third.name@gmail.com      ?        ────────────────────────    -         -
  *work        account.name@gmail.com    58%      ██████████████▒▒▒┆▒▒▒▒▒▒    1         09-11 01:03 (in 4h)
                                                              ┴    ┻  ┴  ┴
                                                              50   70 85 95    ┻ = current gate

   Policy settings
   Refresh usage
   Usage resets
   Adopt the account in use
   Automatic switching: on
   Check accounts
   Update codex-swap
   Quit

   enter select   s switch   r usage   a adopt   p policy   o auto   q quit   ↑↓ move
```

`*` is the account in use, `>` is the cursor. On a bar, `┆` is the gate you have to
cross next and `╪` means you already crossed it. `RESETS` is how many usage resets
the account has left — when an account is exhausted and still has a usage reset, spending it
is an alternative to switching.

The cursor runs past the accounts into the menu underneath, and `enter` does whatever the
row it is on says: switches to that account, or opens that menu entry. `s` still switches,
so the old finger memory keeps working.

Switching is not the irreversible part — you can always switch back. The one thing you
cannot undo is discarding credentials that are not saved in any slot, and that asks twice
no matter which key you arrive on.

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
curl -LsSf https://raw.githubusercontent.com/wonjun-lab/codex-swap/main/install.sh | sh
```

That picks whichever of uv, pipx or pip you already have and runs one command with it —
there is nothing in [`install.sh`](install.sh) you could not type yourself, which is the
only reason piping a script into a shell is reasonable here. It never uses sudo.

Or name the tool yourself:

```bash
uv tool install git+https://github.com/wonjun-lab/codex-swap.git    # isolated
pipx install git+https://github.com/wonjun-lab/codex-swap.git       # isolated
pip install --user git+https://github.com/wonjun-lab/codex-swap.git
```

`codex-swap update` knows which of these you used and reuses it.

A Homebrew formula is kept in [`packaging/homebrew/`](packaging/homebrew/codex-swap.rb) but is
**not published yet.** There is no tagged release to point it at, and a HEAD-only formula is
one that plain `brew upgrade` skips — people would install it and quietly stay on the first
build. It goes into the tap with the first release.

### Then run init

```bash
codex-swap init
```

It sets this machine up and prints whatever is left: codex missing, no accounts yet.
The installer calls it for you at the end, and you can run it again any time — it stops
mentioning things you have already done.

Wiring is not something it asks you to do. `init` places a two-line `codex` wrapper in
`~/.local/bin` and everything else happens behind it: the policy runs before each codex
call, and on machines with the ChatGPT desktop app your accounts are kept out of its way.
The wrapper only delegates (`exec codex-swap exec "$@"`), so it cannot go stale — upgrade
the package and the behaviour follows.

If something already occupies `~/.local/bin/codex`, `init` leaves it alone and says so.
A wrapper of your own that calls `codex-swap` counts as wired.

### Sharing a machine with the ChatGPT desktop app

The desktop app runs its own codex with `CODEX_HOME=~/.codex` and keeps its account in
`~/.codex/auth.json`. That file is the one codex-swap swaps. Both writing to it looks, from
where you sit, like an account that keeps logging itself out — and the ledger fills up with
switches that all start from the same account, because the app's are never recorded.

**codex-swap is the third party here, so codex-swap moves.** With the app installed, the
live credentials go in `~/.codex-cli` and `~/.codex` is left to the app. No setting to
change: it is decided each time codex-swap runs, so installing the app later is enough.

**Your registered accounts do not move with it.** They stay in `~/.codex/accounts`, which
the app never touches, so there is nothing to migrate and nothing to lose. The first switch
after the split fills the new home; `codex-swap init` says so if you have not made it yet.

**If your own `codex` wrapper is on PATH** — from a dotfiles repo, say — it has to start codex
in that home too. Otherwise switching changes codex-swap's credentials while codex keeps
reading the app's, and nothing reports an error. Put this before it runs `codex-swap rotate`:

```bash
export CODEX_HOME="$(codex-swap home)"
```

`codex-swap init` looks for exactly this and says so when it is missing, or when the wrapper
still calls the old bash switcher.

**Accounts registered before the split may share a login with the app.** A refresh token is
replaced every time it is used, so two copies of one login cannot both stay valid. `doctor`
flags a slot holding the same token as the app; sign that account in again on its own with
`codex-swap add <label> --force`. It does not probe such a slot, because refreshing it could
sign the app out.

Set `CODEX_ACCOUNT_DEFAULT_HOME` if you would rather choose the location yourself — an
explicit value always wins, but pointing it at `~/.codex` puts you back in the app's way, and
`init` and `doctor` both say so.

### When something looks wrong

```bash
codex-swap doctor        # or: pick "Check accounts" in the TUI
```

It tries each account for real rather than checking that files exist, and prints what to
do about anything that fails. The two are not the same: credentials can be present and
still be dead, which shows up as an email that reads fine next to a usage column of `?`.

That happens most often after logging in over SSH — the browser is on your laptop while
the listener waiting for the OAuth callback is on the remote box, so the login
half-finishes without saying so. `doctor` names that case specifically.

### Updating

```bash
codex-swap update          # or: pick "Update codex-swap" in the TUI
codex-swap update --check  # say whether there is anything new, install nothing
```

It reads how it was installed — uv, pipx or pip, from git or from a path — and reuses
that, so you do not have to remember. Since a git install keeps the same version number
while the commit moves, it compares commits rather than versions and tells you when there
is nothing to do.

**If `update` is not there yet**, you are on a build from before it existed — run the
install line again instead. Nothing needs uninstalling first: every path above overwrites
in place, and your accounts live in `~/.codex/accounts`, outside the package.

```bash
curl -LsSf https://raw.githubusercontent.com/wonjun-lab/codex-swap/main/install.sh | sh
```

If it cannot tell where it came from it prints the command and stops rather than guessing:
running the default URL over an install that came from a fork would quietly replace it.

## First run

This is what `init` walks you through. Automatic switching needs **two or more** accounts;
with one, `rotate` stops quietly at `only one account registered`.

```bash
# 1. Keep the account you are logged in as
codex-swap adopt work

# 2. Add a second one — a browser opens.
#    Log in with an account *different* from the current one.
codex-swap add personal

# 3. Check
codex-swap list
```

**Over SSH, run `add` while sitting at that machine.** It opens a browser for the OAuth
callback, and a browser on your laptop cannot reach the listener waiting on the remote —
the login half-finishes without saying so, leaving credentials that look present and are
not. `codex-swap doctor` names this case if you hit it.

`adopt` only **copies** the current credentials into a slot, so you stay logged in.
`add` logs in against the new slot's own home, so it does not disturb the account you
are using.

Then run it with no arguments to get the TUI:

```bash
codex-swap
```

`enter` (or `s`) switches to the account under the cursor, `r` refreshes usage, `p` edits
the policy. Or move down to the menu and press `enter` — every shortcut has an entry there.

## Wiring up automatic switching

`init` already did this: the wrapper it puts in `~/.local/bin/codex` runs the policy
before handing over to the real binary. Nothing else to set up.

If you would rather wire it yourself — you keep your shell config in a repo, or you want
the policy on some invocations and not others — a shell function does the same job:

<details>
<summary>bash · zsh · fish</summary>

```bash
codex() {
  command codex-swap rotate >/dev/null || true   # see the two notes below
  command codex "$@"
}
```

```fish
function codex
    command codex-swap rotate >/dev/null; or true
    command codex $argv
end
```

`init` recognises any wrapper or function that calls `codex-swap` and leaves it alone,
so it will not tell you something is missing that is not.
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
codex-swap auto off
codex-swap auto on
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
| `ladder exhausted` · `reached but no lighter account` | Every account is spent | Wait for a reset, or spend a usage reset |
| `off switch (…)` · `CODEX_ROTATE_SKIP` | You turned it off | See "Turning it off" |
| `recent job activity` | The busy gate | See the environment table below |
| `CODEX_HOME points elsewhere` | You called it with a different home set | Deliberate protection. Unset `CODEX_HOME` |

Switches that did happen are recorded in `~/.codex/accounts/rotate.log` (tokens are
never written there).

## When usage shows `?`

| Shown | Meaning |
| --- | --- |
| `58%` | Fresh reading |
| `~58% 5h` | Cached value past its TTL (5 min by default), and how old it is |
| `?` | Never read successfully |

If `?` persists, find out why:

```bash
codex-swap status --fresh
```

`usage: probe failed` means either `codex app-server` could not be started or the
credentials are no longer valid. Check `codex --version` and `codex login status` first.
If discovery is the problem, point `CODEX_ACCOUNT_BIN` straight at the binary.

`list` **never touches the network** — it only reads the cache. `codex-swap list --fresh`
probes every slot and stores what it reads; `status --fresh` only reads the active account,
and `r` in the TUI reads them all.

Watch the age next to `~`. Nothing refreshes an idle account on its own: `rotate` stops
early while the active account is below the first rung, which is most of the time, so a
slot you are not using can sit at a reading from days ago — long enough for its window to
have reset underneath it.

## Usage resets

A usage reset gives one account a fresh window. The `RESETS` column shows how many an
account has, but not how long they last — a reset that expires tonight and one with two
months left both read as `1`.

```
codex-swap credits
```

```
    LABEL  EMAIL               RESET      EXPIRES
*   master you@example.com     Full reset 10-05 13:20 (in 25d)
    shared other@example.com   -          -
```

This probes every account, so it takes a moment and needs the network — unlike `list`,
which only reads the cache. Credits are not cached: they change rarely and the detail is
only wanted when you ask for it.

### Spending one

```
codex-swap credits use            # the active account
codex-swap credits use shared     # a named one
codex-swap credits use --dry-run  # say what would happen, spend nothing
```

The TUI has the same screen: `Usage resets` in the menu, then move to the reset you want and
press `enter`. Both surfaces then ask the same thing — `y` to go ahead, naming the account
and when that reset expires.

The confirmation is the only thing between you and an irreversible action; the cursor starts
on the first row, so there may be nothing to move. That is why the question spells out what
you are about to lose rather than just asking.

It asks before spending, naming the account. **A spent reset cannot be recovered**, so
without a terminal it refuses rather than guessing — pass `--yes` if you mean it from a
script. `remove` is more relaxed about this because deleting a slot leaves the live
credentials alone; a usage reset has no such second copy.

When several are available it spends the one that **expires first** — the one you would
lose anyway. Override with `--credit <id>`; `credits --json` prints the ids.

Automatic switching never spends one. That is a decision with a cost, and the policy
has no way to know whether you would rather switch accounts instead.

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
| `codex-swap list [--fresh]` | Stored accounts and cached usage. `--fresh` probes every slot |
| `codex-swap status [--fresh]` | Active account and its usage. `--fresh` probes now |
| `codex-swap policy [--ladder …]` | Show or change the switching policy |
| `codex-swap auto [on\|off]` | Turn automatic switching on or off |
| `codex-swap credits` (`resets`) | Usage resets per account, with the date each one expires |
| `codex-swap credits use [label]` | Spend one usage reset. Asks first; `--dry-run` spends nothing |
| `codex-swap use <label>` | Switch by hand |
| `codex-swap rotate [--dry-run]` | Run the policy |
| `codex-swap rename <old> <new>` | Give a slot a different name |
| `codex-swap remove <label>` | Delete a slot (the live credentials are untouched) |
| `codex-swap clean` | Clear probe leftovers from the slots (`auth.json` is kept) |

`ls`, `switch` and `rm` exist as aliases.

`remove` asks for confirmation when it is talking to a terminal, and names the account it
is about to delete. `--yes` skips the question; called from a script it does not ask at
all, because a prompt behind a pipe never returns.

If a slot's token has gone stale, `codex-swap add --force <label>` logs in again and
replaces it. Without `--force` an existing label is refused — the point is that you should
not have to `remove` (irreversible) before attempting a login (which can fail).

### Machine-readable output

`list --json` and `status --json` print values instead of a formatted table, and their
shape is a contract — the human table is free to change its widths and wording, so scripts
should not parse it.

```bash
$ codex-swap list --json | jq -r '.accounts | map(select(.usedPercent != null))
                                            | min_by(.usedPercent) | .label'
personal
```

Usage that has never been read is `null`, not `0`. Zero would read as "the least-used
account" and invert the decision it feeds.

```json
{
  "active": "work",
  "accounts": [
    {"label": "work", "email": "…", "active": true,
     "usedPercent": 58, "stale": false, "resetsAt": 1788844071, "resetCredits": 1}
  ],
  "policy": {"ladder": [50,70,85,95], "margin": 5, "cooldown": 900,
             "cacheTtl": 300, "checkInterval": 60, "busyWindow": 180},
  "autoSwitch": true
}
```

`status --json` reports failure as data too (`"ok": false` with null readings), so a
script never has to read prose off stderr. The exit code still follows the human form.

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
| `CODEX_SWAP_THEME` | `auto` | `dark`, `light`, or `auto` — see below |

### Light and dark terminals

The TUI asks your terminal what colour its background is (OSC 11) and picks foreground
colours to match. The background itself is never painted, so your terminal's own theme
shows through either way.

This matters because the default yellow all but disappears on a white background — on a
light terminal the warning colour stopped reading as a warning. On light backgrounds it
uses darker greens, ambers and blues instead.

Terminals that do not answer fall back to `COLORFGBG`, and then to dark. If the guess is
wrong — over SSH, inside a multiplexer, or with a terminal that lies — say so directly:

```bash
CODEX_SWAP_THEME=light codex-swap
```

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

Probing runs `codex app-server` with the slot as its `CODEX_HOME`, so each slot grows a
full codex home of its own — sqlite databases, logs, caches. Measured at roughly 8 MB per
slot after ordinary use. `codex-swap clean` deletes all of it and keeps `auth.json`; run
it when the directory gets larger than you would like. Nothing breaks if you never do.

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
