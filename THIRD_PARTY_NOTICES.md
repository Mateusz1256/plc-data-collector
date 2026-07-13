# Third-Party Notices

Ten plik zawiera informacje o bibliotekach i narzedziach uzywanych przez
projekt.

Nie wpisuj licencji na podstawie pamieci, bloga ani przypadkowego badge'a.
Kazda pozycja musi zostac zweryfikowana w oficjalnych metadanych pakietu lub
repozytorium uzywanej wersji.

## Format Wpisu

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

## Runtime Dependencies

The following packages are installed by `python -m pip install -e .` for runtime
configuration validation. Direct dependencies are declared in `pyproject.toml`;
transitive dependencies are listed from installed package metadata and should be
regenerated from a lockfile once dependency locking is introduced.

### annotated-types

- Version: 0.7.0
- Purpose: transitive Pydantic dependency for reusable validation constraints
- License: MIT
- Source: https://pypi.org/project/annotated-types/0.7.0/
- Copyright notice: See package license file.
- Bundled in distribution: yes
- Notes: installed transitively by Pydantic. License verified from package
  metadata classifier `License :: OSI Approved :: MIT License`.

### alembic

- Version: 1.17.1
- Purpose: database migrations for the relational persistence schema
- License: MIT
- Source: https://pypi.org/project/alembic/1.17.1/
- Copyright notice: See package license file.
- Bundled in distribution: yes
- Notes: declared directly as a runtime dependency. License verified from
  package metadata field `License-Expression: MIT`.

### greenlet

- Version: 3.2.2
- Purpose: transitive SQLAlchemy dependency
- License: MIT AND Python-2.0
- Source: https://pypi.org/project/greenlet/3.2.2/
- Copyright notice: See package license file.
- Bundled in distribution: yes
- Notes: installed transitively by SQLAlchemy. License verified from package
  metadata field `License: MIT AND Python-2.0`.

### Mako

- Version: 1.3.10
- Purpose: transitive Alembic dependency for migration templates
- License: MIT
- Source: https://pypi.org/project/Mako/1.3.10/
- Copyright notice: See package license file.
- Bundled in distribution: yes
- Notes: installed transitively by Alembic. License verified from package
  metadata field `License: MIT`.

### MarkupSafe

- Version: 3.0.3
- Purpose: transitive Mako dependency for safe markup handling
- License: BSD-3-Clause
- Source: https://pypi.org/project/MarkupSafe/3.0.3/
- Copyright notice: See package license file.
- Bundled in distribution: yes
- Notes: installed transitively by Mako. License verified from package metadata
  field `License-Expression: BSD-3-Clause`.

### pydantic

- Version: 2.11.3
- Purpose: application configuration validation at process boundaries
- License: MIT
- Source: https://pypi.org/project/pydantic/2.11.3/
- Copyright notice: See package license file.
- Bundled in distribution: yes
- Notes: declared directly as a runtime dependency. License verified from
  package metadata field `License-Expression: MIT`.

### pydantic_core

- Version: 2.33.1
- Purpose: transitive Pydantic validation engine
- License: MIT
- Source: https://pypi.org/project/pydantic-core/2.33.1/
- Copyright notice: See package license file.
- Bundled in distribution: yes
- Notes: installed transitively by Pydantic. License verified from package
  metadata field `License: MIT`.

### SQLAlchemy

- Version: 2.0.44
- Purpose: relational schema and repository implementation
- License: MIT
- Source: https://pypi.org/project/SQLAlchemy/2.0.44/
- Copyright notice: See package license file.
- Bundled in distribution: yes
- Notes: declared directly as a runtime dependency. License verified from
  package metadata field `License: MIT`.

### typing-inspection

- Version: 0.4.0
- Purpose: transitive Pydantic dependency for runtime typing introspection
- License: MIT
- Source: https://pypi.org/project/typing-inspection/0.4.0/
- Copyright notice: See package license file.
- Bundled in distribution: yes
- Notes: installed transitively by Pydantic. License verified from package
  metadata classifier `License :: OSI Approved :: MIT License`.

### typing_extensions

- Version: 4.13.2
- Purpose: transitive Pydantic dependency for typing features
- License: PSF-2.0
- Source: https://pypi.org/project/typing-extensions/4.13.2/
- Copyright notice: See package license file.
- Bundled in distribution: yes
- Notes: installed transitively by Pydantic. License verified from package
  metadata field `License-Expression: PSF-2.0`.

## Development Dependencies

The following packages are installed by `python -m pip install -e ".[dev]"` in
the bootstrap development environment. Direct dependencies are declared in
`pyproject.toml`; transitive dependencies are listed from installed package
metadata and should be regenerated from a lockfile once dependency locking is
introduced.

