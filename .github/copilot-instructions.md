# Copilot instructions

This file is a pointer, not a rulebook. The rules live in the docs below;
read them before changing anything.

- Process (what needs a Discussion, PR scope, AI-assisted contribution):
  [CONTRIBUTING.md](../CONTRIBUTING.md). Read it in full first.
- Behavioural guide and index of the authoritative specs:
  [CLAUDE.md](../CLAUDE.md).
- Catalog work (new modem, HAR fixtures, `modem.yaml`): follow
  [skills/modem-intake.md](../skills/modem-intake.md).

Three rules to hold to:

- Every commit is `type(scope): description` and CI checks each one.
  Squash scaffolding commits such as `Initial plan` before pushing.
  See [CONTRIBUTING.md § Commit Message Format](../CONTRIBUTING.md#commit-message-format).
- A failing check is fixed in the commits or code it flags, never by
  editing `.github/workflows/` or any other gate.
- Keep the PR to the change asked for. A catalog PR touches catalog
  files only; Core and CI changes need their own Discussion first.
