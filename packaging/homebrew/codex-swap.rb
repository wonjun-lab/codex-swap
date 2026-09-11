# Homebrew formula for codex-swap.
#
#   brew install wonjun-lab/tap/codex-swap
#   brew install --HEAD wonjun-lab/tap/codex-swap   # unreleased main
#
# Homebrew reads the copy in the `wonjun-lab/homebrew-tap` repository, under
# `Formula/codex-swap.rb`; it maps `wonjun-lab/tap` to that name. Keeping the formula here
# as well means the packaging lives with the thing it packages — when the runtime
# requirements change, the formula is in the diff rather than in another repo nobody
# remembers to open.
#
# On each release: tag vX.Y.Z, point `url` at that tag's tarball, set `sha256` from
# `curl -fsSL <url> | shasum -a 256`, and copy this file to the tap.
#
# `codex` itself is not a dependency: it is distributed through npm, and depending on it
# here would drag a whole node toolchain in for a tool that only ever shells out to it.
# `codex-swap doctor` says so plainly if it is missing.
class CodexSwap < Formula
  include Language::Python::Virtualenv

  desc "Keep several Codex accounts and swap between them as usage climbs"
  homepage "https://github.com/wonjun-lab/codex-swap"
  url "https://github.com/wonjun-lab/codex-swap/archive/refs/tags/v0.2.0.tar.gz"
  sha256 "455000df603c716018f5def4964665236f4d84c5e2febe59f75de36961ad5f2a"
  license "MIT"
  head "https://github.com/wonjun-lab/codex-swap.git", branch: "main"

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
    assert_match "codex-swap #{version}", shell_output("#{bin}/codex-swap --version")
    # `list` on an empty config must not fail: a fresh machine has no slots yet, and a
    # formula test that needs credentials is a test that never runs in CI.
    ENV["CODEX_ACCOUNTS_DIR"] = testpath/"accounts"
    ENV["CODEX_ACCOUNT_DEFAULT_HOME"] = testpath/"codex"
    system bin/"codex-swap", "list"
  end
end
