# CHANGE: MCP-15.6

- change-id:        MCP-15.6
- board-card:       https://github.com/Sugra-Systems/sugra-board/blob/main/cards/MCP-15.6.md
- author-pilot:     grok
- risk-tier:        T1
- tier-justification: CI workflow only. Adds GitHub Release create after a successful PyPI publish. Idempotent if the release already exists. T1.
- required-reviewers: T1: any one of claude / codex / agy (author is grok)

## touched-paths
- .github/workflows/publish-pypi.yml

## intent
A tag push published the wheel and left the Releases tab on an older Latest
(v0.9.1 while 0.10.0 and 0.11.0 were already on PyPI). After Trusted
Publishing, create the GitHub Release for that tag. Skip if it already exists.
Do not retag. Do not publish twice.

## risk-notes
- `contents: write` is required for `gh release create`. Token stays GITHUB_TOKEN.
- `--verify-tag` refuses a release whose tag is missing.
- A failed release step after a successful PyPI upload would red the job; the
  wheel is already public. That is acceptable: the page is the missing half.

## revert-command
git revert <squash-merge sha of this PR>

## test-evidence
Inspection of the workflow. No new pytest. CI Test on the PR still runs.
