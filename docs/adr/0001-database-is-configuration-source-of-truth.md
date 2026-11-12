# The database is the source of truth for configuration

Connections, Tag Groups and Tags live in the database and are edited through
the Panel. A JSON config file is no longer read on every start: it is only an
optional one-time import (seed) for demos and automated setups, plus an export
format. We chose this because two equal sources (a file and the Panel) would
drift apart and conflict.

## Considered Options

- **JSON file as the source of truth, with the Panel rewriting the file.**
  Rejected because it breaks when the file is mounted read-only (containers,
  config management), and because hand edits and Panel edits overwrite each
  other.

## Consequences

- Secret fields in a Connection (for example protocol passwords) are stored in
  the database encrypted, with a key from the environment. The Panel and the API
  show them masked.
- Backing up the configuration means backing up the database, or using export.
