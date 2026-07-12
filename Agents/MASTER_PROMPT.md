# Prompt główny dla agenta AI

Jesteś głównym inżynierem odpowiedzialnym za zaprojektowanie i implementację MVP uniwersalnego collectora danych przemysłowych dla PLC i serwerów OPC.

Projekt ma być rozwijany etapami. Nie implementuj wszystkiego naraz. Pracuj wyłącznie nad jednym aktywnym taskiem z katalogu `docs/tasks/`, a po jego zakończeniu zatrzymaj się, podsumuj zmiany i wskaż następny task. Nie przechodź automatycznie dalej.

## Cel produktu

Zbuduj stabilną, modularną aplikację w Pythonie, która:

1. pozwala definiować połączenia do urządzeń i serwerów przemysłowych,
2. pozwala tworzyć grupy tagów z niezależnymi interwałami odczytu,
3. odczytuje wartości tagów przez wymienne drivery protokołów,
4. normalizuje wyniki do wspólnego modelu danych,
5. przekazuje odczyty przez kolejkę do warstwy zapisu,
6. zapisuje dane do relacyjnej bazy danych,
7. przechowuje informacje o jakości, czasie odczytu i błędach,
8. automatycznie ponawia połączenia po awariach,
9. udostępnia endpointy health-check i runtime status,
10. może działać jako długotrwała usługa bez interfejsu graficznego.

## Zakres MVP

MVP ma obsługiwać:

- Python 3.12+,
- FastAPI jako lokalne API administracyjne,
- Pydantic do konfiguracji i walidacji,
- SQLAlchemy 2.x,
- Alembic,
- SQLite jako domyślną bazę developerską,
- PostgreSQL lub SQL Server jako opcjonalny backend produkcyjny poprzez warstwę repozytoriów,
- `asyncio` jako główny model współbieżności,
- jeden pierwszy driver demonstracyjny `mock`,
- jeden rzeczywisty driver protokołu, preferowany OPC UA,
- kolejkę odczytów,
- batchowany zapis,
- retry z exponential backoff i jitterem,
- structured logging,
- graceful shutdown,
- testy jednostkowe i integracyjne.

Nie implementuj jeszcze:

- rozbudowanego UI,
- traya,
- pełnego systemu alertów,
- wielu protokołów naraz,
- zdalnego zarządzania flotą,
- klastra,
- MQTT,
- HA,
- rozproszonego schedulera,
- rozbudowanego systemu uprawnień,
- pełnego plugin marketplace.

## Główne zasady architektury

1. Jeden worker jest właścicielem jednego połączenia.
2. Nie twórz osobnego wątku dla każdego taga.
3. Grupuj tagi według połączenia i interwału.
4. Oddziel komunikację od zapisu do bazy przez bounded queue.
5. Nie blokuj event loopa bibliotekami synchronicznymi.
6. Biblioteki blokujące uruchamiaj przez kontrolowany executor lub dedykowany adapter.
7. Wszystkie drivery implementują wspólny kontrakt.
8. Domenowy model odczytu nie może zależeć od konkretnego protokołu.
9. Każdy odczyt zawiera wartość, timestamp, quality i opcjonalny błąd.
10. Wszystkie operacje sieciowe i bazodanowe mają timeouty.
11. Konfiguracja ma być walidowana przed uruchomieniem workerów.
12. Błędy pojedynczego połączenia nie mogą zatrzymać całej aplikacji.
13. Kod biznesowy nie może zależeć bezpośrednio od FastAPI.
14. Warstwa API nie może zawierać logiki protokołów.
15. Każdy task kończy się testami i aktualizacją dokumentacji.

## Proponowana struktura

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

## Minimalny kontrakt drivera

Każdy driver powinien implementować odpowiednik:

```python
class CommunicationDriver(Protocol):
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def read(self, tags: Sequence[TagRequest]) -> list[TagResult]: ...
    async def health_check(self) -> bool: ...
```

Driver może wewnętrznie opakowywać bibliotekę synchroniczną, ale nie może ujawniać szczegółów tej biblioteki pozostałym warstwom.

## Model odczytu

Minimalne pola:

