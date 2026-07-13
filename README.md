# PLC Gateway

Modularny collector danych przemyslowych napisany w Pythonie.

Celem projektu jest dostarczenie stabilnego runtime'u do cyklicznego odczytu
grup tagow z PLC i serwerow OPC, normalizacji wynikow oraz zapisu do relacyjnej
bazy danych.

Projekt jest rozwijany etapami. Aktualny zakres repozytorium to bootstrap MVP,
bez implementowania logiki komunikacyjnej, schedulera ani warstwy persistence.

## Status

Projekt w fazie implementacji MVP.

Plan prac znajduje sie w katalogu:

```text
docs/tasks/
```

Agent lub developer powinien realizowac wylacznie jeden task naraz.

## Główne założenia

- jeden worker jest wlascicielem jednego polaczenia,
- tagi sa grupowane wedlug zrodla i interwalu,
- komunikacja jest oddzielona od zapisu przez kolejke,
- wszystkie drivery implementuja wspolny kontrakt,
- dane sa normalizowane do wspolnego modelu,
- bledy jednego polaczenia nie zatrzymuja pozostalych,
- retry uzywa exponential backoff i jittera,
- runtime udostepnia health-check i status komponentow,
- aplikacja ma dzialac bez GUI jako dlugotrwaly proces lub usluga.

## Instalacja developerska

Wymagany jest Python 3.12+.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

## Uruchamianie

Minimalna komenda startowa:

```powershell
python -m plc_gateway
```

Po instalacji editable dostepny jest tez skrypt:

```powershell
plc-gateway
```

Aktualny bootstrap wypisuje status aplikacji w JSON i konfiguruje podstawowe
logowanie. Nie uruchamia jeszcze workerow, API administracyjnego ani driverow.

## Modele domenowe

Podstawowe modele domenowe znajduja sie w src/plc_gateway/domain/.

Warstwa domenowa nie importuje FastAPI, SQLAlchemy ani bibliotek protokolow.
Obejmuje konfiguracje polaczen, grup i tagow, typowane wartosci tagow, wyniki
odczytow, wykonania pollingu, status komponentow runtime oraz jawna hierarchie
wyjatkow.

Wszystkie timestampy przekazywane do modeli runtime musza byc timezone-aware i
sa normalizowane do UTC.

## Konfiguracja

Konfiguracja polaczen, grup tagow i tagow jest ladowana z pliku JSON na granicy
aplikacji i mapowana do modeli domenowych po walidacji. Przykladowy plik:

```text
docs/examples/gateway.config.example.json
```

Minimalny ksztalt:

```json
{
  "connections": [
    {
      "id": "opcua_demo",
      "protocol": "opcua",
      "endpoint": "opc.tcp://127.0.0.1:4840",
      "timeout_ms": 5000,
      "protocol_options": {}
    }
  ],
  "tag_groups": [
    {
      "id": "fast",
      "connection_id": "opcua_demo",
      "interval_ms": 1000
    }
  ],
  "tags": [
    {
      "id": "temperature",
      "tag_group_id": "fast",
      "name": "Temperature",
      "address": "ns=2;s=Machine.Temperature",
      "value_type": "numeric"
    }
  ]
}
```

Domyslne wartosci:

- `connections[].enabled`: `true`
- `connections[].timeout_ms`: `3000`
- `connections[].protocol_options`: `{}`
- `tag_groups[].timeout_ms`: `3000`
- `tag_groups[].overlap_policy`: `skip`
- `tag_groups[].enabled`: `true`
- `tags[].enabled`: `true`
- `tags[].metadata`: `{}`

Walidacja odrzuca puste identyfikatory, niepoprawne timeouty i interwaly,
duplikaty identyfikatorow oraz bledne referencje `connection_id` i
`tag_group_id`. Bledy sa zglaszane jako `ConfigurationError` z lokalizacja
problemu, na przyklad `connections.0.timeout_ms`.

Wspierane sa kontrolowane override'y polaczen przez zmienne srodowiskowe:

