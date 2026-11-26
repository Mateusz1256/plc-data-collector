"""Import Tags from TIA Portal: upload store, preview model and Tag building.

The upload is either one PLC tag table (``.xlsx``) or DB sources (``.db``)
with the PLC data types they use (``.udt``). The files are kept on the server
under a short-lived token, only between the preview and the save; nothing the
browser sends back is trusted beyond choices (DB numbers, ticks, selected
keys, names): every preview and the save parse and lay out the files again.

A candidate's key is ``<file>#<db>#<path>`` (``<file>##<name>`` for a tag
table row). The browser posts the selected keys; a collapsed array posts its
own key (``<file>#<db>#<path without the index>``) as "select all elements".

Parsing and laying out are blocking and can take seconds for a big DB, so
callers run ``prepare_import`` and the ``UploadStore`` methods in a thread.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import tempfile
import time
from collections.abc import Callable, Collection, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from python_multipart.exceptions import FormParserError
from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.requests import Request

from plc_data_collector.domain import ConfigurationError, ConnectionConfig, TagConfig
from plc_data_collector.panel.s7_tag_form import S7TagForm
from plc_data_collector.panel.tag_forms import generate_tag_id
from plc_data_collector.panel.tag_paste import MAX_PASTE_ROWS
from plc_data_collector.protocols.s7_tags import (
    Area,
    DataType,
    S7Address,
    S7TagOptions,
    derive_value_type,
    display_address,
    resolve_tag,
)
from plc_data_collector.tia.layout import Candidate, layout_db
from plc_data_collector.tia.source import (
    DbSource,
    TiaSourceError,
    decode_source,
    parse_sources,
)
from plc_data_collector.tia.tag_table import parse_tag_table

MAX_FILES = 10
MAX_FILE_BYTES = 5 * 1024 * 1024
UPLOAD_MAX_AGE_S = 30 * 60
# Arrays with more members than this show as one row until expanded.
ARRAY_COLLAPSE = 50
# Rows a preview renders at most; the filter finds the others.
MAX_PREVIEW_ROWS = 1000
MAX_IMPORT_ROWS = MAX_PASTE_ROWS
# Candidates kept for one upload: bounds memory when many big DBs come at once.
MAX_CANDIDATES = 600_000
SOURCE_SUFFIXES = (".db", ".udt")
TABLE_SUFFIX = ".xlsx"

EXPIRED_MESSAGE = "This upload expired. Upload the files again."
NO_FILES_MESSAGE = "Choose at least one file."
TOO_MANY_FILES_MESSAGE = f"Upload at most {MAX_FILES} files at a time."
TOO_LARGE_UPLOAD_MESSAGE = (
    f"Upload at most {MAX_FILES} files of at most "
    f"{MAX_FILE_BYTES // (1024 * 1024)} MB each."
)
UNREADABLE_UPLOAD_MESSAGE = "The upload could not be read. Choose the files again."
MIXED_MESSAGE = (
    "Upload either one tag table (.xlsx) or DB and UDT sources (.db, .udt), not both."
)
NO_BLOCKS_MESSAGE = (
    "The sources have no data block (DATA_BLOCK). Upload the .db files too."
)
ROW_LIMIT_MESSAGE = f"Import at most {MAX_IMPORT_ROWS} tags at a time."
NOTHING_SELECTED_MESSAGE = "Select at least one tag to import."
ROW_PROBLEMS_MESSAGE = (
    "Some selected rows have a problem. Clear them or fix the problem, then save."
)
UNKNOWN_SELECTION_MESSAGE = "Some selected rows are not in this upload. Preview again."
GROUP_MESSAGE = "Choose a tag group."
NUMBER_MISSING = "Enter the DB number above."
NUMBER_ERROR = "Enter a DB number from 1 to 65535."
TOO_MANY_MEMBERS = (
    f"The upload has more than {MAX_CANDIDATES} members; upload fewer DBs at a time."
)

_TOKEN = re.compile(r"[A-Za-z0-9_-]{22}")
_META = "upload.json"
_MAX_DB = 65535
_MAX_FIELDS = 20
_MAX_FIELD_BYTES = 64 * 1024
_BODY_SLACK = 1024 * 1024
_MAX_NAME_CHARS = 255


# Upload store


@dataclass(frozen=True)
class UploadedFile:
    """One uploaded file: the name the browser gave it and its bytes."""

    name: str
    data: bytes


class UploadStore:
    """Uploaded files kept under ``<root>/<token>/`` for a short time.

    Files are stored under generated names; the names the browser sent are
    kept only in a JSON file. A token is checked against the
    ``secrets.token_urlsafe(16)`` alphabet and length before it touches a
    path. Uploads not used for ``max_age_s`` are removed on every access and
    when the store is created (at startup). With ``root`` None a private
    temporary directory is made on the first save.
    """

    def __init__(
        self,
        root: Path | None,
        *,
        max_age_s: float = UPLOAD_MAX_AGE_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Create the store and remove expired uploads."""
        self._root = root
        self._max_age_s = max_age_s
        self._clock = clock
        self.clean()

    def save(self, files: Sequence[UploadedFile]) -> str:
        """Store ``files``; return the new token."""
        self.clean()
        if self._root is None:
            self._root = Path(tempfile.mkdtemp(prefix="plc-data-collector-tia-"))
        self._root.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(16)
        folder = self._root / token
        folder.mkdir(mode=0o700)
        try:
            for index, upload in enumerate(files):
                (folder / f"{index}.upload").write_bytes(upload.data)
            meta = folder / _META
            meta.write_text(
                json.dumps({"files": [upload.name for upload in files]}),
                encoding="utf-8",
            )
            now = self._clock()
            os.utime(meta, (now, now))
        except OSError:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        return token

    def load(self, token: str) -> list[UploadedFile] | None:
        """Return the files of ``token``; None when unknown or expired.

        A successful load keeps the upload for another ``max_age_s``.
        """
        self.clean()
        folder = self._folder(token)
        if folder is None:
            return None
        meta = folder / _META
        try:
            names = json.loads(meta.read_text(encoding="utf-8"))["files"]
            if not isinstance(names, list) or not all(
                isinstance(name, str) for name in names
            ):
                return None
            files = [
                UploadedFile(name, (folder / f"{index}.upload").read_bytes())
                for index, name in enumerate(names)
            ]
            now = self._clock()
            os.utime(meta, (now, now))
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return files

    def delete(self, token: str) -> None:
        """Remove the upload of ``token``, if there is one."""
        folder = self._folder(token)
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)

    def clean(self) -> None:
        """Remove uploads older than ``max_age_s``."""
        if self._root is None:
            return
        try:
            children = list(self._root.iterdir())
        except OSError:
            return
        now = self._clock()
        for child in children:
            if not _TOKEN.fullmatch(child.name):
                continue
            meta = child / _META
            try:
                stamp = (meta if meta.exists() else child).stat().st_mtime
            except OSError:
                continue
            if now - stamp > self._max_age_s:
                shutil.rmtree(child, ignore_errors=True)

    def _folder(self, token: str) -> Path | None:
        if self._root is None or not _TOKEN.fullmatch(token):
            return None
        folder = self._root / token
        return folder if folder.is_dir() else None


