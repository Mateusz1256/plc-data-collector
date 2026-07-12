# License Policy

## Status

Licencja projektu nie została jeszcze wybrana.

Do czasu wyboru licencji kod nie powinien być publicznie dystrybuowany jako open source.

## Candidate licenses

Do rozważenia:

- Apache-2.0,
- MIT,
- MPL-2.0,
- licencja komercyjna lub dual licensing.

Wybór powinien uwzględniać:

- możliwość użycia komercyjnego,
- wymagania dotyczące patentów,
- obowiązki dotyczące modyfikacji,
- dystrybucję wersji binarnych,
- kompatybilność z zależnościami,
- możliwość użycia przez pracodawców i klientów.

## Required repository files

Po wyborze licencji repozytorium powinno zawierać:

- `LICENSE`,
- `THIRD_PARTY_NOTICES.md`,
- opcjonalny `NOTICE`,
- SPDX identifier w metadanych projektu,
- informację o licencji w dokumentacji,
- creditsy w dystrybucji.

## Source file wrapper

Po wyborze licencji źródła mogą otrzymać krótki nagłówek:

```python
# SPDX-License-Identifier: Apache-2.0
```

Nie należy kopiować pełnego tekstu licencji do każdego pliku.

## Runtime license information

Aplikacja powinna docelowo udostępniać:

```text
plc-gateway --version
plc-gateway licenses
```

oraz endpoint:

```text
GET /api/about
```

Zwracane dane:

- nazwa aplikacji,
- wersja,
- commit SHA, jeśli dostępny,
- licencja projektu,
- lokalizacja pełnego tekstu licencji,
- lista zależności i creditsów.

## Dependency rules

Nowa zależność może zostać dodana dopiero po:

1. weryfikacji licencji,
2. ocenie kompatybilności,
3. dopisaniu jej do `THIRD_PARTY_NOTICES.md`,
4. zachowaniu wymaganych notice files,
5. potwierdzeniu, że wersja paczki odpowiada zweryfikowanej licencji.
