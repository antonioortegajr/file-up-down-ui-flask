# Project Workflow

## Managed Development Model

This project uses a split-agent workflow:

| Role | Tool | Responsibility |
|------|------|---------------|
| Project manager | Claude Code | Investigate, plan, create issues, run OpenCode, review PRs |
| Implementer | OpenCode CLI | Write code, open PRs |
| Approver | User | Final merge approval |

## How We Work

1. **User describes feature or bug** → Claude Code investigates, plans, and creates a GitHub issue with clear spec + acceptance criteria
2. **Claude Code runs OpenCode CLI** against the issue → OpenCode writes code and opens a PR on GitHub
3. **Claude Code reviews the PR** via `gh` — checks correctness, requests changes if needed (Claude Code re-runs OpenCode if changes required)
4. **Claude Code asks user to approve and merge** — user has final say, Claude Code never merges

## Rules

- Claude Code does NOT write implementation code directly — ever
- Claude Code does NOT merge PRs — user has final say
- Claude Code runs OpenCode CLI to implement; OpenCode must open a PR (not edit files directly)
- Issues must have clear acceptance criteria before OpenCode picks them up
- PRs must reference the issue they close (`closes #N`)
- One issue per PR; keep scope tight
- No exceptions to this workflow regardless of how the user phrases the request

## GitHub

- Issues: tracked via `gh issue`
- PRs: reviewed via `gh pr`
- Branch naming: `issue-{N}-{short-description}`
