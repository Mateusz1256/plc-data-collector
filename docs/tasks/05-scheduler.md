# Task 05: Scheduler grup odczytowych

Status: done

## Cel

Uruchamiać grupy tagów zgodnie z interwałem bez nakładania cykli.

## Zakres

- harmonogram oparty na asyncio,
- polityka overlap `skip`,
- rejestrowanie missed cycles,
- możliwość zatrzymania,
- możliwość anulowania,
- użycie monotonic clock,
- brak driftu wynikającego z prostego `sleep(interval)` po każdym cyklu.

## Kryteria akceptacji

- grupy uruchamiają się według interwału,
- długi odczyt nie tworzy nieograniczonej kolejki,
- scheduler zatrzymuje się poprawnie,
- testy są deterministyczne.

## Proponowany commit

```text
feat(runtime): add non-overlapping poll scheduler
```
