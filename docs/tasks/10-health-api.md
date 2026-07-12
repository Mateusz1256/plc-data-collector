# Task 10: Health i runtime API

## Cel

Udostępnić podstawowy wgląd w działanie aplikacji.

## Zakres

Endpointy:

- `GET /health/live`,
- `GET /health/ready`,
- `GET /api/runtime/components`,
- `GET /api/runtime/workers`,
- `GET /api/about`.

## Zasady

- API binduje się domyślnie do localhost,
- endpointy są read-only,
- brak sekretów,
- readiness uwzględnia konfigurację i storage,
- liveness nie zależy od pojedynczego PLC.

## Kryteria akceptacji

- operator widzi stan workerów,
- readiness odróżnia awarię krytyczną od awarii pojedynczego źródła,
- `/api/about` zwraca wersję i placeholder licencji,
- testy API przechodzą.

## Proponowany commit

```text
feat(api): add health and runtime status endpoints
```
