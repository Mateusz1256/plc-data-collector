# Third-Party Notices

This file lists the libraries and tools used by the project.

Do not record a license from memory, a blog post or a random badge. Every entry
must be verified against the official package metadata or repository of the
version in use.

## Entry Format

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
configuration validation, protocol communication, persistence, and the health
API. Direct dependencies are declared in `pyproject.toml`; transitive
dependencies are listed from installed package metadata and should be
regenerated from a lockfile once dependency locking is introduced.

### aiosqlite

- Version: 0.22.1
- Purpose: transitive asyncua dependency for asynchronous SQLite access used by
  opcua-asyncio internals
- License: MIT
- Source: https://pypi.org/project/aiosqlite/0.22.1/
- Copyright notice: Copyright Amethyst Reese. See package license file.
- Bundled in distribution: yes
- Notes: installed transitively by asyncua. License verified from package
  metadata classifier `License :: OSI Approved :: MIT License`.

### annotated-types

- Version: 0.8.0
- Purpose: transitive Pydantic dependency for reusable validation constraints
- License: MIT
- Source: https://pypi.org/project/annotated-types/0.8.0/
- Copyright notice: See package license file.
- Bundled in distribution: yes
- Notes: installed transitively by Pydantic. License verified from package
  metadata classifier `License :: OSI Approved :: MIT License`.

### annotated-doc

- Version: 0.0.6
- Purpose: transitive FastAPI dependency for annotated documentation metadata
- License: MIT
- Source: https://pypi.org/project/annotated-doc/0.0.6/
- Copyright notice: See package license file.
- Bundled in distribution: yes
- Notes: installed transitively by FastAPI. License verified from package
  metadata field `License-Expression: MIT`.

### asn1crypto

- Version: 1.5.1
- Purpose: transitive scramp dependency for ASN.1 parsing
- License: MIT
- Source: https://pypi.org/project/asn1crypto/1.5.1/
- Copyright notice: Copyright (c) 2015-2022 Will Bond. See package license file.
- Bundled in distribution: yes
- Notes: installed transitively by scramp. License verified from package metadata classifier `License :: OSI Approved :: MIT License`.

### asyncua

- Version: 2.0.1
- Purpose: asynchronous OPC UA client used by the `opcua` protocol driver
- License: GNU Lesser General Public License v3 or later
- Source: https://pypi.org/project/asyncua/2.0.1/
- Copyright notice: See package COPYING file.
- Bundled in distribution: yes
- Notes: declared directly as a runtime dependency. License verified from
  package metadata field `License: GNU Lesser General Public License v3 or
  later` and classifier `License :: OSI Approved :: GNU Lesser General Public
  License v3 or later (LGPLv3+)`.

### alembic

- Version: 1.17.1
- Purpose: database migrations for the relational persistence schema
- License: MIT
- Source: https://pypi.org/project/alembic/1.17.1/
- Copyright notice: See package license file.
- Bundled in distribution: yes
- Notes: declared directly as a runtime dependency. License verified from
  package metadata field `License-Expression: MIT`.

### anyio

- Version: 4.16.0
- Purpose: transitive Starlette dependency for async runtime compatibility
- License: MIT
- Source: https://pypi.org/project/anyio/4.16.0/
- Copyright notice: See package license file.
- Bundled in distribution: yes
- Notes: installed transitively by Starlette. License verified from package
  metadata field `License-Expression: MIT`.

### click

- Version: 8.5.1
- Purpose: transitive Uvicorn dependency for command-line support
- License: BSD-3-Clause
- Source: https://pypi.org/project/click/8.5.1/
- Copyright notice: See package license file.
- Bundled in distribution: yes
- Notes: installed transitively by Uvicorn. License verified from package
  metadata field `License-Expression: BSD-3-Clause`.

### colorama

- Version: 0.4.6
- Purpose: transitive Click dependency for Windows console support
- License: BSD-3-Clause
- Source: https://pypi.org/project/colorama/0.4.6/
- Copyright notice: Copyright (c) 2010 Jonathan Hartley.
- Bundled in distribution: yes
- Notes: installed transitively by Click on Windows. License verified from
  package metadata field `License: BSD-3-Clause`.