# Reading the upload


class UploadRefused(Exception):
    """The upload breaks a limit or is not what the import reads."""

    def __init__(self, message: str) -> None:
        """Keep the message shown on the page."""
        super().__init__(message)
        self.message = message


@dataclass
class _Part:
    headers: dict[bytes, bytes] = field(default_factory=dict)
    header_name: bytes = b""
    header_value: bytes = b""
    name: str = ""
    file_name: str | None = None
    data: bytearray = field(default_factory=bytearray)


class _UploadReader:
    """Callbacks for ``MultipartParser`` that keep every limit while reading."""

    def __init__(self) -> None:
        self.fields: dict[str, str] = {}
        self.files: list[UploadedFile] = []
        self.field_count = 0
        self.part = _Part()

    def on_part_begin(self) -> None:
        self.part = _Part()

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self.part.header_name += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self.part.header_value += data[start:end]

    def on_header_end(self) -> None:
        part = self.part
        part.headers[part.header_name.strip().lower()] = part.header_value.strip()
        part.header_name = part.header_value = b""

    def on_headers_finished(self) -> None:
        part = self.part
        _, options = parse_options_header(part.headers.get(b"content-disposition"))
        part.name = options.get(b"name", b"").decode("utf-8", "replace")
        raw_file_name = options.get(b"filename")
        if raw_file_name is None:
            self.field_count += 1
            if self.field_count > _MAX_FIELDS:
                raise UploadRefused(UNREADABLE_UPLOAD_MESSAGE)
            return
        part.file_name = _display_name(raw_file_name.decode("utf-8", "replace"))
        if part.file_name and len(self.files) >= MAX_FILES:
            raise UploadRefused(TOO_MANY_FILES_MESSAGE)

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        part = self.part
        part.data += data[start:end]
        if part.file_name is None:
            if len(part.data) > _MAX_FIELD_BYTES:
                raise UploadRefused(UNREADABLE_UPLOAD_MESSAGE)
        elif len(part.data) > MAX_FILE_BYTES:
            name = part.file_name or "A file"
            raise UploadRefused(
                f"{name} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB."
            )

    def on_part_end(self) -> None:
        part = self.part
        if part.file_name is None:
            self.fields[part.name] = part.data.decode("utf-8", "replace")
        elif part.file_name or part.data:
            # A file input left empty sends a part with no name and no data.
            self.files.append(UploadedFile(part.file_name, bytes(part.data)))


