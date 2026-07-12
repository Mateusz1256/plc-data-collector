# Task 01: Modele domenowe

Status: done

## Cel

Zdefiniować stabilne modele domenowe niezależne od FastAPI, SQLAlchemy i bibliotek protokołów.

## Zakres

Modele:

- ConnectionConfig,
- TagGroupConfig,
- TagConfig,
- TagRequest,
- TagResult,
- PollExecution,
- RuntimeComponentStatus,
- TagValue.

Enumy:

- Quality,
- ValueType,
- WorkerState,
- OverlapPolicy,
- PollStatus.

Wyjątki:

- GatewayError,
- ConfigurationError,
- TransientCommunicationError,
- PermanentProtocolError,
- StorageError.

## Zasady

- pełne type hints,
- brak zależności od frameworków,
- jawna reprezentacja różnych typów wartości,
- walidacja invariantów domenowych,
- timestamps w UTC.

## Kryteria akceptacji

- modele reprezentują sukces i błąd odczytu,
- wartości są typowane,
- nie można utworzyć niepoprawnego interwału,
- nie można utworzyć taga bez adresu,
- testy pokrywają walidację.

## Proponowany commit

```text
feat(domain): add core communication and runtime models
```

