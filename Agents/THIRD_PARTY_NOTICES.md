# Third-Party Notices

Ten plik zawiera informacje o bibliotekach i narzędziach używanych przez projekt.

Nie wpisuj licencji na podstawie pamięci, bloga ani przypadkowego badge'a. Każda pozycja musi zostać zweryfikowana w oficjalnych metadanych pakietu lub repozytorium używanej wersji.

## Format wpisu

```text
### Package name

- Version: x.y.z
- Purpose: short description
- License: SPDX identifier or exact license name
- Source: official package or repository location
- Copyright notice: required notice
- Bundled in distribution: yes/no
- Notes: obligations, exceptions, linking requirements
```

## Runtime dependencies

Brak zatwierdzonych zależności runtime na obecnym etapie.

## Development dependencies

Brak zatwierdzonych zależności developerskich na obecnym etapie.

## Credits generation

Docelowo projekt powinien posiadać skrypt, który:

1. odczytuje dokładne wersje z lockfile lub aktywnego środowiska,
2. generuje listę zależności runtime,
3. łączy je z ręcznie zweryfikowaną mapą licencji,
4. przerywa działanie przy brakującej lub niejednoznacznej licencji,
5. generuje plik creditsów do dystrybucji,
6. nie nadpisuje wymaganych informacji copyright.
