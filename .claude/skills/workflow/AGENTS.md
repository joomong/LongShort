---
name: workflow
description: Full development workflow orchestrator. Use when a task requires coding, review, and testing in sequence. Coordinates the development cycle with automatic retries on failure.
license: MIT
metadata:
  author: custom
  version: "1.0.0"
---

# Workflow Orchestrator Skill

Orchestrates the complete coding → review → test workflow with automatic retries.

## When to Use

Use this skill when you need to:
- Implement a feature and verify it works correctly
- Make changes that need review and testing
- Run a full development cycle automatically

## Workflow Steps

### Step 1: Coding
Use the Task tool to invoke the **coding** agent (subagent_type: "coding").
- Pass the user's request to the coding agent
- The coding agent will make minimal changes only
- Wait for the coding agent to complete

### Step 2: Review
Use the Task tool to invoke the **review** agent (subagent_type: "review").
- Pass the changes made in Step 1 to the review agent
- Review agent must provide a score out of 10 points
- Review criteria:
  - No unnecessary modifications
  - Requirements fully met
  - No over-engineering
  - Code quality and simplicity
- If score < 10: note specific issues for retry
- Only proceed to testing when score = 10

### Step 3: Test
Use the Task tool to invoke the **test** agent (subagent_type: "test").
- Pass test requirements to the test agent
- Test agent will run pytest and analyze failures
- Report test results

## Retry Logic

If review score < 10 or test fails:
1. Analyze the review feedback or test failure
2. Use Task tool to invoke coding agent with targeted fixes (minimal changes only!)
3. Re-run review to get new score
4. Repeat until review score = 10
5. Then proceed to testing

## Output Format

```
## Workflow Execution

### Iteration [n]

#### 1. Coding
[Changes made]

#### 2. Review
Score: [X/10]
[Detailed feedback]
[Issues to fix if score < 10]

#### 3. Test (only if review score = 10)
[PASS/FAIL + summary]

### Final Result: SUCCESS | FAILED

[Summary and any remaining issues]
```

## Rules
- Review must score 10/10 before proceeding to tests
- Each retry should only fix issues identified in review feedback
- Do not add new features or changes beyond the original request
- Stop on SUCCESS (review = 10/10 AND tests pass)
- After full workflow iterations, report final status and issues
- Make small, reviewable diffs
- Follow CLAUDE.md conventions
