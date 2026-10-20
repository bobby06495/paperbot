- **[.github/agents/reviewer.agent.md](.github/agents/reviewer.agent.md)**: The review workflow is inconsistent with the repository’s required output contract.
  - **Context**: The agent tells the reviewer to create a separate `review_comments.md` artifact and ask a follow-up question, while [.github/copilot-instructions.md](.github/copilot-instructions.md) requires a concise markdown list wrapped in a single fenced code block with no greeting or extra commentary.
  - **Actionable Fix**: Align the agent instructions to the repo template exactly: return only the markdown list in one fenced block and remove the extra artifact requirement and follow-up question.
  - **Constraint**: Keep the review focused on code quality and process clarity only; do not change trading or runtime business logic.

- **[.github/copilot-instructions.md](.github/copilot-instructions.md)**: The branch does not yet reflect a committed, enforceable review policy.
  - **Context**: The git status for this branch shows the instructions file as untracked, which means the policy is present in the workspace but not yet part of the branch’s actual committed state.
  - **Actionable Fix**: Stage and commit the instructions file together with the agent definition, then verify the branch is clean before depending on this review workflow in future PRs.
  - **Constraint**: This is a process-only change and should not affect any production behavior or application logic.