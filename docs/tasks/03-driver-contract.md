# Task 03: Kontrakt drivera i registry

Status: done

## Cel

Zdefiniować wspólny interfejs dla wszystkich protokołów.

## Zakres

- `CommunicationDriver` Protocol lub ABC,
- lifecycle connect/disconnect,
- batch read,
- health check,
- capabilities,
- driver factory,
- driver registry,
- obsługa nieznanego protokołu,
- kontrakt timeoutów i anulowania.

## Kryteria akceptacji

- runtime może utworzyć driver po nazwie protokołu,
- nie zna implementacji konkretnej biblioteki,
- nieznany protokół daje czytelny błąd konfiguracji,
- testy kontraktu przechodzą.

## Proponowany commit

```text
feat(protocols): add driver contract and registry
```