def _display_name(raw: str) -> str:
    """The file name without any folder a browser may send with it."""
    return re.split(r"[\\/]", raw)[-1].strip()[:_MAX_NAME_CHARS]


async def read_upload(request: Request) -> tuple[dict[str, str], list[UploadedFile]]:
    """Read a multipart upload; return its fields and files.

    The body is read in chunks and parsed as it arrives: a request declaring
    or sending more than the files' total limit, an 11th file and a file over
    5 MB are refused as soon as they show, before the rest is read.
    """
    kind, params = parse_options_header(request.headers.get("content-type"))
    boundary = params.get(b"boundary")
    if kind != b"multipart/form-data" or not boundary:
        raise UploadRefused(NO_FILES_MESSAGE)
    limit = MAX_FILES * MAX_FILE_BYTES + _BODY_SLACK
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise UploadRefused(TOO_LARGE_UPLOAD_MESSAGE)
    reader = _UploadReader()
    parser = MultipartParser(
        boundary,
        {
            "on_part_begin": reader.on_part_begin,
            "on_header_field": reader.on_header_field,
            "on_header_value": reader.on_header_value,
            "on_header_end": reader.on_header_end,
            "on_headers_finished": reader.on_headers_finished,
            "on_part_data": reader.on_part_data,
            "on_part_end": reader.on_part_end,
        },
    )
    received = 0
    try:
        async for chunk in request.stream():
            received += len(chunk)
            if received > limit:
                raise UploadRefused(TOO_LARGE_UPLOAD_MESSAGE)
            parser.write(chunk)
        parser.finalize()
    except FormParserError as error:
        raise UploadRefused(UNREADABLE_UPLOAD_MESSAGE) from error
    return reader.fields, reader.files


def check_files(files: Sequence[UploadedFile]) -> None:
    """Refuse an upload that is not one tag table or a set of sources."""
    if not files:
        raise UploadRefused(NO_FILES_MESSAGE)
    if len(files) > MAX_FILES:
        raise UploadRefused(TOO_MANY_FILES_MESSAGE)
    seen: set[str] = set()
    for upload in files:
        if not upload.name.lower().endswith((*SOURCE_SUFFIXES, TABLE_SUFFIX)):
            raise UploadRefused(f"{upload.name} is not a .db, .udt or .xlsx file.")
        if len(upload.data) > MAX_FILE_BYTES:
            raise UploadRefused(
                f"{upload.name} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB."
            )
        if upload.name.lower() in seen:
            raise UploadRefused(f"Two files are named {upload.name}.")
        seen.add(upload.name.lower())
    tables = [f for f in files if f.name.lower().endswith(TABLE_SUFFIX)]
    if tables and len(files) > 1:
        raise UploadRefused(MIXED_MESSAGE)


# What the form sends back