### ast_serialize

- Version: 0.6.0
- Purpose: transitive mypy dependency
- License: MIT
- Source: https://pypi.org/project/ast-serialize/0.6.0/
- Copyright notice: See package license file.
- Bundled in distribution: no
- Notes: installed transitively in the `dev` extra.

### colorama

- Version: 0.4.6
- Purpose: transitive pytest dependency on Windows
- License: BSD-3-Clause
- Source: https://pypi.org/project/colorama/0.4.6/
- Copyright notice: Copyright (c) 2010 Jonathan Hartley.
- Bundled in distribution: no
- Notes: installed transitively in the `dev` extra.

### iniconfig

- Version: 2.3.0
- Purpose: transitive pytest dependency
- License: MIT
- Source: https://pypi.org/project/iniconfig/2.3.0/
- Copyright notice: See package license file.
- Bundled in distribution: no
- Notes: installed transitively in the `dev` extra.

### librt

- Version: 0.13.0
- Purpose: transitive mypy dependency
- License: MIT
- Source: https://pypi.org/project/librt/0.13.0/
- Copyright notice: See package license file.
- Bundled in distribution: no
- Notes: installed transitively in the `dev` extra.

### pytest

- Version: 9.1.1
- Purpose: test runner
- License: MIT
- Source: https://pypi.org/project/pytest/9.1.1/
- Copyright notice: Copyright Holger Krekel and others, 2004.
- Bundled in distribution: no
- Notes: declared directly in the `dev` extra.

### pytest-asyncio

- Version: 1.4.0
- Purpose: pytest support for asyncio tests
- License: Apache-2.0
- Source: https://pypi.org/project/pytest-asyncio/1.4.0/
- Copyright notice: See package license file.
- Bundled in distribution: no
- Notes: declared directly in the `dev` extra.

### ruff

- Version: 0.15.21
- Purpose: linting and formatting checks
- License: MIT
- Source: https://pypi.org/project/ruff/0.15.21/
- Copyright notice: See package license file.
- Bundled in distribution: no
- Notes: declared directly in the `dev` extra.

### mypy

- Version: 2.2.0
- Purpose: static type checking
- License: MIT
- Source: https://pypi.org/project/mypy/2.2.0/
- Copyright notice: See package license file.
- Bundled in distribution: no
- Notes: declared directly in the `dev` extra.

### mypy_extensions

- Version: 1.1.0
- Purpose: transitive mypy dependency
- License: MIT
- Source: https://pypi.org/project/mypy-extensions/1.1.0/
- Copyright notice: See package license file.
- Bundled in distribution: no
- Notes: installed transitively in the `dev` extra.

### packaging

- Version: 26.2
- Purpose: transitive pytest dependency
- License: Apache-2.0 OR BSD-2-Clause
- Source: https://pypi.org/project/packaging/26.2/
- Copyright notice: See package license file.
- Bundled in distribution: no
- Notes: installed transitively in the `dev` extra.

### pathspec

- Version: 1.1.1
- Purpose: transitive mypy dependency
- License: MPL-2.0
- Source: https://pypi.org/project/pathspec/1.1.1/
- Copyright notice: See package license file.
- Bundled in distribution: no
- Notes: installed transitively in the `dev` extra.

### pluggy

- Version: 1.6.0
- Purpose: transitive pytest dependency
- License: MIT
- Source: https://pypi.org/project/pluggy/1.6.0/
- Copyright notice: See package license file.
- Bundled in distribution: no
- Notes: installed transitively in the `dev` extra.

### Pygments

- Version: 2.20.0
- Purpose: transitive pytest dependency for terminal output
- License: BSD-2-Clause
- Source: https://pypi.org/project/Pygments/2.20.0/
- Copyright notice: See package license file.
- Bundled in distribution: no
- Notes: installed transitively in the `dev` extra.

### typing_extensions

- Version: 4.16.0
- Purpose: transitive mypy dependency
- License: PSF-2.0
- Source: https://pypi.org/project/typing-extensions/4.16.0/
- Copyright notice: See package license file.
- Bundled in distribution: no
- Notes: installed transitively in the `dev` extra.

## Credits Generation

Docelowo projekt powinien posiadac skrypt, ktory:

1. odczytuje dokladne wersje z lockfile lub aktywnego srodowiska,
2. generuje liste zaleznosci runtime,
3. laczy je z recznie zweryfikowana mapa licencji,
4. przerywa dzialanie przy brakujacej lub niejednoznacznej licencji,
5. generuje plik creditsow do dystrybucji,
6. nie nadpisuje wymaganych informacji copyright.
