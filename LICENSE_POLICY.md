# License Policy

## Project License

PLC Data Collector is released under the MIT License (SPDX identifier `MIT`).
The full text is in `LICENSE`, and the copyright holder is Mateusz Prucnal.

## Acceptable Dependency Licenses

A runtime dependency must be compatible with distributing the project under the
MIT License. Accepted without further discussion:

- MIT,
- BSD-2-Clause and BSD-3-Clause,
- Apache-2.0,
- ISC,
- PSF-2.0,
- dual licenses where at least one option is in this list.

Needs an explicit decision before it is added:

- weak copyleft licenses such as MPL-2.0 or LGPL (for example, packages that
  bundle binary libraries),
- licenses with unusual patent, field-of-use or attribution terms.

Not acceptable:

- strong copyleft licenses such as GPL and AGPL,
- source-available, non-commercial or proprietary licenses.

Example: the PostgreSQL driver is `pg8000` (BSD-3-Clause, pure Python) rather
than `psycopg`, which is LGPL and bundles binary libraries.

## Required Repository Files

The repository contains:

- `LICENSE`, the MIT license text,
- `NOTICE`, the project and third-party notice,
- `THIRD_PARTY_NOTICES.md`, verified notices for every runtime dependency,
- `license = "MIT"` in the project metadata,
- bundled credits in the distribution
  (`plc_data_collector/license_data/credits.json`).

## Runtime License Information

The application exposes license information through:

```text
plc-data-collector --version
plc-data-collector licenses
```

and the endpoint:

```text
GET /api/about
```

## Dependency Rules

A new dependency (direct or transitive) may be added only after:

1. verifying its license from the official package metadata or repository,
2. confirming it is acceptable under this policy,
3. adding it to `THIRD_PARTY_NOTICES.md`,
4. preserving any required notice files,
5. confirming that the pinned package version matches the verified license,
6. regenerating the bundled credits with `python tools/generate_credits.py`.