@dataclass(frozen=True)
class ImportForm:
    """The choices posted with a preview or a save; keys are only looked up."""

    group_id: str = ""
    db_numbers: Mapping[int, str] = field(default_factory=dict)
    classic: frozenset[int] = frozenset()
    selected: frozenset[str] = frozenset()
    selected_arrays: frozenset[str] = frozenset()
    names: Mapping[str, str] = field(default_factory=dict)
    expanded: frozenset[str] = frozenset()
    filter: str = ""
    action: str = "preview"

    @classmethod
    def from_pairs(cls, pairs: Iterable[tuple[str, object]]) -> ImportForm:
        """Read the posted fields; anything unexpected is ignored."""
        numbers: dict[int, str] = {}
        classic: set[int] = set()
        selected: set[str] = set()
        arrays: set[str] = set()
        names: dict[str, str] = {}
        expanded: set[str] = set()
        collapsed: set[str] = set()
        single: dict[str, str] = {}
        for key, raw in pairs:
            if not isinstance(raw, str):
                continue
            value = raw.strip()
            if key.startswith("db_number_") and key[10:].isdigit():
                numbers[int(key[10:])] = value
            elif key.startswith("classic_") and key[8:].isdigit():
                classic.add(int(key[8:]))
            elif key == "select":
                selected.add(raw)
            elif key == "select_array":
                arrays.add(raw)
            elif key.startswith("name:"):
                names[key[5:]] = value
            elif key == "expand":
                expanded.add(raw)
            elif key == "collapse":
                collapsed.add(raw)
            else:
                single[key] = value
        return cls(
            group_id=single.get("tag_group_id", ""),
            db_numbers=numbers,
            classic=frozenset(classic),
            selected=frozenset(selected),
            selected_arrays=frozenset(arrays),
            names=names,
            expanded=frozenset(expanded - collapsed),
            filter=single.get("filter", ""),
            action=single.get("action", "preview") or "preview",
        )


# The preview


@dataclass(frozen=True)
class PreviewRow:
    """One candidate as the preview table shows it."""

    key: str
    name: str
    path: str
    address: str
    data_type: str
    offset: str
    comment: str
    tag_id: str
    problem: str
    selected: bool
    unsupported: bool
    kind: str = "row"


@dataclass(frozen=True)
class ArrayRow:
    """A collapsed array: one row with "select all elements" and expand."""

    key: str
    label: str
    count: str
    comment: str
    selected: bool
    expanded: bool
    problems: int
    kind: str = "array"


@dataclass
class Section:
    """One DB (or the tag table) of the upload."""

    title: str
    file: str
    index: int | None = None
    ask_number: bool = False
    number: str = ""
    number_error: str | None = None
    ask_classic: bool = False
    classic: bool = False
    size: int | None = None
    error: str | None = None
    items: list[PreviewRow | ArrayRow] = field(default_factory=list)


@dataclass
class ImportPreview:
    """Everything the preview page shows; ``tags`` is set when a save may go on."""

    sections: list[Section]
    group_id: str
    filter: str
    error: str | None = None
    selected_count: int = 0
    shown_rows: int = 0
    matching_rows: int = 0
    hidden_selected: list[str] = field(default_factory=list)
    hidden_arrays: list[str] = field(default_factory=list)
    hidden_names: list[tuple[str, str]] = field(default_factory=list)
    expanded: list[str] = field(default_factory=list)
    tags: list[TagConfig] | None = None


class ImportRefused(Exception):
    """The upload as a whole cannot be imported (a syntax error, no DB)."""

    def __init__(self, message: str) -> None:
        """Keep the message shown on the page."""
        super().__init__(message)
        self.message = message


@dataclass
class _Entry:
    key: str
    candidate: Candidate
    section: int
    group: str | None  # the array key, for members of a collapsed array
    base_problem: str | None


def _source_error(error: TiaSourceError) -> str:
    if error.line > 0:
        return f"{error.file}, line {error.line}: {error.message}"
    return f"{error.file}: {error.message}"


def _db_number(text: str) -> tuple[int | None, str | None]:
    if not text:
        return None, None
    if text.isdigit() and 1 <= int(text) <= _MAX_DB:
        return int(text), None
    return None, NUMBER_ERROR


def _address_key(address: S7Address) -> tuple[object, ...]:
    # The same key as ``S7TagForm.register_key``.
    return (address.area.value, address.db, address.byte, address.bit)


def _array_base(path: str) -> str | None:
    bracket = path.find("[")
    return None if bracket < 0 else path[:bracket]


def _indices(path: str) -> list[str]:
    start = path.find("[")
    end = path.find("]", start)
    return [part.strip() for part in path[start + 1 : end].split(",")]


def _array_label(base: str, first: str, last: str) -> tuple[str, int]:
    """``Values[0..4999]`` and the element count, from the first and last paths."""
    low, high = _indices(first), _indices(last)
    bounds: list[str] = []
    count = 1
    for a, b in zip(low, high, strict=False):
        bounds.append(f"{a}..{b}")
        try:
            count *= int(b) - int(a) + 1
        except ValueError:
            count = 0
    return f"{base}[{', '.join(bounds)}]", count


