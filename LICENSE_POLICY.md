# License Policy

## Status

Projekt ma obecnie status `LicenseRef-PLC-Gateway-Proprietary`.

Nie jest to licencja open source. Kod nie powinien byc publicznie
dystrybuowany, sublicencjonowany ani hostowany bez osobnej pisemnej zgody
wlasciciela projektu.

## Candidate Licenses For Future Review

Do rozwazenia:

- Apache-2.0,
- MIT,
- MPL-2.0,
- licencja komercyjna lub dual licensing.

Wybor powinien uwzgledniac:

- mozliwosc uzycia komercyjnego,
- wymagania dotyczace patentow,
- obowiazki dotyczace modyfikacji,
- dystrybucje wersji binarnych,
- kompatybilnosc z zaleznosciami,
- mozliwosc uzycia przez pracodawcow i klientow.

## Required Repository Files

Repozytorium zawiera:

- `LICENSE`,
- `THIRD_PARTY_NOTICES.md`,
- `NOTICE`,
- SPDX-compatible `LicenseRef-PLC-Gateway-Proprietary` w metadanych projektu,
- informacje o licencji w dokumentacji,
- creditsy w dystrybucji.

## Runtime License Information

Aplikacja powinna docelowo udostepniac:

```text
plc-gateway --version
plc-gateway licenses
```

oraz endpoint:

```text
GET /api/about
```

## Dependency Rules

Nowa zaleznosc moze zostac dodana dopiero po:

1. weryfikacji licencji,
2. ocenie kompatybilnosci,
3. dopisaniu jej do `THIRD_PARTY_NOTICES.md`,
4. zachowaniu wymaganych notice files,
5. potwierdzeniu, ze wersja paczki odpowiada zweryfikowanej licencji.
