# Task 04: Mock driver

## Cel

Dodać deterministyczny driver testowy umożliwiający rozwój bez PLC i OPC.

## Zakres

Mock driver powinien obsługiwać:

- stałe wartości,
- sekwencje wartości,
- sztuczne opóźnienie,
- timeout,
- błąd połączenia,
- błąd pojedynczego taga,
- odzyskanie połączenia,
- health check.

## Kryteria akceptacji

- zachowanie jest konfigurowalne,
- testy nie zależą od realnego czasu, jeśli można tego uniknąć,
- driver spełnia wspólny kontrakt,
- można nim testować retry i scheduler.

## Proponowany commit

```text
feat(mock): add deterministic protocol driver
```