class _Upload:
    """The parsed and laid-out upload: sections and candidates by key."""

    def __init__(self, files: Sequence[UploadedFile], form: ImportForm) -> None:
        self.sections: list[Section] = []
        self.entries: list[_Entry] = []
        self.by_key: dict[str, _Entry] = {}
        self.arrays: dict[str, list[_Entry]] = {}
        tables = [f for f in files if f.name.lower().endswith(TABLE_SUFFIX)]
        try:
            if tables:
                self._read_table(tables[0])
            else:
                self._read_sources(files, form)
        except TiaSourceError as error:
            raise ImportRefused(_source_error(error)) from error

    def _add(
        self, section: int, key: str, candidate: Candidate, problem: str | None
    ) -> None:
        unique = key
        number = 2
        while unique in self.by_key:
            unique = f"{key}#{number}"
            number += 1
        entry = _Entry(unique, candidate, section, None, candidate.problem or problem)
        self.entries.append(entry)
        self.by_key[unique] = entry

    def _read_table(self, upload: UploadedFile) -> None:
        self.sections.append(Section(title="PLC tag table", file=upload.name))
        for candidate in parse_tag_table(upload.data, upload.name):
            self._add(0, f"{upload.name}##{candidate.path}", candidate, None)

    def _read_sources(self, files: Sequence[UploadedFile], form: ImportForm) -> None:
        sources = parse_sources(
            [(f.name, decode_source(f.data, f.name)) for f in files]
        )
        if not sources.blocks:
            raise ImportRefused(NO_BLOCKS_MESSAGE)
        names: set[str] = set()
        for db in sources.blocks:
            if db.name in names:
                raise ImportRefused(f'Data block "{db.name}" is in the upload twice.')
            names.add(db.name)
        total = 0
        for index, db in enumerate(sources.blocks):
            section = self._section(index, db, form)
            self.sections.append(section)
            number, _ = _db_number(section.number)
            missing = None
            if number is None:
                missing = section.number_error or NUMBER_MISSING
            try:
                layout = layout_db(
                    db if number is None else replace(db, number=number),
                    sources.types,
                    classic_confirmed=section.classic,
                )
            except TiaSourceError as error:
                section.error = (
                    f"Line {error.line}: {error.message}"
                    if error.line > 0
                    else error.message
                )
                continue
            total += len(layout.candidates)
            if total > MAX_CANDIDATES:
                section.error = TOO_MANY_MEMBERS
                continue
            section.size = layout.size
            start = len(self.entries)
            for candidate in layout.candidates:
                self._add(
                    index, f"{db.file}#{db.name}#{candidate.path}", candidate, missing
                )
            self._group_arrays(self.entries[start:], f"{db.file}#{db.name}#")

    @staticmethod
    def _section(index: int, db: DbSource, form: ImportForm) -> Section:
        section = Section(title=f'DB "{db.name}"', file=db.file, index=index)
        if db.number is not None:
            section.number = str(db.number)
        else:
            section.ask_number = True
            section.number = form.db_numbers.get(index, "")
            _, section.number_error = _db_number(section.number)
        if db.access == "unknown":
            section.ask_classic = True
            section.classic = index in form.classic
        return section

    def _group_arrays(self, entries: list[_Entry], prefix: str) -> None:
        run: list[_Entry] = []
        run_base: str | None = None
        for entry in [*entries, None]:
            base = None if entry is None else _array_base(entry.candidate.path)
            if base is not None and base == run_base:
                assert entry is not None
                run.append(entry)
                continue
            if run_base is not None and len(run) > ARRAY_COLLAPSE:
                key = prefix + run_base
                self.arrays[key] = run
                for member in run:
                    member.group = key
            run = [] if entry is None or base is None else [entry]
            run_base = base


def _matches(entry: _Entry, needle: str) -> bool:
    candidate = entry.candidate
    return needle in candidate.path.lower() or needle in candidate.comment.lower()


def _address_text(candidate: Candidate) -> str:
    address = candidate.address
    if address is None or (address.area is Area.DB and address.db is None):
        return "" if address is not None else candidate.source_address
    if candidate.data_type is None or candidate.problem:
        return candidate.source_address or str(address)
    return display_address(address, candidate.data_type)


