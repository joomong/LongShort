---
name: commit
description: Create git commits without Co-Authored-By attribution. Use when user wants to commit changes.
license: MIT
metadata:
  author: custom
  version: "1.0.0"
---

# Commit Skill

Creates git commits without adding Co-Authored-By attribution to Claude.

## When to Use

Use this skill when the user wants to commit changes to git.

## Instructions

When creating commits:

1. Run git status to see untracked files (never use -uall flag)
2. Run git diff to see staged and unstaged changes
3. Run git log to see recent commit messages and follow the repository's commit style
4. Analyze changes and draft a commit message:
   - Summarize the nature of changes (feature, enhancement, bug fix, refactor, test, docs)
   - Keep it concise (1-2 sentences) focusing on "why" rather than "what"
   - Ensure it accurately reflects the changes and their purpose
5. Add relevant untracked files to staging area
6. Create the commit **WITHOUT** Co-Authored-By message
7. Run git status after commit to verify success

IMPORTANT:
- NEVER add Co-Authored-By attribution
- Do NOT push to remote unless user explicitly asks
- Do NOT use -i flag with git commands
- Do NOT create empty commits if there are no changes
- Always pass commit message via HEREDOC for proper formatting

Example commit command:
```bash
git commit -m "$(cat <<'EOF'
Commit message here.
EOF
)"
```
