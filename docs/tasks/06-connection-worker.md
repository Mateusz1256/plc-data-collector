# Task 06: Connection worker

Status: done

## Cel

Dodać worker będący wyłącznym właścicielem jednego połączenia i drivera.

## Zakres

- lifecycle workera,
- state machine,
- connect,
- disconnect,
- poll group,
- częściowe błędy tagów,
- health state,
- heartbeat,
- izolacja awarii.

## Kryteria akceptacji

- jeden worker posiada jedną instancję drivera,
- awaria workera nie zatrzymuje innych,
- stan workera jest obserwowalny,
- anulowanie kończy połączenie,
- testy pokrywają reconnect-ready lifecycle.

## Proponowany commit

```text
feat(runtime): add isolated connection worker
```
