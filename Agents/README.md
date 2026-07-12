# PLC Gateway

Modularny collector danych przemysłowych napisany w Pythonie.

Celem projektu jest dostarczenie stabilnego runtime'u do cyklicznego odczytu grup tagów z PLC i serwerów OPC, normalizacji wyników oraz zapisu do relacyjnej bazy danych.

Projekt jest rozwijany etapami. Aktualny zakres to MVP, nie próba zbudowania całego ekosystemu SCADA w jeden weekend.

## Status

Projekt w fazie projektowania i implementacji MVP.

Plan prac znajduje się w katalogu:

```text
docs/tasks/
```

Agent lub developer powinien realizować wyłącznie jeden task naraz.

## Główne założenia

- jeden worker jest właścicielem jednego połączenia,
- tagi są grupowane według źródła i interwału,
- komunikacja jest oddzielona od zapisu przez kolejkę,
- wszystkie drivery implementują wspólny kontrakt,
- dane są normalizowane do wspólnego modelu,
- błędy jednego połączenia nie zatrzymują pozostałych,
- retry używa exponential backoff i jittera,
- runtime udostępnia health-check i status komponentów,
- aplikacja ma działać bez GUI jako długotrwały proces lub usługa.

## Planowany stack MVP

- Python 3.12+
- FastAPI
- Pydantic
- SQLAlchemy 2.x
- Alembic
- asyncio
- SQLite dla developmentu
- PostgreSQL lub SQL Server przez adapter persistence
- pytest
- pytest-asyncio
- ruff
- mypy
- structlog lub standardowy logging w formacie JSON
- asyncua dla pierwszego rzeczywistego drivera OPC UA

Ostateczne zależności muszą zostać zatwierdzone w odpowiednim tasku.

## Architektura

```text
┌────────────────────────────────────────────┐
│              Administration API            │
└──────────────────────┬─────────────────────┘
                       │
┌──────────────────────▼─────────────────────┐
│           Configuration Manager            │
└──────────────────────┬─────────────────────┘
                       │
┌──────────────────────▼─────────────────────┐
│                Scheduler                    │
└───────────────┬────────────────────────────┘
                │
      ┌─────────▼─────────┐
      │ Connection Worker │
      └─────────┬─────────┘
                │
      ┌─────────▼─────────┐
      │ Protocol Driver   │
      └─────────┬─────────┘
                │
      ┌─────────▼─────────┐
      │ Bounded Queue     │
      └─────────┬─────────┘
                │
      ┌─────────▼─────────┐
      │ Database Writer   │
      └───────────────────┘
```

## Docelowa struktura katalogów

```text
src/
  plc_gateway/
    app/
    api/
    domain/
    protocols/
    runtime/
    persistence/
    monitoring/
    service/
tests/
docs/
  tasks/
```

## Przykładowa konfiguracja

Planowany kierunek:

```yaml
connections:
  - id: opcua_demo
    protocol: opcua
    endpoint: opc.tcp://127.0.0.1:4840
    enabled: true

tag_groups:
  - id: demo_fast
    connection_id: opcua_demo
    interval_ms: 1000
    timeout_ms: 3000
    overlap_policy: skip

tags:
  - id: demo_temperature
    tag_group_id: demo_fast
    name: Temperature
    address: ns=2;s=Machine.Temperature
    data_type: float
    enabled: true
```

Dokładny format zostanie ustalony i zwalidowany w tasku konfiguracji.

## Model współbieżności

Domyślnym mechanizmem jest `asyncio`.

Nie tworzymy:

- jednego wątku na każdy tag,
- współdzielonych klientów protokołów bez synchronizacji,
- nieograniczonych kolejek,
- blokujących wywołań w event loopie.

Biblioteki synchroniczne będą izolowane w adapterach i uruchamiane przez kontrolowany executor.

## Baza danych

MVP używa wspólnego modelu danych zamiast osobnych tabel dla każdego urządzenia.

Planowane encje:

- `connections`,
- `tag_groups`,
- `tags`,
- `tag_readings`,
- `poll_executions`,
- `runtime_components`.

Wartości odczytów będą przechowywane w typowanych kolumnach, a nie jako jeden tekstowy worek bez dna.

## Uruchamianie

Instrukcja zostanie uzupełniona po ukończeniu tasków bootstrap i konfiguracji.

Planowany tryb developerski:

```bash
python -m venv .venv
pip install -e ".[dev]"
uvicorn plc_gateway.api.main:app --reload
```

Nie traktuj powyższych komend jako gotowych przed ukończeniem odpowiednich tasków.

## Jakość

Każda zmiana powinna przechodzić:

```bash
pytest
ruff check .
ruff format --check .
mypy src
```

Dokładne komendy będą definiowane przez konfigurację repozytorium.

## Commity

Projekt używa Conventional Commits:

```text
feat(runtime): add connection worker
fix(storage): retry failed batch insert
docs(readme): describe local setup
```

Szczegóły znajdują się w `AGENTS.md`.

## Wersjonowanie

Projekt używa Semantic Versioning.

Przed wersją `1.0.0`:

- nowe funkcje i breaking changes zwiększają `MINOR`,
- poprawki zwiększają `PATCH`.

## Changelog

Zmiany trafiają do `CHANGELOG.md` pod sekcję `Unreleased`.

Format jest zgodny z Keep a Changelog.

## Licencja i zależności

Licencja projektu nie została jeszcze ostatecznie wybrana.

Repozytorium ma zawierać:

- plik `LICENSE`,
- `THIRD_PARTY_NOTICES.md`,
- informację o licencji w paczce,
- komendę lub endpoint pokazujący wersję, licencję i creditsy,
- generator creditsów oparty na rzeczywiście zainstalowanych zależnościach.

Licencji bibliotek nie wolno zgadywać. Każda nowa zależność musi zostać zweryfikowana na podstawie oficjalnych metadanych.

## Bezpieczeństwo

Domyślnie API administracyjne powinno nasłuchiwać wyłącznie na localhost.

Repozytorium nie może zawierać:

- haseł,
- certyfikatów prywatnych,
- danych klientów,
- adresów produkcyjnych,
- zrzutów firmowych konfiguracji,
- kodu pochodzącego z innych zamkniętych projektów.

## Roadmap MVP

1. bootstrap repozytorium,
2. modele domenowe,
3. konfiguracja,
4. kontrakt drivera,
5. mock driver,
6. scheduler,
7. connection worker,
8. kolejka i writer,
9. persistence,
10. health API,
11. retry i observability,
12. OPC UA,
13. packaging,
14. licencje i credits,
15. release MVP.

Szczegółowe kryteria znajdują się w `docs/tasks/`.
