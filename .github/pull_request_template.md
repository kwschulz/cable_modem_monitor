<!--
Before opening — gating rule:

- Catalog work (new modem, HAR fixtures, parser.yaml): the encouraged path. Claude Code: /modem-intake; other AI tools: load skills/modem-intake.md as context.
- Bug fixes / documented-feature work / small scoped changes: proceed directly.
- Core changes (packages/cable_modem_monitor_core/): start a Discussion regardless of size.
  Link the Discussion + regression tests + golden files + real-modem evidence.
- New features, sensors, architecture, workflow/doc proposals: link a Discussion first.

PRs without a prior Discussion may be closed in favor of starting one.
See CONTRIBUTING.md § Before You File / § What Happens After You File.
-->

# Description

<!-- What does this PR do, and why? Include screenshots for UI changes. -->

## Related

<!-- Use "Related to #N" — not "Fixes #N" or "Closes #N". GitHub auto-closes
     issues from commit messages on merge. -->

Related to #

## Testing

<!-- How was this verified? Include modem model + HA version if relevant. -->

## Checklist

- [ ] This PR is a bug fix, documented-feature change, or small scoped fix — **OR** it links to a prior Discussion (see [CONTRIBUTING § Before You File](../CONTRIBUTING.md#before-you-file))
- [ ] Tested against real modem hardware (catalog PRs and Core parsing/auth changes)
- [ ] Catalog PR: used `/modem-intake` (or `skills/modem-intake.md` with another AI tool) or output matches its structure
- [ ] Breaking change? Migration path described in the Description above
- [ ] Every commit is `type(scope): description` and AI-agent scaffolding commits are squashed (CI checks each commit, see [CONTRIBUTING § Commit Message Format](../CONTRIBUTING.md#commit-message-format))

---

References: [Contributing Guide](../CONTRIBUTING.md) · [Release Process](../docs/reference/RELEASING.md) (version bump, release notes)