```powershell
$env:PLC_GATEWAY_CONNECTIONS__OPCUA_DEMO__ENDPOINT = "opc.tcp://localhost:4841"
$env:PLC_GATEWAY_CONNECTIONS__OPCUA_DEMO__TIMEOUT_MS = "7500"
$env:PLC_GATEWAY_CONNECTIONS__OPCUA_DEMO__ENABLED = "false"
```

Dane diagnostyczne nalezy pobierac przez `GatewayConfig.safe_for_logging()`.
Maskuje ono sekrety w `protocol_options`, `metadata` i dane uwierzytelniajace w
endpointach, zeby hasla, tokeny i klucze nie trafialy do logow.

## Kontrakt driverow

Wspolny kontrakt driverow znajduje sie w `src/plc_gateway/protocols/`.
Runtime tworzy drivery przez `DriverRegistry`, podajac zwalidowany
`ConnectionConfig`. Registry mapuje nazwe protokolu na factory drivera i nie
zna szczegolow bibliotek protokolow.

Minimalny driver implementuje `CommunicationDriver`:

```python
class CommunicationDriver(Protocol):
    @property
    def capabilities(self) -> DriverCapabilities: ...
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def read(self, tags: Sequence[TagRequest]) -> list[TagResult]: ...
    async def health_check(self) -> bool: ...
```

Zasady kontraktu:

- jeden driver jest tworzony dla jednego `ConnectionConfig` i jednego workera,
- `read()` przyjmuje batch tagow i zwraca wyniki w modelach domenowych,
- `connect()`, `disconnect()` i `health_check()` honoruja timeout polaczenia,
- `read()` honoruje `TagRequest.timeout_ms`, jezeli zostal ustawiony,
- implementacje nie moga ukrywac `asyncio.CancelledError`,
- nieznany protokol konczy sie `ConfigurationError` przed startem runtime.

## Mock driver

Deterministyczny driver `mock` znajduje sie w `plc_gateway.protocols.mock`.
Sluzy do lokalnego developmentu oraz testow schedulera i retry bez realnego PLC
lub serwera OPC. Factory `create_mock_driver` mozna zarejestrowac w
`DriverRegistry` dla protokolu `mock`.

Opcje sa przekazywane przez `connections[].protocol_options`:

```json
{
  "delay_ms": 250,
  "connect_failures_before_success": 1,
  "timeout_on_read": false,
  "timeout_on_connect": false,
  "tags": {
    "temperature": {"value": 20.5},
    "counter": {"sequence": [1, 2, 3]},
    "broken": {
      "error_code": "mock_bad_tag",
      "error_message": "Configured tag failure"
    }
  }
}
```

Zachowanie:

- brak konfiguracji taga zwraca domyslna wartosc zgodna z `ValueType`,
- `value` zwraca stala wartosc,
- `sequence` zwraca kolejne wartosci cyklicznie,
- `delay_ms` uzywa wstrzykiwanego async sleepera, wiec testy moga unikac
  realnego czekania,
- timeout jest zglaszany jako `TransientCommunicationError`, gdy `delay_ms`
  przekracza timeout polaczenia lub najnizszy timeout taga w batchu,
- `connect_failures_before_success` pozwala zasymulowac awarie i odzyskanie
  polaczenia,
- blad pojedynczego taga zwraca `TagResult.failure()` bez przerywania calego
  batcha.

## Scheduler grup odczytowych

`PollScheduler` w `plc_gateway.runtime` uruchamia wlaczone `TagGroupConfig`
zgodnie z `interval_ms`. Scheduler uzywa monotonic clock i wylicza kolejne
terminy przez dodawanie interwalu do poprzedniego terminu, wiec dlugi cykl lub
opoznienie petli nie przesuwa trwale harmonogramu jak `sleep(interval)` po
zakonczeniu kazdego cyklu.

Zachowanie:

- pierwsze uruchomienie grupy jest zaplanowane natychmiast po starcie,
- aktualnie wspierana polityka overlap to `skip`,
- dla kazdej grupy istnieje najwyzej jeden aktywny task pollingu,
- gdy termin wypada podczas aktywnego cyklu, scheduler zwieksza
  `missed_cycles` i nie tworzy kolejki zaleglych cykli,
