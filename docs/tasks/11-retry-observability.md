# Task 11: Retry, logging i observability

Status: done

## Cel

Dodać spójne mechanizmy retry i logowania strukturalnego.

## Zakres

- exponential backoff,
- jitter,
- maksymalny delay,
- klasyfikacja błędów,
- structured logging,
- correlation IDs,
- podstawowe metryki runtime,
- ograniczenie powtarzalnych logów.

## Kryteria akceptacji

- transient errors są ponawiane,
- configuration errors nie są retryowane bez końca,
- logi nie zawierają sekretów,
- każdy poll i reconnect ma identyfikator korelacyjny,
- testy retry są deterministyczne.

## Proponowany commit

```text
feat(monitoring): add retry policies and structured logs
```
