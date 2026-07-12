# Task 07: Kolejka odczytów

## Cel

Oddzielić odczyt protokołu od zapisu do storage.

## Zakres

- bounded asyncio queue,
- konfiguracja rozmiaru,
- metryka zajętości,
- jawna polityka po zapełnieniu,
- bezpieczne zamykanie,
- kontrakt konsumenta.

## Domyślna polityka MVP

Producent czeka z timeoutem. Przekroczenie timeoutu generuje jawny błąd i zdarzenie runtime.

Spool zostanie dodany później.

## Kryteria akceptacji

- kolejka jest ograniczona,
- nie ma nieograniczonego wzrostu RAM,
- shutdown nie gubi elementów bez jawnego statusu,
- testy pokrywają pełną kolejkę.

## Proponowany commit

```text
feat(runtime): add bounded reading queue
```
