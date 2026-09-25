# Changelog

## 0.3.0 — 2026-09-25

### Keys that moved

The screen's keys were rearranged so that every menu entry is opened by the first letter of
its name. If your fingers learned 0.2.0:

| Was | Now |
| --- | --- |
| `s` switches to the account under the cursor | `enter` switches; `s` opens the switching policy |
| `r` refreshes usage | `f` fetches the latest usage; `r` opens usage resets |
| `p` opens the policy | `s` |
| `o` turns automatic switching on or off | `m` (Mode: auto / manual) |
| `esc` goes back | `b` goes back too, and so does `←` outside the policy screen |

Spending a usage reset still asks `[y/N]`, so a stray `r` costs nothing.

### Screen

- Each menu entry shows its shortcut as one bold letter in its own name: **S**witching
  policy, **F**etch latest usage, **R**eset usage, **A**dd current login, **M**ode, **T**est
  all logins, **U**pdate codex-swap, **Q**uit. Usage resets, the account check and updating
  had no key before.
- Renamed entries: `Add current login` is `codex-swap adopt` (it keeps the login you are in;
  `codex-swap add` still signs a new one in through the browser), `Test all logins` is
  `codex-swap doctor`, and `Mode: auto` / `Mode: manual` is `codex-swap auto on` / `off`.
- `?` (or `h`) lists every key.
- Phones, foldables and tablets: key hints wrap onto more lines instead of losing their words;
  a screen too short for the menu folds it into one or two lines and moves the cursor inside
  that line, so the layout never shifts as the cursor moves. On very short screens the
  accounts and the latest message come first, then the menu, then `? help  q quit`. Policy
  values move to their own line rather than being cut into a different number.

### Switching

- Managed login, probes and launches all use the file credential backend, so a switch reaches
  the next Codex process on macOS even when Codex is configured for the keyring. An explicit
  backend choice, a custom home or a remote invocation is left alone.
- The wrapper passes leading Codex options and a literal `--` through unchanged.
- A failed re-login or sync-back no longer loses credentials; a rotation decision that went
  stale while waiting is dropped; exhausted accounts are never picked; slots that share a
  refresh token with the ChatGPT app are never probed internally.

### Tests

- The install e2e keeps `account/read` local under codex 0.157.0, which started checking a
  selected workspace with the server before answering.
- Terminal tests draw at the size they ask for; they had been drawing at 140×24 regardless.

## 0.2.0 — 2026-09-11

The first tagged release. `0.1.0` was never tagged; everything below landed between 2026-09-05
and 2026-09-11.

### Accounts and switching

- Keep several Codex (ChatGPT) accounts in slots and switch with `use`, or let `rotate` switch
  when usage crosses the ladder you set (`policy`, `auto`).
- `adopt` keeps the login you are already using; `add` signs a new one in through the browser;
  `add --force` signs an existing slot in again without deleting it first. `rename`, `remove`.
- Usage resets (`credits`): how many each account has and until when, and spending one — which
  is refused while the account still has room.

### Screen

- `codex-swap` with no arguments opens a screen that holds the same guards as the CLI. Arrow
  keys move, Enter switches, and the colours follow a light or dark terminal
  (`CODEX_SWAP_THEME` forces one).
- Checking accounts and updating are reachable from the screen's menu.
- On an account row, `n` renames the slot and `d` removes it, behind the same checks as the
  CLI. Removing shows which account it is and what stops working before asking `[y/N]`.

### Setup and upkeep

- `init` places a two-line `codex` wrapper in `~/.local/bin` and prints whatever is left to do.
- `doctor` tries each account for real and says how to fix what fails, including logins that
  only half finished because they were made over SSH.
- `update` knows whether codex-swap came from uv, pipx, pip or Homebrew and reuses that.
- One-line installer (`curl … install.sh | sh`), and Homebrew:
  `brew install wonjun-lab/tap/codex-swap`.

### Living next to the ChatGPT desktop app

- With the app installed, the active login lives in `~/.codex-cli` and `~/.codex` is left to the
  app, so the two stop signing each other out. Registered accounts stay where they were.
- `codex-swap home` tells a wrapper of your own which home to start codex in, and `init` checks
  that such a wrapper really passes it on.
- `doctor` finds slots that hold the same refresh token as the app and never probes them.
- `init` points those slots at `add <label> --force` and only suggests switching from a slot
  that was signed in on its own. Switching from a shared slot would have handed the same token
  to a third place.

### Tests

- `tests/e2e-install.sh` installs into an empty HOME and starts the real codex through the
  wrapper. CI runs it on Linux and macOS.
