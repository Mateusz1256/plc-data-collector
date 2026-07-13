# Task 15: Licencja, credits i informacje o buildzie

Status: done

## Cel

Dodać poprawną obsługę licencji projektu i zależności.

## Zakres

- wybór licencji projektu przez właściciela,
- plik `LICENSE`,
- opcjonalny `NOTICE`,
- SPDX metadata,
- generator creditsów,
- zweryfikowana mapa licencji zależności,
- `plc-gateway licenses`,
- `/api/about`,
- commit SHA i build version,
- dołączenie creditsów do paczki.

## Zasady

- nie zgaduj licencji,
- weryfikuj dokładne wersje,
- nie usuwaj wymaganych notice files,
- build ma się nie udać, jeśli runtime dependency nie ma zweryfikowanej licencji.

## Kryteria akceptacji

- dystrybucja zawiera pełne informacje licencyjne,
- creditsy odpowiadają lockfile,
- brak zależności o nieustalonej licencji,
- CLI i API pokazują wersję oraz licencję.

## Proponowany commit

```text
build(license): add verified dependency credits
```
