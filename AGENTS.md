# Agent engineering instructions / AI 開發規範

Read README.zh-TW.md before changing architecture. Keep a single deterministic execution authority. Models propose actions; policy and tools enforce authorization. Never use cloud providers without task-specific approval, transmit private data without permission, bypass quotas, purchase credits, or expose credentials. Use isolated worktrees and branches; do not merge main or trigger paid workflows without approval. No changes to other repositories or existing schedules.

## Development
- Begin with a minimal mock provider and reproducible offline baseline. Use Python standard library where practical; pin dependencies and verify supported Python versions.
- Separate provider adapters, controller, tools, evidence, world models and research. Never claim an adapter is integrated until tested.
- Inspect existing code and issues before implementing. Record upstream URL, pinned commit, license, capability, gaps and local validation before adopting upstream code.
- Add tests for failure, replay, cancellation, unauthorized cloud requests and resource exhaustion. Preserve original logs and bind verification to commit SHA. Never change acceptance criteria merely to pass a test.
- Keep candidate research and production release separate. Compare against the minimal baseline under equal resource budgets.
- Use Traditional Chinese or English documentation and keep both synchronized. Never publish personal data, secret values, private repository content or real account credentials.

## Completion evidence
List changed files, exact test commands, outputs, limitations and commit SHA. Open draft PRs for review. Documentation alone is not working implementation.