### cffi

- Version: 2.1.1
- Purpose: transitive cryptography dependency for C foreign function interface
- License: MIT-0
- Source: https://pypi.org/project/cffi/2.1.1/
- Copyright notice: See package license and authors files.
- Bundled in distribution: yes
- Notes: installed transitively by cryptography. License verified from package
  metadata field `License-Expression: MIT-0`.

### cryptography

- Version: 50.0.2
- Purpose: transitive asyncua dependency for certificate and encryption support
- License: Apache-2.0 OR BSD-3-Clause
- Source: https://pypi.org/project/cryptography/50.0.2/
- Copyright notice: See package license files.
- Bundled in distribution: yes
- Notes: installed transitively by asyncua and pyOpenSSL. License verified from
  package metadata field `License-Expression: Apache-2.0 OR BSD-3-Clause`.

### fastapi

- Version: 0.139.0
- Purpose: read-only health and runtime HTTP API
- License: MIT
- Source: https://pypi.org/project/fastapi/0.139.0/
- Copyright notice: See package license file.
- Bundled in distribution: yes
- Notes: declared directly as a runtime dependency. License verified from
  package metadata field `License-Expression: MIT`.

### greenlet

- Version: 3.5.6
- Purpose: transitive SQLAlchemy dependency
- License: MIT AND PSF-2.0
- Source: https://pypi.org/project/greenlet/3.5.6/
- Copyright notice: See package license file.
- Bundled in distribution: yes
- Notes: installed transitively by SQLAlchemy. License verified from package
  metadata field `License-Expression: MIT AND PSF-2.0`.

### h11

- Version: 0.16.0
- Purpose: transitive Uvicorn dependency for HTTP/1.1 protocol handling
- License: MIT
- Source: https://pypi.org/project/h11/0.16.0/
- Copyright notice: See package license file.
- Bundled in distribution: yes
- Notes: installed transitively by Uvicorn. License verified from package
  metadata field `License: MIT`.

### idna

- Version: 3.20
- Purpose: transitive AnyIO dependency for internationalized domain names
- License: BSD-3-Clause
- Source: https://pypi.org/project/idna/3.20/
- Copyright notice: See package license file.
- Bundled in distribution: yes
- Notes: installed transitively by AnyIO. License verified from package
  metadata field `License-Expression: BSD-3-Clause`.

### Mako

- Version: 1.4.3
- Purpose: transitive Alembic dependency for migration templates
- License: MIT
- Source: https://pypi.org/project/Mako/1.4.3/
- Copyright notice: See package license file.
- Bundled in distribution: yes
- Notes: installed transitively by Alembic. License verified from package
  metadata field `License: MIT`.

### MarkupSafe

- Version: 3.0.4
- Purpose: transitive Mako dependency for safe markup handling
- License: BSD-3-Clause
- Source: https://pypi.org/project/MarkupSafe/3.0.4/
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

### pg8000

- Version: 1.31.2
- Purpose: pure-Python PostgreSQL driver used by SQLAlchemy (postgresql+pg8000)
- License: BSD-3-Clause
- Source: https://pypi.org/project/pg8000/1.31.2/
- Copyright notice: See package license file.
- Bundled in distribution: yes
- Notes: direct dependency. License verified from package metadata field `License: BSD 3-Clause License` and classifier `License :: OSI Approved :: BSD License`.

### pycparser

- Version: 3.12
- Purpose: transitive cffi dependency for parsing C declarations
- License: BSD-3-Clause
- Source: https://pypi.org/project/pycparser/3.12/
- Copyright notice: Copyright Eli Bendersky. See package license file.
- Bundled in distribution: yes
- Notes: installed transitively by cffi. License verified from package metadata
  field `License-Expression: BSD-3-Clause`.

### pyOpenSSL

- Version: 26.4.0
- Purpose: transitive asyncua dependency for OpenSSL integration
- License: Apache License, Version 2.0
- Source: https://pypi.org/project/pyOpenSSL/26.4.0/
- Copyright notice: See package license file.
- Bundled in distribution: yes
- Notes: installed transitively by asyncua. License verified from package
  metadata field `License: Apache License, Version 2.0`.

