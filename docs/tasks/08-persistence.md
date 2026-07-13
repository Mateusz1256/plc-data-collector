# Task 08: Persistence i migracje

Status: done

## Cel

Dodać wspólny model relacyjny i repozytoria.

## Zakres

- SQLAlchemy 2.x,
- Alembic,
- SQLite development backend,
- modele tabel:
  - connections,
  - tag_groups,
  - tags,
  - tag_readings,
  - poll_executions,
  - runtime_components,
- repozytoria,
- transakcje,
- testy integracyjne.

## Zasady

- typowane kolumny wartości,
- unikalny `event_id`,
- timestamps UTC,
- JSON tylko dla danych specyficznych dla protokołu,
- brak ORM leakage do domain layer.

## Kryteria akceptacji

- migracja tworzy schemat,
- repozytorium zapisuje odczyty i wykonania polli,
- duplikat `event_id` nie tworzy drugiego rekordu,
- testy integracyjne przechodzą na SQLite.

## Proponowany commit

```text
feat(storage): add relational schema and repositories
```
