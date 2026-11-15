# Removed configuration is archived, not deleted

When a Connection, Tag Group or Tag is deleted in the Panel, or is missing from
an imported file, its row stays in the database with `archived_at` set.
Archiving a Connection or Tag Group archives its children too. Archived items
are not polled and are hidden from the Panel and from `/api/v1/tags`, but their
stored readings stay readable at `/api/v1/tags/{id}/history`. Creating an item
with the ID of an archived one restores it. We chose this because
`tag_readings` has foreign keys to `tags`, `tag_groups` and `connections`, and
Poll Results for a removed Tag may still wait in the Reading Queue or the Spool.

## Considered Options

- **Delete the rows and their readings (cascade).** Rejected because removing a
  Tag from the configuration would silently erase its history, and Poll Results
  still in the Spool would fail on replay.
- **Delete the rows and keep the readings without foreign keys.** Rejected
  because readings would point at IDs that no longer exist, so their names,
  addresses and value types would be lost, and SQL consumers could no longer
  join them to configuration.

## Consequences

- An ID, once used, is never free again: creating it again restores the
  archived row, so its old and new readings share one history.
- Archived rows are never removed by the collector. Retention prunes
  `tag_readings` and `poll_executions` only.
- SQL consumers that want the current configuration filter on
  `archived_at IS NULL`; queries over history can join archived rows as well.
