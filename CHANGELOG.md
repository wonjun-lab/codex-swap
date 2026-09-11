# Changelog

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
