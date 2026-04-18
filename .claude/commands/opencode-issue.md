# opencode-issue

Run OpenCode CLI against a GitHub issue, then review the resulting PR.

## Usage

```
/opencode-issue <issue-number>
```

## Steps

1. Read the issue: `gh issue view $ISSUE`
2. Run OpenCode: `opencode run "implement issue #$ISSUE: <title>. Read the issue with gh issue view $ISSUE first, then implement on a new branch issue-$ISSUE-<short-description>, commit, and open a PR that closes #$ISSUE."`
3. Wait for OpenCode to finish and open a PR
4. Review the PR diff with `gh pr diff` and `gh pr view`
5. Report review findings to user — approve if clean, request changes if not
6. Wait for user to merge

## Rules

- Always read the issue before running OpenCode so the prompt is accurate
- Never merge the PR — user has final say
- If OpenCode PR needs changes, communicate clearly so user can re-run
- One issue per run

## Arguments

`$ARGUMENTS` — the issue number to implement
