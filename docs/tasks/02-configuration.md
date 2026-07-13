# Task 02: Konfiguracja

Status: done

## Cel

Dodać ładowanie i walidację konfiguracji połączeń, grup i tagów.

## Zakres

- modele Pydantic na granicy aplikacji,
- ładowanie YAML lub JSON,
- environment variable overrides,
- walidacja referencji connection -> group -> tag,
- maskowanie sekretów,
- przykładowa konfiguracja,
- błędy z czytelną lokalizacją problemu.

## Poza zakresem

- hot reload,
- zapis konfiguracji przez API,
- szyfrowany secret store.

## Kryteria akceptacji

- poprawna konfiguracja ładuje się do modeli domenowych,
- błędne referencje są odrzucane,
- sekrety nie pojawiają się w logach,
- przykładowy plik jest opisany w README.

## Proponowany commit

```text
feat(config): add validated connection and tag configuration
```