def _offset(candidate: Candidate) -> str:
    address = candidate.address
    if address is None or address.area is not Area.DB:
        return ""
    return str(address.byte) if address.bit is None else f"{address.byte}.{address.bit}"


def _type_text(candidate: Candidate) -> str:
    if candidate.data_type is DataType.STRING:
        return f"string[{candidate.length}]"
    return "" if candidate.data_type is None else candidate.data_type.value


def _build_tag(
    candidate: Candidate, tag_id: str, name: str, group_id: str
) -> TagConfig:
    """Return the Tag of a candidate; raise ``ConfigurationError`` if invalid."""
    assert candidate.address is not None and candidate.data_type is not None
    options = S7TagOptions(data_type=candidate.data_type, length=candidate.length)
    stored = options.to_options()
    address = str(candidate.address)
    resolve_tag(address, stored)
    metadata = {"description": candidate.comment} if candidate.comment else {}
    return TagConfig(
        id=tag_id,
        tag_group_id=group_id,
        name=name,
        address=address,
        value_type=derive_value_type(options),
        metadata=metadata,
        protocol_options=stored,
    )


def prepare_import(
    files: Sequence[UploadedFile],
    form: ImportForm,
    *,
    connection: ConnectionConfig,
    existing: Sequence[TagConfig],
    taken_ids: Collection[str],
    group_ids: Sequence[str],
) -> ImportPreview:
    """Parse the upload with the posted choices and build the preview.

    ``existing`` are the Connection's Tags (their addresses are flagged);
    ``taken_ids`` every Tag ID in use or archived. With ``form.action`` "save"
    the preview's ``tags`` are set only when the selection can be saved as a
    whole; otherwise ``error`` says why. "select_all" selects the visible rows
    that have no problem, "select_none" clears the selection. Raises
    ``ImportRefused``.
    """
    upload = _Upload(files, form)
    group_id = form.group_id if form.group_id in group_ids else ""
    if not group_id and group_ids and form.action != "save":
        group_id = group_ids[0]
    preview = ImportPreview(upload.sections, group_id, form.filter)
    existing_by_address = _existing_addresses(existing, connection)
    expanded = {key for key in form.expanded if key in upload.arrays}
    preview.expanded = sorted(expanded)
    visible, preview.matching_rows = _visible(
        upload, form.filter.strip().lower(), expanded
    )

    if form.action == "select_none":
        selected_keys: set[str] = set()
        selected_arrays: set[str] = set()
    else:
        selected_keys = {key for key in form.selected if key in upload.by_key}
        selected_arrays = {key for key in form.selected_arrays if key in upload.arrays}
    unknown = len(selected_keys) < len(form.selected) or len(selected_arrays) < len(
        form.selected_arrays
    )
    if form.action == "select_all":
        selected_keys |= {
            item.key
            for item in visible
            if isinstance(item, _Entry)
            and item.base_problem is None
            and item.candidate.address is not None
            and _address_key(item.candidate.address) not in existing_by_address
        }

    chosen = [
        entry
        for entry in upload.entries
        if entry.key in selected_keys or entry.group in selected_arrays
    ]
    preview.selected_count = len(chosen)
    errors: dict[str, str] = {}
    ids: dict[str, str] = {}
    tags: list[TagConfig] = []
    if len(chosen) <= MAX_IMPORT_ROWS:
        tags = _check_selection(
            chosen,
            form.names,
            connection.id,
            existing_by_address,
            taken_ids,
            group_id,
            errors,
            ids,
        )
    if form.action == "save":
        if not group_id:
            preview.error = GROUP_MESSAGE
        elif unknown:
            preview.error = UNKNOWN_SELECTION_MESSAGE
        elif not chosen:
            preview.error = NOTHING_SELECTED_MESSAGE
        elif len(chosen) > MAX_IMPORT_ROWS:
            preview.error = ROW_LIMIT_MESSAGE
        elif errors:
            preview.error = ROW_PROBLEMS_MESSAGE
        else:
            preview.tags = tags
    elif len(chosen) > MAX_IMPORT_ROWS:
        preview.error = ROW_LIMIT_MESSAGE

    def row(entry: _Entry) -> PreviewRow:
        candidate = entry.candidate
        problem = errors.get(entry.key) or entry.base_problem
        if problem is None and candidate.address is not None:
            holder = existing_by_address.get(_address_key(candidate.address))
            if holder is not None:
                problem = f"Same address as Tag {holder}."
        return PreviewRow(
            key=entry.key,
            name=form.names.get(entry.key, "") or candidate.path,
            path=candidate.path,
            address=_address_text(candidate),
            data_type=_type_text(candidate),
            offset=_offset(candidate),
            comment=candidate.comment,
            tag_id=ids.get(entry.key, ""),
            problem=problem or "",
            selected=entry.key in selected_keys or entry.group in selected_arrays,
            unsupported=candidate.data_type is None,
        )

    shown: set[str] = set()
    for item in visible:
        if isinstance(item, _Entry):
            upload.sections[item.section].items.append(row(item))
            shown.add(item.key)
            continue
        members = upload.arrays[item]
        label, elements = _array_label(
            _array_base(members[0].candidate.path) or "",
            members[0].candidate.path,
            members[-1].candidate.path,
        )
        count = f"{elements} elements"
        if elements != len(members):
            count += f", {len(members)} tags"
        upload.sections[members[0].section].items.append(
            ArrayRow(
                key=item,
                label=label,
                count=count,
                comment=members[0].candidate.comment,
                selected=item in selected_arrays,
                expanded=item in expanded,
                problems=sum(
                    1
                    for member in members
                    if member.base_problem is not None or member.key in errors
                ),
            )
        )
        shown.add(item)
    preview.shown_rows = len(visible)
    preview.hidden_selected = sorted(selected_keys - shown)
    preview.hidden_arrays = sorted(selected_arrays - shown)
    preview.hidden_names = sorted(
        (key, name)
        for key, name in form.names.items()
        if key in selected_keys and key not in shown
    )
    return preview


