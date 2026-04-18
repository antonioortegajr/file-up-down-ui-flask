# Project Workflow

## Managed Development Model

This project uses a split-agent workflow:

| Role | Tool | Responsibility |
|------|------|---------------|
| Project manager | Claude Code (this session) | Create issues, review PRs, coordinate |
| Implementer | OpenCode CLI | Write code, open PRs |
| Approver | User | Final merge approval |

## How We Work

1. **User describes feature or bug** → Claude Code creates a GitHub issue with clear spec
2. **User runs OpenCode CLI** against the issue → OpenCode implements and opens PR
3. **Claude Code reviews PR** via `gh` — checks correctness, requests changes if needed
4. **User approves and merges**

## Rules

- Claude Code does NOT write implementation code directly
- Claude Code does NOT merge PRs — user has final say
- Issues must have clear acceptance criteria before OpenCode picks them up
- PRs must reference the issue they close (`closes #N`)
- One issue per PR; keep scope tight

## GitHub

- Issues: tracked via `gh issue`
- PRs: reviewed via `gh pr`
- Branch naming: `issue-{N}-{short-description}`
