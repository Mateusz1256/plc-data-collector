# Task 00: Bootstrap repozytorium

Status: done

## Cel

UtworzyÄ‡ minimalny, uruchamialny szkielet projektu bez implementowania logiki komunikacyjnej.

## Zakres

- konfiguracja `pyproject.toml`,
- ukĹ‚ad `src/`,
- pakiet `plc_gateway`,
- podstawowa komenda startowa,
- pytest,
- pytest-asyncio,
- ruff,
- mypy,
- `.gitignore`,
- `.env.example`,
- podstawowy logging,
- CI uruchamiajÄ…ce testy, lint i type check,
- placeholder wersji aplikacji.

## Poza zakresem

- FastAPI endpoints poza minimalnym `/health`,
- drivery,
- scheduler,
- baza danych,
- Alembic,
- UI,
- usĹ‚uga Windows.

## Kryteria akceptacji

- projekt instaluje siÄ™ w trybie editable,
- `python -m plc_gateway` dziaĹ‚a,
- test przykĹ‚adowy przechodzi,
- ruff przechodzi,
- mypy przechodzi,
- CI jest skonfigurowane,
- brak sekretĂłw i danych Ĺ›rodowiskowych w repo.

## Testy

- import pakietu,
- odczyt wersji,
- uruchomienie minimalnej funkcji aplikacji.

## Dokumentacja

- uzupeĹ‚nij sekcjÄ™ instalacji w `README.md`,
- dodaj wpis do `CHANGELOG.md`.

## Proponowany commit

```text
chore(project): bootstrap Python package and quality tooling
```