### python-dateutil

- Version: 4.0.0
- Purpose: transitive asyncua dependency for date and time handling
- License: BSD-3-Clause
- Source: https://pypi.org/project/python-dateutil/4.0.0/
- Copyright notice: See package license file.
- Bundled in distribution: yes
- Notes: installed transitively by asyncua. License verified from package
  metadata field `License-Expression: BSD-3-Clause`.

### pytz

- Version: 2026.6
- Purpose: transitive asyncua dependency for timezone data
- License: MIT
- Source: https://pypi.org/project/pytz/2026.6/
- Copyright notice: See package license file.
- Bundled in distribution: yes
- Notes: installed transitively by asyncua. License verified from package
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

### scramp

- Version: 1.5.0
- Purpose: transitive pg8000 dependency implementing SCRAM authentication
- License: MIT-0
- Source: https://pypi.org/project/scramp/1.5.0/
- Copyright notice: See package license file.
- Bundled in distribution: yes
- Notes: installed transitively by pg8000. License verified from package metadata field `License-Expression: MIT-0`.

### six

- Version: 1.17.0
- Purpose: transitive python-dateutil dependency for Python compatibility helpers
- License: MIT
- Source: https://pypi.org/project/six/1.17.0/
- Copyright notice: Copyright Benjamin Peterson. See package license file.
- Bundled in distribution: yes
- Notes: installed transitively by python-dateutil. License verified from
  package metadata field `License: MIT`.

### sniffio

- Version: 1.3.1
- Purpose: transitive AnyIO dependency for async library detection
- License: MIT OR Apache-2.0
- Source: https://pypi.org/project/sniffio/1.3.1/
- Copyright notice: See package license file.
- Bundled in distribution: yes
- Notes: installed transitively by AnyIO. License verified from package
  metadata field `License: MIT OR Apache-2.0`.

### sortedcontainers

- Version: 2.4.0
- Purpose: transitive asyncua dependency for sorted collection primitives
- License: Apache 2.0
- Source: https://pypi.org/project/sortedcontainers/2.4.0/
- Copyright notice: Copyright Grant Jenks. See package license file.
- Bundled in distribution: yes
- Notes: installed transitively by asyncua. License verified from package
  metadata field `License: Apache 2.0` and classifier
  `License :: OSI Approved :: Apache Software License`.

### starlette

- Version: 1.10.0
- Purpose: transitive FastAPI dependency providing ASGI routing primitives
- License: BSD-3-Clause
- Source: https://pypi.org/project/starlette/1.10.0/
- Copyright notice: See package license file.
- Bundled in distribution: yes
- Notes: installed transitively by FastAPI. License verified from package
  metadata field `License-Expression: BSD-3-Clause`.

### typing-inspection

- Version: 0.4.5
- Purpose: transitive Pydantic dependency for runtime typing introspection
- License: MIT
- Source: https://pypi.org/project/typing-inspection/0.4.5/
- Copyright notice: See package license file.
- Bundled in distribution: yes
- Notes: installed transitively by Pydantic and FastAPI. License verified from
  package metadata field `License-Expression: MIT`.

### typing_extensions

- Version: 4.16.0
- Purpose: transitive Pydantic dependency for typing features
- License: PSF-2.0
- Source: https://pypi.org/project/typing-extensions/4.16.0/
- Copyright notice: See package license file.
- Bundled in distribution: yes
- Notes: installed transitively by Pydantic. License verified from package
  metadata field `License-Expression: PSF-2.0`.

### tzdata

- Version: 2026.6
- Purpose: transitive python-dateutil dependency providing IANA time zone data
- License: Apache-2.0
- Source: https://pypi.org/project/tzdata/2026.6/
- Copyright notice: Copyright (c) 2020, Paul Ganssle (Google); Copyright (c)
  2026, Stan Ulbrych. See package license file.
- Bundled in distribution: yes
- Notes: installed transitively by python-dateutil. License verified from
  package metadata field `License: Apache-2.0` and the bundled license file.

### uvicorn

