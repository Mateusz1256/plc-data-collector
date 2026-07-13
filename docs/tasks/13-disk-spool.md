# Task 13: Trwały spool awaryjny

Status: done

## Cel

Zapewnić przetrwanie odczytów podczas niedostępności głównej bazy.

## Zakres

- lokalny SQLite spool,
- unikalny event_id,
- retry_count,
- next_retry_at,
- replay batchami,
- usunięcie dopiero po potwierdzonym zapisie,
- limit rozmiaru,
- alarm przy zapełnieniu,
- recovery po restarcie procesu.

## Kryteria akceptacji

- dane przetrwają restart aplikacji,
- ponowna wysyłka nie duplikuje danych,
- spool ma limit,
- przekroczenie limitu jest widoczne,
- testy symulują awarię i powrót bazy.

## Proponowany commit

```text
feat(storage): add durable offline spool
```