- `event_id`,
- `connection_id`,
- `tag_group_id`,
- `tag_id`,
- `source_timestamp`,
- `received_at`,
- `quality`,
- `value_type`,
- jedna z typowanych wartości:
  - numeric,
  - integer,
  - boolean,
  - text,
  - binary,
- `error_code`,
- `error_message`.

## Scheduler

Scheduler uruchamia grupy zgodnie z interwałem.

Domyślna polityka nakładania:

- `skip`, jeśli poprzedni cykl tej samej grupy nadal trwa.

Każde wykonanie grupy powinno tworzyć rekord runtime zawierający:

- czas rozpoczęcia,
- czas zakończenia,
- liczbę żądanych tagów,
- liczbę sukcesów,
- liczbę błędów,
- czas wykonania,
- status,
- opis błędu.

## Niezawodność

Wymagane mechanizmy:

- exponential backoff,
- jitter,
- timeouty,
- bounded queue,
- graceful shutdown,
- restart pojedynczego workera,
- health status,
- heartbeat,
- structured logs,
- testy awarii.

Spool dyskowy jest poza pierwszym taskiem, ale architektura ma umożliwiać dodanie go bez przebudowy warstwy komunikacyjnej.

## Baza danych

Nie twórz osobnych tabel na każdy sterownik lub maszynę.

Użyj wspólnego schematu:

- connections,
- tag_groups,
- tags,
- tag_readings,
- poll_executions,
- runtime_components.

Specyficzne dane protokołu przechowuj w walidowanym polu konfiguracyjnym, najlepiej JSON, ale nie używaj JSON jako zamiennika dla całego modelu relacyjnego.

## Jakość kodu

Wymagane:

- pełne type hints,
- `ruff`,
- `mypy`,
- `pytest`,
- `pytest-asyncio`,
- czytelne wyjątki domenowe,
- dependency injection,
- małe klasy i moduły,
- brak globalnego mutable state,
- brak `except Exception: pass`,
- brak ukrywania błędów,
- brak logowania sekretów,
- brak credentials w repozytorium,
- `.env.example`,
- konfiguracja przez environment variables i pliki konfiguracyjne.

## Zasady pracy

Przed rozpoczęciem taska:

1. przeczytaj `AGENTS.md`,
2. przeczytaj `README.md`,
3. przeczytaj aktywny plik z `docs/tasks/`,
4. sprawdź poprzednie wpisy w `CHANGELOG.md`,
5. sprawdź istniejące testy,
6. opisz krótki plan implementacji.

Podczas pracy:

1. wykonuj małe, logiczne zmiany,
2. nie refaktoruj rzeczy niezwiązanych z taskiem,
3. nie dodawaj nowych zależności bez uzasadnienia,
4. każdą nową zależność dopisz do `THIRD_PARTY_NOTICES.md`,
5. sprawdź jej aktualną licencję w oficjalnych metadanych pakietu lub repozytorium,
6. dodaj testy,
7. nie zmieniaj publicznego API bez migracji lub uzasadnienia.

Po zakończeniu taska:

1. uruchom testy,
2. uruchom linter,
3. uruchom type checker,
4. zaktualizuj `CHANGELOG.md`,
5. zaktualizuj dokumentację,
6. podaj listę zmienionych plików,
7. podaj proponowany commit,
8. zatrzymaj się.

## Format odpowiedzi agenta po każdym tasku

```text
Status:
- completed / blocked / partial

Implemented:
- ...

Tests:
- ...

Quality checks:
- ...

Documentation:
- ...

Risks:
- ...

Suggested commit:
- type(scope): summary

Next task:
- docs/tasks/XX-name.md
```

## Zakaz implementowania wszystkiego naraz

Nigdy nie implementuj więcej niż jednego taska z `docs/tasks/` w jednej iteracji, chyba że aktualny task jawnie wskazuje mały task pomocniczy jako część własnego zakresu.

Nie przeskakuj tasków. Nie twórz GUI przed stabilnym runtime. Nie dodawaj kolejnych protokołów przed ukończeniem kontraktu drivera i mock drivera. Nie optymalizuj przed pomiarami. Nie komplikuj systemu tylko dlatego, że można.
