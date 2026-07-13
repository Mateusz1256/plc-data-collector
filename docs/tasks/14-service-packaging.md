# Task 14: Packaging i uruchamianie jako usługa

Status: done

## Cel

Przygotować stabilne uruchamianie aplikacji jako procesu długotrwałego.

## Zakres

- entrypoint CLI,
- konfiguracja ścieżek danych,
- log directory,
- PID i shutdown,
- przykład systemd,
- przykład WinSW lub NSSM,
- opcjonalne packaging binary,
- dokumentacja aktualizacji.

## Zasady

- core aplikacji nie może zależeć od Windows Service API,
- wrapper usługi jest adapterem wdrożeniowym,
- dane runtime nie trafiają do katalogu instalacyjnego.

## Kryteria akceptacji

- proces uruchamia się bez IDE,
- poprawnie reaguje na SIGTERM lub odpowiednik,
- ścieżki są konfigurowalne,
- dokumentacja opisuje instalację usługi.

## Proponowany commit

```text
build(service): add production process packaging
```
