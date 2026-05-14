# Task

Review the code changes on branch `{{BRANCH}}` against `{{BASE_BRANCH}}`.

# Context

Read AGENTS.md, CONTEXT.md, the PRD, and the ADRs before reviewing. This repo is being worked by multiple Sandcastle agents, so preserve the branch intent and do not revert unrelated work.

# Review Process

1. Inspect the branch diff:

```sh
git diff {{BASE_BRANCH}}...{{BRANCH}}
```

2. Check whether the implementation satisfies its GitHub issue acceptance criteria.
3. Look for correctness bugs, missing tests, schema or migration hazards, deployment regressions, and unclear contracts.
4. If the issue is materially wrong and the fix is tightly scoped, make the fix and commit it.
5. If the branch is blocked or needs a human decision, do not paper over it. Report it clearly.

# Verification

Run focused tests that cover the changed behavior. Document any skipped or blocked tests.

When complete, output:

<promise>COMPLETE</promise>
