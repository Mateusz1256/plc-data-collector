# AGENTS.md

## Purpose

This repository contains an industrial data collector for PLC and OPC communication.

Agents working in this repository must prioritize:

1. stability,
2. observability,
3. testability,
4. modularity,
5. safe failure,
6. incremental delivery.

The system is expected to run continuously. A silent failure is more dangerous than a visible crash, because missing production data discovered two weeks later is not a charming surprise.

## Work one task at a time

Only one task from `docs/tasks/` may be active at once.

Before coding:

- read this file,
- read `README.md`,
- read the active task,
- inspect existing tests,
- inspect `CHANGELOG.md`.

After completing the task:

- run tests,
- run linting,
- run type checking,
- update documentation,
- update the changelog,
- propose a commit message,
- stop.

Do not continue automatically to the next task.

## Architecture rules

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

## Module boundaries

Expected dependency direction:

```text
api -> application/runtime -> domain
protocol adapters -> domain
persistence adapters -> domain
monitoring adapters -> application/runtime
```

The domain layer must not import:

- FastAPI,
- SQLAlchemy,
- protocol libraries,
- operating-system service wrappers.

## Coding style

- Python 3.12+.
- Full type hints.
- Prefer dataclasses or Pydantic models where appropriate.
- Keep functions focused.
- Prefer composition over deep inheritance.
- Use explicit domain exceptions.
- Do not use bare `except`.
- Do not silence failures.
- Do not add speculative abstractions.
- Do not introduce a framework merely to avoid writing twenty clear lines of code.
- Public functions and classes require concise docstrings.
- Comments should explain why, not narrate obvious syntax.

## Concurrency rules

- `asyncio` is the default concurrency model.
- Blocking protocol clients must be isolated using a dedicated executor or adapter.
- Each connection worker owns its client.
- Shared mutable state must be avoided.
- If shared state is unavoidable, its synchronization strategy must be explicit.
- Cancellation must be handled.
- Shutdown must flush or safely persist queued data according to the current milestone.
- Queue sizes must be finite and configurable.

## Error handling

Errors must be classified where possible:

- transient communication errors,
- configuration errors,
- permanent protocol errors,
- database errors,
- internal programming errors.

Retry only errors that are plausibly transient.

Retries require:

- exponential backoff,
- maximum delay,
- jitter,
- structured logging,
- cancellation awareness.

Never retry configuration errors indefinitely.

## Testing requirements

Every task must include appropriate tests.

Minimum expectations:

- unit tests for domain logic,
- async tests for workers and schedulers,
- integration tests for repositories,
- failure-path tests,
- deterministic tests for retry logic,
- fake clocks or injectable timing where useful,
- no reliance on real PLC hardware in the default test suite.

Real protocol tests must be marked separately.

## Commit methodology

Use Conventional Commits:

```text
type(scope): summary
```

Allowed types:

- `feat`: new user-visible capability,
- `fix`: bug fix,
- `refactor`: internal change without behavior change,
- `perf`: measurable performance improvement,
- `test`: test-only change,
- `docs`: documentation-only change,
- `build`: packaging or dependency changes,
- `ci`: continuous integration changes,
- `chore`: maintenance not covered above,
- `revert`: revert of a previous commit.

Examples:

```text
feat(runtime): add bounded reading queue
fix(opcua): reconnect after session timeout
refactor(domain): extract tag value model
test(scheduler): cover skipped overlapping polls
docs(readme): document local startup
build(deps): add asyncua dependency
```

### Commit size

A commit should represent one coherent change.

Small change:

- typo fix,
- one test correction,
- one narrow refactor,
- one configuration adjustment.

Normal feature change:

- one task or one independently reviewable portion of a task,
- implementation plus its tests,
- related documentation.

Large change:

- changes multiple architectural layers,
- introduces a new protocol,
- changes persistence schema,
- modifies public APIs,
- requires migration steps,
- contains several independent concepts.

Large changes must be split unless they are inseparable for correctness.

Do not create commits such as:

```text
update stuff
changes
fix
final
working version
```

Humanity has suffered enough.

## Versioning

Use Semantic Versioning:

```text
MAJOR.MINOR.PATCH
```

- `PATCH`: backward-compatible fixes and small internal improvements,
- `MINOR`: backward-compatible features,
- `MAJOR`: incompatible configuration, API, database, or behavior changes.

Before `1.0.0`:

- breaking changes increment `MINOR`,
- backward-compatible features also increment `MINOR`,
- fixes increment `PATCH`.

Examples:

```text
0.1.0 initial runnable skeleton
0.2.0 mock driver and scheduler
0.2.1 scheduler overlap fix
0.3.0 OPC UA support
1.0.0 first production-supported release
```

Do not bump versions on every commit. Version bumps belong to release commits.

## Changelog

Maintain `CHANGELOG.md` using Keep a Changelog structure.

Use sections:

- Added,
- Changed,
- Deprecated,
- Removed,
- Fixed,
- Security.

During development, add entries under:

```text
## [Unreleased]
```

Entries must describe user-visible or operator-visible changes.

Do not add every internal refactor unless it affects maintenance, architecture, or deployment.

## Dependencies and license compliance

Before adding a dependency:

1. confirm it is necessary,
2. confirm active maintenance,
3. confirm Python compatibility,
4. check its exact license from official package metadata or repository,
5. check transitive licensing where relevant,
6. add it to `THIRD_PARTY_NOTICES.md`,
7. preserve required copyright notices,
8. avoid dependencies with incompatible or unclear licenses.

Never guess a license.

If package metadata and repository documentation disagree, treat the dependency as blocked until clarified.

## License wrapper

All source files may optionally include a short SPDX header once the repository license is selected:

```python
# SPDX-License-Identifier: Apache-2.0
```

Do not add license headers until the project owner selects the final license.

The repository should contain:

- `LICENSE`,
- `THIRD_PARTY_NOTICES.md`,
- optional `NOTICE`,
- license information in packaged distributions,
- an API endpoint or CLI command exposing version and license information,
- generated credits based on actually installed runtime dependencies.

The credits generator must not hardcode guessed licenses. It should derive package names and versions from the locked environment and require verified mappings for license data.

## Security

- No secrets in Git.
- Provide `.env.example`.
- Validate all configuration.
- Avoid remote code execution through plugin loading.
- Do not deserialize untrusted pickle data.
- Bind the administration API to localhost by default.
- Do not expose write endpoints without an explicit security model.
- Mask credentials in logs and diagnostics.
- Add dependency scanning in CI.
- Keep protocol certificates and private keys outside the repository.

## Documentation

When behavior changes, update the relevant docs in the same task.

Documentation must state:

- configuration shape,
- defaults,
- failure behavior,
- retry behavior,
- operational limitations,
- migration requirements.

## Definition of done

A task is done only when:

- acceptance criteria are met,
- tests pass,
- linting passes,
- type checking passes,
- documentation is updated,
- changelog is updated when relevant,
- dependencies and licenses are documented,
- no unrelated changes are included.
