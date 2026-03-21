---
name: code-review
description: Code review team — coder writes code, reviewer checks quality.
roles:
  - coder
  - reviewer
---

## Team Protocol

Two-phase workflow:
1. **Coder** implements the requested changes
2. **Reviewer** checks for correctness, security, and style

The Lead assigns tasks and mediates between coder and reviewer.
Reviewer can request changes via QUESTION events.
Coder submits plans for high-risk changes before executing.
