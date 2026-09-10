# Homebrew formula for codex-swap.
#
# There is no release tarball yet, so this is HEAD-only:
#
#   brew install --HEAD wonjun-lab/tap/codex-swap
#
# To serve it, put this file in a repository named `homebrew-tap` under
# `Formula/codex-swap.rb`; Homebrew maps `wonjun-lab/tap` to that name. Keeping the
# formula here as well means the packaging lives with the thing it packages — when the
# runtime requirements change, the formula is in the diff rather than in another repo
# nobody remembers to open.
#
# `codex` itself is not a dependency: it is distributed through npm, and depending on it
# here would drag a whole node toolchain in for a tool that only ever shells out to it.
# `codex-swap doctor` says so plainly if it is missing.
class CodexSwap < Formula
  include Language::Python::Virtualenv

  desc "Keep several Codex accounts and swap between them as usage climbs"
  homepage "https://github.com/wonjun-lab/codex-swap"
  head "https://github.com/wonjun-lab/codex-swap.git", branch: "main"
  license "MIT"

  depends_on "python@3.12"

  def install
    # No runtime dependencies, so there are no resources to vendor.
    virtualenv_install_with_resources
  end

  def caveats
    <<~EOS
      codex-swap reads usage by running the codex CLI, so install that too:
        npm install -g @openai/codex

      Then check everything is reachable:
        codex-swap doctor
    EOS
  end

  test do
    assert_match "codex-swap", shell_output("#{bin}/codex-swap --version")
    # `list` on an empty config must not fail: a fresh machine has no slots yet, and a
    # formula test that needs credentials is a test that never runs in CI.
    ENV["CODEX_ACCOUNTS_DIR"] = testpath/"accounts"
    ENV["CODEX_ACCOUNT_DEFAULT_HOME"] = testpath/"codex"
    system bin/"codex-swap", "list"
  end
end