- Version: 0.35.0
- Purpose: ASGI server for local health and runtime HTTP API
- License: BSD-3-Clause
- Source: https://pypi.org/project/uvicorn/0.35.0/
- Copyright notice: See package license file.
- Bundled in distribution: yes
- Notes: declared directly as a runtime dependency. License verified from
  package metadata field `License-Expression: BSD-3-Clause`.

## Development Dependencies

The following packages are installed by `python -m pip install -e ".[dev]"` in
the bootstrap development environment. Direct dependencies are declared in
`pyproject.toml`; transitive dependencies are listed from installed package
metadata and should be regenerated from a lockfile once dependency locking is
introduced.

### ast_serialize

- Version: 0.13.1
- Purpose: transitive mypy dependency
- License: MIT
- Source: https://pypi.org/project/ast-serialize/0.13.1/
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

### certifi

- Version: 2026.10.23
- Purpose: transitive httpx dependency for certificate authority bundle
- License: MPL-2.0
- Source: https://pypi.org/project/certifi/2026.10.23/
- Copyright notice: See package license file.
- Bundled in distribution: no
- Notes: installed transitively in the `dev` extra. License verified from
  package metadata field `License: MPL-2.0`.

### httpcore

- Version: 1.0.9
- Purpose: transitive httpx dependency for low-level HTTP client behavior
- License: BSD-3-Clause
- Source: https://pypi.org/project/httpcore/1.0.9/
- Copyright notice: See package license file.
- Bundled in distribution: no
- Notes: installed transitively in the `dev` extra. License verified from
  package metadata field `License-Expression: BSD-3-Clause`.

### httpx

- Version: 0.28.1
- Purpose: test client dependency for FastAPI API tests
- License: BSD-3-Clause
- Source: https://pypi.org/project/httpx/0.28.1/
- Copyright notice: See package license file.
- Bundled in distribution: no
- Notes: declared directly in the `dev` extra. License verified from package
  metadata field `License: BSD-3-Clause`.

### iniconfig

- Version: 2.3.1
- Purpose: transitive pytest dependency
- License: MIT
- Source: https://pypi.org/project/iniconfig/2.3.1/
- Copyright notice: See package license file.
- Bundled in distribution: no
- Notes: installed transitively in the `dev` extra.

### librt

- Version: 0.16.0
- Purpose: transitive mypy dependency
- License: MIT
- Source: https://pypi.org/project/librt/0.16.0/
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

- Version: 1.2.0
- Purpose: transitive mypy dependency
- License: MIT
- Source: https://pypi.org/project/mypy-extensions/1.2.0/
- Copyright notice: See package license file.
- Bundled in distribution: no
- Notes: installed transitively in the `dev` extra.

### packaging

- Version: 23.2
- Purpose: transitive pytest dependency
- License: Apache-2.0 OR BSD-2-Clause
- Source: https://pypi.org/project/packaging/23.2/
- Copyright notice: See package license file.
- Bundled in distribution: no
- Notes: declared directly in the `dev` extra for the credits generator and
  also installed transitively by pytest. License verified from package metadata
  classifiers `License :: OSI Approved :: Apache Software License` and
  `License :: OSI Approved :: BSD License`.

### pathspec

- Version: 1.1.1
- Purpose: transitive mypy dependency
- License: MPL-2.0
- Source: https://pypi.org/project/pathspec/1.1.1/
- Copyright notice: See package license file.
- Bundled in distribution: no
- Notes: installed transitively in the `dev` extra.

### pluggy

- Version: 1.7.0
- Purpose: transitive pytest dependency
- License: MIT
- Source: https://pypi.org/project/pluggy/1.7.0/
- Copyright notice: See package license file.
- Bundled in distribution: no
- Notes: installed transitively in the `dev` extra.

### Pygments

- Version: 2.21.0
- Purpose: transitive pytest dependency for terminal output
- License: BSD-2-Clause
- Source: https://pypi.org/project/Pygments/2.21.0/
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

`tools/generate_credits.py` does the following:

1. reads the exact versions from the lockfile or the active environment,
2. builds the list of runtime dependencies,
3. joins it with the manually verified license map in this file,
4. aborts when a license is missing or ambiguous,
5. writes the credits file shipped with the distribution,
6. never overwrites the required copyright information.
