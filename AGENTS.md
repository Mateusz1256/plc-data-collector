# AGENTS.md

## Purpose

This repository contains an industrial data collector for PLC and OPC
communication.

Agents working in this repository must prioritize:

1. stability,
2. observability,
3. testability,
4. modularity,
5. safe failure,
6. incremental delivery.

## Required Reading

Before coding, read:

- `AGENTS.md`,
- `README.md`,
- the active task in `docs/tasks/`,
- `CHANGELOG.md`,
- existing tests.

The original planning package provided by the project owner is kept in
`Agents/`. Treat it as product and process context, especially:

- `Agents/MASTER_PROMPT.md`,
- `Agents/AGENTS.md`,
- `Agents/LICENSE_POLICY.md`,
- `Agents/THIRD_PARTY_NOTICES.md`.

## Work One Task At A Time

Only one task from `docs/tasks/` may be active at once.

After completing a task:

- run tests,
- run linting,
- run type checking,
- update documentation,
- update the changelog,
- propose a commit message,
- stop.

Do not continue automatically to the next task.

## Architecture Rules

- One worker owns one protocol connection.
- Never share a non-thread-safe client between workers.
- Never create one thread per tag.
- Use bounded queues.
- Keep protocol code behind adapters.
- Keep database code behind repositories.
- Keep FastAPI code outside the domain layer.
- Never block the asyncio event loop.
- Every network and database operation requires a timeout.
- A failure in one worker must not terminate unrelated workers.
- Runtime state must be observable.
- Configuration must be validated before activation.
- Graceful shutdown is mandatory.
- Secrets must never be logged.

Expected dependency direction:

```text
api -> application/runtime -> domain
protocol adapters -> domain
persistence adapters -> domain
monitoring adapters -> application/runtime
```

The domain layer must not import FastAPI, SQLAlchemy, protocol libraries, or
operating-system service wrappers.

## Coding Style

- Python 3.12+.
- Full type hints.
- Prefer dataclasses or Pydantic models where appropriate.
- Keep functions focused.
- Prefer composition over deep inheritance.
- Use explicit domain exceptions.
- Do not use bare `except`.
- Do not silence failures.
- Do not add speculative abstractions.
- Public functions and classes require concise docstrings.
- Comments should explain why, not narrate obvious syntax.

## Dependencies And Licenses

Before adding a dependency:

1. confirm it is necessary,
2. confirm active maintenance,
3. confirm Python compatibility,
4. check its exact license from official package metadata or repository,
5. add it to `THIRD_PARTY_NOTICES.md`,
6. preserve required copyright notices,
7. avoid dependencies with incompatible or unclear licenses.

Never guess a license.

## Definition Of Done

A task is done only when:

- acceptance criteria are met,
- tests pass,
- linting passes,
- type checking passes,
- documentation is updated,
- changelog is updated when relevant,
- dependencies and licenses are documented,
- no unrelated changes are included.