def _visible(
    upload: _Upload, needle: str, expanded: Collection[str]
) -> tuple[list[_Entry | str], int]:
    """The rows to render (an array key for a collapsed array), at most
    ``MAX_PREVIEW_ROWS``, and how many rows match the filter in all."""
    visible: list[_Entry | str] = []
    matching = 0
    entries = upload.entries
    index = 0
    while index < len(entries):
        entry = entries[index]
        if entry.group is None:
            index += 1
            rows: list[_Entry | str] = [entry] if _matches(entry, needle) else []
        else:
            members = upload.arrays[entry.group]
            index += len(members)
            found = [member for member in members if _matches(member, needle)]
            rows = [entry.group, *found] if found and entry.group in expanded else []
            if found and entry.group not in expanded:
                rows = [entry.group]
        matching += len(rows)
        visible.extend(rows[: max(0, MAX_PREVIEW_ROWS - len(visible))])
    return visible, matching


def _existing_addresses(
    existing: Sequence[TagConfig], connection: ConnectionConfig
) -> dict[Hashable, str]:
    form = S7TagForm()
    found: dict[Hashable, str] = {}
    for tag in existing:
        found.setdefault(form.register_key(tag, connection), tag.id)
    return found


def _check_selection(
    chosen: Sequence[_Entry],
    names: Mapping[str, str],
    connection_id: str,
    existing_by_address: Mapping[Hashable, str],
    taken_ids: Collection[str],
    group_id: str,
    errors: dict[str, str],
    ids: dict[str, str],
) -> list[TagConfig]:
    """Give each chosen row its Tag ID and Tag, or its problem in ``errors``."""
    taken = set(taken_ids)
    seen: dict[tuple[object, ...], str] = {}
    tags: list[TagConfig] = []
    for entry in chosen:
        candidate = entry.candidate
        name = names.get(entry.key, "") or candidate.path
        if entry.base_problem is not None or candidate.address is None:
            errors[entry.key] = entry.base_problem or NUMBER_MISSING
            continue
        address_key = _address_key(candidate.address)
        holder = existing_by_address.get(address_key)
        if holder is not None:
            errors[entry.key] = f"Same address as Tag {holder}."
            continue
        earlier = seen.get(address_key)
        if earlier is not None:
            errors[entry.key] = f"Same address as {earlier}."
            continue
        seen[address_key] = name
        tag_id = generate_tag_id(connection_id, name, taken)
        try:
            tag = _build_tag(candidate, tag_id, name, group_id or "pending")
        except ConfigurationError as error:
            errors[entry.key] = error.message
            continue
        taken.add(tag_id)
        ids[entry.key] = tag_id
        tags.append(tag)
    return tags
