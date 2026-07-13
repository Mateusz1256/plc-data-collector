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