- `stop()` zatrzymuje petle schedulera bez anulowania aktywnego cyklu,
- `cancel_active_cycles()` anuluje aktywne cykle i czeka na ich zakonczenie,
- `snapshot()` udostepnia stan grup: uruchomione, pominiete i nieudane cykle
  oraz ostatni blad handlera.

## Connection worker

`ConnectionWorker` w `plc_gateway.runtime` jest wlascicielem jednego
`ConnectionConfig` i jednej instancji drivera utworzonej przez `DriverRegistry`.
Nie wspoldzieli klienta protokolu z innymi workerami.

Lifecycle:

- `connect()` ustawia stan `starting`, laczy driver i przechodzi do `running`,
- blad polaczenia ustawia `failed`, ale ten sam worker moze ponowic
  `connect()`, jezeli driver jest gotowy do reconnect,
- `poll_group()` buduje `TagRequest` z tagow grupy i zwraca `WorkerPollResult`
  zawierajacy `PollExecution` oraz wyniki tagow,
- czesciowe bledy tagow daja status `partial_failure` i stan workera
  `degraded`,
- awaria batch read jest izolowana do wynikow blednych dla tagow z tej grupy i
  nie zatrzymuje innych workerow,
- `health_check()` aktualizuje obserwowalny stan workera,
- `heartbeat()` odswieza timestamp statusu,
- anulowanie `connect()`, `poll_group()` lub `health_check()` wykonuje
  best-effort `disconnect()` i propaguje `asyncio.CancelledError`.

## Kolejka odczytow

`ReadingQueue` w `plc_gateway.runtime` oddziela odczyty protokolow od
pozniejszego zapisu do storage. Przenosi `WorkerPollResult`, czyli wynik
pollingu workera wraz z `PollExecution` i wynikami tagow.

Zachowanie:

- kolejka jest zawsze ograniczona przez `max_size`,
- producent uzywa `put()` i czeka na wolne miejsce maksymalnie
  `put_timeout_s`,
- przekroczenie timeoutu zglasza `StorageError` z kodem `reading_queue_full`,
- `metrics()` zwraca rozmiar, zajetosc, liczbe timeoutow i liczbe jawnie
  odrzuconych elementow,
- konsument uzywa `get()`, a po zapisie wywoluje `task_done()`,
- `join()` pozwala czekac na przetworzenie wszystkich pobranych elementow,
- `close()` zamyka producentow bez usuwania oczekujacych elementow,
- `drop_pending()` zamyka kolejke i zwraca jawny status liczby odrzuconych
  elementow. Spool dyskowy pozostaje poza tym etapem.

## Jakość

Kazda zmiana powinna przechodzic:

```powershell
pytest
ruff check .
ruff format --check .
mypy src tests
```

Te same komendy uruchamia GitHub Actions.

## Architektura docelowa

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

## Commity

Projekt uzywa Conventional Commits:

```text
feat(runtime): add connection worker
fix(storage): retry failed batch insert
docs(readme): describe local setup
```

Szczegoly znajduja sie w `AGENTS.md`.

## Changelog

Zmiany trafiaja do `CHANGELOG.md` pod sekcje `Unreleased`.

Format jest zgodny z Keep a Changelog.

## Licencja i zależności

Licencja projektu nie zostala jeszcze ostatecznie wybrana. Do czasu wyboru
licencji kod nie powinien byc publicznie dystrybuowany jako open source.

Licencji bibliotek nie wolno zgadywac. Kazda nowa zaleznosc musi zostac
zweryfikowana na podstawie oficjalnych metadanych.

## Bezpieczeństwo

Repozytorium nie moze zawierac:

- hasel,
- certyfikatow prywatnych,
- danych klientow,
- adresow produkcyjnych,
- zrzutow firmowych konfiguracji,
- kodu pochodzacego z innych zamknietych projektow.


