# Task 09: Batch database writer

## Cel

Dodać niezależny konsument kolejki zapisujący rekordy batchami.

## Zakres

- batch size,
- flush interval,
- transakcyjny zapis,
- retry storage errors,
- metryki sukcesów i błędów,
- graceful shutdown,
- idempotentne ponowienie batcha.

## Poza zakresem

- trwały spool dyskowy,
- wielobazowy routing.

## Kryteria akceptacji

- writer nie blokuje workerów,
- flush następuje po rozmiarze lub czasie,
- retry nie duplikuje rekordów,
- shutdown próbuje opróżnić kolejkę w limicie czasu,
- testy awarii bazy przechodzą.

## Proponowany commit

```text
feat(storage): add batched reading writer
```
