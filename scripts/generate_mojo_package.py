#!/usr/bin/env python3
"""Generate crate-agnostic ergonomic Mojo wrappers from a reviewed ABI map.

The Diplomat backend owns ``_ffi.mojo`` and ``abi-report.json``.  This script
owns the small, deliberately closed semantic layer described by
``[[mojo.types]]`` and ``[[mojo.functions]]`` in ``binding.toml``.  It never
guesses that a bridge item is safe or useful: every HIR type and function must
be exported with an explicit mapping or skipped with a reason.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
import tomllib
from typing import Any, Iterable


GENERATOR_NAME = "rust-mojo-wrapper-generator"
GENERATOR_VERSION = "0.1.0"
REPORT_SCHEMA_VERSION = 3
GENERATED_FILES = ("_runtime.mojo", "_types.mojo", "_wrappers.mojo", "__init__.mojo")
_ABI_MODEL_FIELDS = (
    "structs",
    "opaques",
    "enums",
    "functions",
    "types",
    "unsupported",
)
_RAW_BACKEND_HEADER = re.compile(
    r"\A# GENERATED FILE — DO NOT EDIT DIRECTLY\n"
    r"# Generator: diplomat-gen-mojo ([^\n]+)\n"
    r"# Diplomat core: ([^\n]+)\n"
    r"# ABI model SHA-256: ([0-9a-f]{64})\n\n"
)
_FINAL_BACKEND_HEADER = re.compile(
    r"\A# GENERATED FILE — DO NOT EDIT DIRECTLY\n"
    r"# Source crate: [^\n]+\n"
    r"# Binding manifest: binding\.toml\n"
    r"# Generator: [^\n]+\n"
    r"# Diplomat: [^\n]+\n"
    r"# Diplomat core: [^\n]+\n"
    r"# Diplomat runtime: [^\n]+\n"
    r"# Mojo compiler: [^\n]+\n"
    r"# ABI backend: diplomat-gen-mojo ([^\n]+)\n"
    r"# ABI backend Diplomat core: ([^\n]+)\n"
    r"# ABI model SHA-256: ([0-9a-f]{64})\n\n"
)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESERVED = {
    "Self",
    "True",
    "False",
    "None",
    "alias",
    "as",
    "assert",
    "async",
    "await",
    "break",
    "comptime",
    "continue",
    "def",
    "elif",
    "else",
    "fn",
    "for",
    "from",
    "if",
    "import",
    "in",
    "let",
    "mut",
    "out",
    "owned",
    "raise",
    "raises",
    "read",
    "ref",
    "return",
    "self",
    "std",
    "struct",
    "trait",
    "try",
    "type",
    "var",
    "while",
    "with",
    "yield",
}
_PRIMITIVES = {
    "bool": "Bool",
    "int8": "Int8",
    "u_int8": "UInt8",
    "int16": "Int16",
    "u_int16": "UInt16",
    "int32": "Int32",
    "u_int32": "UInt32",
    "int64": "Int64",
    "u_int64": "UInt64",
    "int": "Int",
    "u_int": "UInt",
    "float32": "Float32",
    "float64": "Float64",
}


class GenerationError(RuntimeError):
    """A deterministic manifest/report error the calling agent can repair."""


@dataclass(frozen=True)
class TypeMapping:
    abi_name: str
    mojo_name: str | None
    kind: str
    reason: str | None
    destroy_abi_symbol: str | None
    item_field: str | None
    status_field: str | None
    finished_discriminant: int | None
    next_style: str | None
    out_param: str | None
    item_discriminant: int | None
    error_discriminants: tuple[tuple[int, str], ...]
    export_id: str | None


@dataclass(frozen=True)
class ResultPolicy:
    status_field: str | None
    ok_discriminant: int
    errors: tuple[tuple[int, str], ...]
    value_fields: tuple[str, ...]
    mojo_type: str | None
    value_names: tuple[tuple[str, str], ...]
    out_value: bool


@dataclass(frozen=True)
class ScalarizedParam:
    name: str
    kind: str
    element: str
    data_param: str
    len_param: str


@dataclass(frozen=True)
class FunctionMapping:
    abi_owner: str | None
    rust_name: str
    abi_symbol: str
    mojo_name: str | None
    kind: str
    reason: str | None
    scalarized_params: tuple[ScalarizedParam, ...]
    out_param: str | None
    result: ResultPolicy | None
    optional_none_error: str | None
    export_id: str | None


@dataclass(frozen=True)
class ExportRecord:
    id: str
    rust: str
    mojo: str | None
    status: str
    reason: str


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise GenerationError(f"Missing binding manifest: {path}") from error
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise GenerationError(f"Cannot read TOML from {path}: {error}") from error


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise GenerationError(f"Missing ABI report: {path}") from error
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GenerationError(f"Cannot read JSON from {path}: {error}") from error
    if not isinstance(value, dict):
        raise GenerationError(f"ABI report must be a JSON object: {path}")
    return value


def _abi_model_sha256(report: dict[str, Any]) -> str:
    payload = {key: report.get(key) for key in _ABI_MODEL_FIELDS}
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _table(document: dict[str, Any], key: str, source: str) -> dict[str, Any]:
    value = document.get(key)
    if not isinstance(value, dict):
        raise GenerationError(f"{source} requires a [{key}] table")
    return value


def _string(table: dict[str, Any], key: str, label: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value or value != value.strip():
        raise GenerationError(f"{label} must be a non-empty trimmed string")
    if any(character in value for character in "\r\n\0"):
        raise GenerationError(f"{label} must be a single-line string")
    return value


def _optional_string(table: dict[str, Any], key: str, label: str) -> str | None:
    if key not in table:
        return None
    return _string(table, key, label)


def _optional_manifest_text(
    table: dict[str, Any], key: str, label: str
) -> str | None:
    """Read an optional audit-ledger string, normalizing an empty value away."""
    if key not in table:
        return None
    value = table[key]
    if not isinstance(value, str) or value != value.strip():
        raise GenerationError(f"{label} must be a trimmed string")
    if any(character in value for character in "\r\n\0"):
        raise GenerationError(f"{label} must be a single-line string")
    return value or None


def _identifier(value: str, label: str) -> str:
    if _IDENTIFIER.fullmatch(value) is None or value in _RESERVED:
        raise GenerationError(f"{label} is not a usable Mojo identifier: {value!r}")
    return value


def _array_of_tables(table: dict[str, Any], key: str, label: str) -> list[dict[str, Any]]:
    value = table.get(key)
    if not isinstance(value, list):
        raise GenerationError(f"{label} must be an array of tables")
    if not all(isinstance(item, dict) for item in value):
        raise GenerationError(f"Every entry in {label} must be a table")
    return value


def _string_array(table: dict[str, Any], key: str, label: str) -> tuple[str, ...]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item and item == item.strip() for item in value
    ):
        raise GenerationError(f"{label} must be an array of non-empty strings")
    if len(value) != len(set(value)):
        raise GenerationError(f"{label} must not contain duplicates")
    return tuple(value)


def _discriminant_messages(value: Any, label: str) -> tuple[tuple[int, str], ...]:
    if not isinstance(value, dict):
        raise GenerationError(f"{label} must be a table mapping integer strings to messages")
    result: list[tuple[int, str]] = []
    for raw, message in value.items():
        if not isinstance(raw, str) or re.fullmatch(r"-?[0-9]+", raw) is None:
            raise GenerationError(f"{label} key {raw!r} must be an integer string")
        if not isinstance(message, str) or not message or message != message.strip():
            raise GenerationError(f"{label}.{raw} must be a non-empty message")
        result.append((int(raw), message))
    return tuple(sorted(result))


def _parse_result_policy(item: dict[str, Any], label: str) -> ResultPolicy | None:
    value = item.get("result")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise GenerationError(f"{label}.result must be a table")
    allowed = {
        "status_field",
        "ok_discriminant",
        "errors",
        "value_fields",
        "mojo_type",
        "value_names",
        "out_value",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise GenerationError(
            f"{label}.result has unknown keys: {', '.join(unknown)}"
        )
    status_field = _optional_string(value, "status_field", f"{label}.result.status_field")
    if status_field is not None and _IDENTIFIER.fullmatch(status_field) is None:
        raise GenerationError(f"{label}.result.status_field must be an identifier")
    ok = value.get("ok_discriminant")
    if not isinstance(ok, int):
        raise GenerationError(f"{label}.result.ok_discriminant must be an integer")
    if "errors" not in value:
        raise GenerationError(f"{label}.result.errors is required (use an empty table if needed)")
    errors = _discriminant_messages(value["errors"], f"{label}.result.errors")
    if ok in {code for code, _ in errors}:
        raise GenerationError(f"{label}.result ok discriminant also appears in errors")
    fields = _string_array(value, "value_fields", f"{label}.result.value_fields")
    for field in fields:
        if _IDENTIFIER.fullmatch(field) is None:
            raise GenerationError(f"{label}.result value field {field!r} is not an identifier")
    mojo_type = _optional_string(value, "mojo_type", f"{label}.result.mojo_type")
    out_value = value.get("out_value", False)
    if not isinstance(out_value, bool):
        raise GenerationError(f"{label}.result.out_value must be a boolean")
    if out_value and (fields or mojo_type is not None or "value_names" in value):
        raise GenerationError(
            f"{label}.result.out_value cannot be combined with field projection"
        )
    if len(fields) > 1:
        if mojo_type is None:
            raise GenerationError(f"{label}.result with multiple fields requires mojo_type")
        _identifier(mojo_type, f"{label}.result.mojo_type")
    elif mojo_type is not None:
        raise GenerationError(f"{label}.result.mojo_type is only valid with multiple fields")
    raw_names = value.get("value_names", {})
    if not isinstance(raw_names, dict) or not all(
        isinstance(source, str)
        and isinstance(target, str)
        and target
        and _IDENTIFIER.fullmatch(target) is not None
        and target not in _RESERVED
        for source, target in raw_names.items()
    ):
        raise GenerationError(
            f"{label}.result.value_names must map source fields to Mojo identifiers"
        )
    if set(raw_names) - set(fields):
        raise GenerationError(
            f"{label}.result.value_names contains fields not in value_fields"
        )
    names = tuple(
        (field, raw_names.get(field, _mojo_abi_ident(field))) for field in fields
    )
    if len({target for _, target in names}) != len(names):
        raise GenerationError(f"{label}.result value_names must be unique")
    return ResultPolicy(status_field, ok, errors, fields, mojo_type, names, out_value)


def _parse_type_mappings(mojo: dict[str, Any]) -> list[TypeMapping]:
    result: list[TypeMapping] = []
    for index, item in enumerate(_array_of_tables(mojo, "types", "[[mojo.types]]")):
        label = f"[[mojo.types]][{index}]"
        allowed = {
            "abi_name",
            "mojo_name",
            "kind",
            "reason",
            "destroy_abi_symbol",
            "next_style",
            "item_field",
            "status_field",
            "out_param",
            "item_discriminant",
            "finished_discriminant",
            "error_discriminants",
            "export",
        }
        unknown = sorted(set(item) - allowed)
        if unknown:
            raise GenerationError(f"{label} has unknown keys: {', '.join(unknown)}")
        abi_name = _string(item, "abi_name", f"{label}.abi_name")
        kind = _string(item, "kind", f"{label}.kind")
        if kind not in {"value", "enum", "opaque", "iterator", "skip"}:
            raise GenerationError(
                f"{label}.kind must be value, enum, opaque, iterator, or skip"
            )
        mojo_name = _optional_string(item, "mojo_name", f"{label}.mojo_name")
        reason = _optional_string(item, "reason", f"{label}.reason")
        export_id = _optional_string(item, "export", f"{label}.export")
        if kind == "skip":
            if mojo_name is not None:
                raise GenerationError(f"{label} is skipped and must not have mojo_name")
            if reason is None:
                raise GenerationError(f"{label} is skipped and requires reason")
            if export_id is not None:
                raise GenerationError(
                    f"{label} is an internal skipped ABI type and must not link an export"
                )
        else:
            if mojo_name is None:
                raise GenerationError(f"{label} requires mojo_name")
            _identifier(mojo_name, f"{label}.mojo_name")
            if reason is not None:
                raise GenerationError(f"{label} is exported and must not have reason")
            if export_id is None:
                raise GenerationError(f"{label} public type requires export linkage")
        destroy = _optional_string(
            item, "destroy_abi_symbol", f"{label}.destroy_abi_symbol"
        )
        item_field = _optional_string(item, "item_field", f"{label}.item_field")
        status_field = _optional_string(
            item, "status_field", f"{label}.status_field"
        )
        finished = item.get("finished_discriminant")
        next_style = _optional_string(item, "next_style", f"{label}.next_style")
        out_param = _optional_string(item, "out_param", f"{label}.out_param")
        item_discriminant = item.get("item_discriminant")
        error_discriminants: tuple[tuple[int, str], ...] = ()
        if kind == "iterator":
            if next_style != "status-out":
                raise GenerationError(
                    f"{label} iterator requires next_style=status-out in schema v1; "
                    "step structs returned by value are unsafe with Mojo 1.0 dynamic FFI"
                )
            if not isinstance(finished, int):
                raise GenerationError(f"{label} iterator requires integer finished_discriminant")
            if not isinstance(item_discriminant, int):
                raise GenerationError(f"{label} iterator requires integer item_discriminant")
            if "error_discriminants" not in item:
                raise GenerationError(
                    f"{label} iterator requires error_discriminants (use an empty table if needed)"
                )
            error_discriminants = _discriminant_messages(
                item["error_discriminants"], f"{label}.error_discriminants"
            )
            used = {item_discriminant, finished} | {
                code for code, _ in error_discriminants
            }
            if len(used) != 2 + len(error_discriminants):
                raise GenerationError(f"{label} iterator discriminants must be distinct")
            if out_param is None or item_field is not None or status_field is not None:
                raise GenerationError(
                    f"{label} status-out iterator requires out_param and no item/status fields"
                )
            if _IDENTIFIER.fullmatch(out_param) is None:
                raise GenerationError(f"{label}.out_param must be an identifier")
        elif any(
            value is not None
            for value in (
                item_field,
                status_field,
                finished,
                next_style,
                out_param,
                item_discriminant,
            )
        ):
            raise GenerationError(f"{label} iterator metadata is only valid for kind=iterator")
        elif "error_discriminants" in item:
            raise GenerationError(f"{label} iterator metadata is only valid for kind=iterator")
        result.append(
            TypeMapping(
                abi_name,
                mojo_name,
                kind,
                reason,
                destroy,
                item_field,
                status_field,
                finished if isinstance(finished, int) else None,
                next_style,
                out_param,
                item_discriminant if isinstance(item_discriminant, int) else None,
                error_discriminants,
                export_id,
            )
        )
    return result


def _parse_scalarized_params(item: dict[str, Any], label: str) -> tuple[ScalarizedParam, ...]:
    raw = item.get("scalarized_params", [])
    if not isinstance(raw, list) or not all(isinstance(value, dict) for value in raw):
        raise GenerationError(f"{label}.scalarized_params must be an array of tables")
    result: list[ScalarizedParam] = []
    public_names: set[str] = set()
    raw_names: set[str] = set()
    kinds = {"copied-value-slice", "borrowed-primitive-slice", "utf8-string"}
    for index, value in enumerate(raw):
        adapter_label = f"{label}.scalarized_params[{index}]"
        allowed = {"name", "kind", "element", "data_param", "len_param"}
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise GenerationError(
                f"{adapter_label} has unknown keys: {', '.join(unknown)}"
            )
        name = _identifier(_string(value, "name", f"{adapter_label}.name"), f"{adapter_label}.name")
        kind = _string(value, "kind", f"{adapter_label}.kind")
        if kind not in kinds:
            raise GenerationError(f"{adapter_label}.kind must be one of {sorted(kinds)}")
        element = _string(value, "element", f"{adapter_label}.element")
        data_param = _string(value, "data_param", f"{adapter_label}.data_param")
        len_param = _string(value, "len_param", f"{adapter_label}.len_param")
        if _IDENTIFIER.fullmatch(data_param) is None or _IDENTIFIER.fullmatch(len_param) is None:
            raise GenerationError(f"{adapter_label} raw parameter names must be identifiers")
        if data_param == len_param:
            raise GenerationError(f"{adapter_label} data_param and len_param must differ")
        if name in public_names:
            raise GenerationError(f"{label} repeats scalarized public parameter {name!r}")
        overlap = {data_param, len_param} & raw_names
        if overlap:
            raise GenerationError(
                f"{label} reuses scalarized ABI parameters: {', '.join(sorted(overlap))}"
            )
        if kind == "utf8-string" and element != "u_int8":
            raise GenerationError(f"{adapter_label} utf8-string element must be 'u_int8'")
        if kind == "borrowed-primitive-slice" and element not in _PRIMITIVES:
            raise GenerationError(
                f"{adapter_label} element must be an ABI primitive spelling"
            )
        public_names.add(name)
        raw_names.update((data_param, len_param))
        result.append(ScalarizedParam(name, kind, element, data_param, len_param))
    return tuple(result)


def _parse_function_mappings(mojo: dict[str, Any]) -> list[FunctionMapping]:
    result: list[FunctionMapping] = []
    kinds = {
        "constructor",
        "named_constructor",
        "static",
        "method",
        "free",
        "iterator_next",
        "skip",
    }
    for index, item in enumerate(
        _array_of_tables(mojo, "functions", "[[mojo.functions]]")
    ):
        label = f"[[mojo.functions]][{index}]"
        allowed = {
            "abi_owner",
            "rust_name",
            "abi_symbol",
            "mojo_name",
            "kind",
            "reason",
            "scalarized_params",
            "out_param",
            "result",
            "optional_none_error",
            "export",
        }
        unknown = sorted(set(item) - allowed)
        if unknown:
            raise GenerationError(f"{label} has unknown keys: {', '.join(unknown)}")
        owner = _optional_string(item, "abi_owner", f"{label}.abi_owner")
        rust_name = _string(item, "rust_name", f"{label}.rust_name")
        symbol = _string(item, "abi_symbol", f"{label}.abi_symbol")
        kind = _string(item, "kind", f"{label}.kind")
        if kind not in kinds:
            raise GenerationError(f"{label}.kind must be one of {sorted(kinds)}")
        mojo_name = _optional_string(item, "mojo_name", f"{label}.mojo_name")
        reason = _optional_string(item, "reason", f"{label}.reason")
        export_id = _optional_string(item, "export", f"{label}.export")
        if kind == "skip":
            if mojo_name is not None:
                raise GenerationError(f"{label} is skipped and must not have mojo_name")
            if reason is None:
                raise GenerationError(f"{label} is skipped and requires reason")
            if export_id is not None:
                raise GenerationError(
                    f"{label} is an internal skipped ABI callable and must not link an export"
                )
        else:
            if mojo_name is None:
                raise GenerationError(f"{label} requires mojo_name")
            _identifier(mojo_name, f"{label}.mojo_name")
            if reason is not None:
                raise GenerationError(f"{label} is exported and must not have reason")
            if export_id is None:
                raise GenerationError(f"{label} public callable requires export linkage")
        scalarized_params = _parse_scalarized_params(item, label)
        out_param = _optional_string(item, "out_param", f"{label}.out_param")
        if out_param is not None and _IDENTIFIER.fullmatch(out_param) is None:
            raise GenerationError(f"{label}.out_param must be an ABI parameter identifier")
        result_policy = _parse_result_policy(item, label)
        optional_none_error = _optional_string(
            item, "optional_none_error", f"{label}.optional_none_error"
        )
        if kind == "skip" and (
            scalarized_params
            or out_param is not None
            or result_policy is not None
            or optional_none_error is not None
        ):
            raise GenerationError(f"{label} skipped functions cannot declare adapters")
        if kind == "iterator_next" and (
            scalarized_params
            or out_param is not None
            or result_policy is not None
            or optional_none_error is not None
        ):
            raise GenerationError(
                f"{label} iterator_next adapters belong on its kind=iterator type mapping"
            )
        result.append(
            FunctionMapping(
                owner,
                rust_name,
                symbol,
                mojo_name,
                kind,
                reason,
                scalarized_params,
                out_param,
                result_policy,
                optional_none_error,
                export_id,
            )
        )
    return result


def _parse_exports(manifest: dict[str, Any]) -> list[ExportRecord]:
    raw = manifest.get("exports")
    if not isinstance(raw, list) or not raw:
        raise GenerationError("binding.toml requires at least one [[exports]] record")
    if not all(isinstance(item, dict) for item in raw):
        raise GenerationError("Every [[exports]] entry must be a table")
    statuses = {"DIRECT", "ADAPTED", "MONOMORPHIZED", "OPAQUE", "SKIPPED"}
    result: list[ExportRecord] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw):
        label = f"[[exports]][{index}]"
        allowed = {"id", "rust", "mojo", "status", "reason"}
        unknown = sorted(set(item) - allowed)
        if unknown:
            raise GenerationError(f"{label} has unknown keys: {', '.join(unknown)}")
        export_id = _string(item, "id", f"{label}.id")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", export_id) is None:
            raise GenerationError(f"{label}.id is not a stable export identifier")
        if export_id in seen_ids:
            raise GenerationError(f"Export id {export_id!r} is declared more than once")
        seen_ids.add(export_id)
        rust = _string(item, "rust", f"{label}.rust")
        mojo = _optional_manifest_text(item, "mojo", f"{label}.mojo")
        status = _string(item, "status", f"{label}.status")
        if status not in statuses:
            raise GenerationError(
                f"{label}.status must be one of {sorted(statuses)}"
            )
        reason = _string(item, "reason", f"{label}.reason")
        if status == "SKIPPED" and mojo is not None:
            raise GenerationError(f"{label} is SKIPPED and must not expose a Mojo name")
        if status != "SKIPPED" and mojo is None:
            raise GenerationError(f"{label} requires a Mojo name unless it is SKIPPED")
        result.append(ExportRecord(export_id, rust, mojo, status, reason))
    return result


def _validate_reviewed_decisions(
    manifest: dict[str, Any], exports: list[ExportRecord]
) -> None:
    """Require an auditable user decision for concrete generics/adaptations."""

    exports_by_id = {export.id: export for export in exports}
    specializations = manifest.get("specializations", [])
    if not isinstance(specializations, list) or not all(
        isinstance(item, dict) for item in specializations
    ):
        raise GenerationError("[[specializations]] must be an array of tables")
    specialization_keys: set[tuple[str, str]] = set()
    specialization_links: set[str] = set()
    for index, item in enumerate(specializations):
        label = f"[[specializations]][{index}]"
        allowed = {"rust_type", "mojo_name", "parameters", "chosen_by", "exports"}
        unknown = sorted(set(item) - allowed)
        if unknown:
            raise GenerationError(f"{label} has unknown keys: {', '.join(unknown)}")
        rust_type = _string(item, "rust_type", f"{label}.rust_type")
        mojo_name = _identifier(
            _string(item, "mojo_name", f"{label}.mojo_name"),
            f"{label}.mojo_name",
        )
        parameters = item.get("parameters")
        if not isinstance(parameters, dict) or not parameters:
            raise GenerationError(f"{label}.parameters must be a non-empty table")
        for name, value in parameters.items():
            if (
                not isinstance(name, str)
                or not name
                or not isinstance(value, str)
                or not value
                or value != value.strip()
                or any(character in value for character in "\r\n\0")
            ):
                raise GenerationError(
                    f"{label}.parameters must map names to concrete single-line Rust values"
                )
        linked_exports = _string_array(item, "exports", f"{label}.exports")
        if not linked_exports:
            raise GenerationError(f"{label}.exports must name at least one export")
        for export_id in linked_exports:
            export = exports_by_id.get(export_id)
            if export is None:
                raise GenerationError(
                    f"{label}.exports references unknown export id {export_id!r}"
                )
            if export.status == "SKIPPED":
                raise GenerationError(
                    f"{label}.exports must not reference SKIPPED export {export_id!r}"
                )
            specialization_links.add(export_id)
        if item.get("chosen_by") != "user":
            raise GenerationError(
                f"{label}.chosen_by must be 'user'; unresolved concrete generic "
                "choices require user confirmation"
            )
        key = (rust_type, mojo_name)
        if key in specialization_keys:
            raise GenerationError(
                f"{label} duplicates specialization {rust_type!r} as {mojo_name!r}"
            )
        specialization_keys.add(key)
    missing_specializations = sorted(
        export.id
        for export in exports
        if export.status == "MONOMORPHIZED" and export.id not in specialization_links
    )
    if missing_specializations:
        raise GenerationError(
            "Every MONOMORPHIZED export requires at least one explicit "
            "[[specializations]].exports link: " + ", ".join(missing_specializations)
        )

    adaptations = manifest.get("adaptations", [])
    if not isinstance(adaptations, list) or not all(
        isinstance(item, dict) for item in adaptations
    ):
        raise GenerationError("[[adaptations]] must be an array of tables")
    adaptation_keys: set[tuple[str, str, str]] = set()
    adaptation_links: dict[str, str] = {}
    for index, item in enumerate(adaptations):
        label = f"[[adaptations]][{index}]"
        allowed = {"rust", "mojo", "kind", "effect", "chosen_by", "export"}
        unknown = sorted(set(item) - allowed)
        if unknown:
            raise GenerationError(f"{label} has unknown keys: {', '.join(unknown)}")
        rust = _string(item, "rust", f"{label}.rust")
        mojo = _string(item, "mojo", f"{label}.mojo")
        kind = _string(item, "kind", f"{label}.kind")
        _string(item, "effect", f"{label}.effect")
        export_id = _string(item, "export", f"{label}.export")
        export = exports_by_id.get(export_id)
        if export is None:
            raise GenerationError(
                f"{label}.export references unknown export id {export_id!r}"
            )
        if export.status != "ADAPTED":
            raise GenerationError(
                f"{label}.export must name an ADAPTED export, not {export.status} "
                f"export {export_id!r}"
            )
        if rust != export.rust:
            raise GenerationError(
                f"{label}.rust must exactly match [[exports]] {export_id!r}.rust "
                f"({export.rust!r})"
            )
        assert export.mojo is not None
        if mojo != export.mojo:
            raise GenerationError(
                f"{label}.mojo must exactly match [[exports]] {export_id!r}.mojo "
                f"({export.mojo!r})"
            )
        previous_link = adaptation_links.get(export_id)
        if previous_link is not None:
            raise GenerationError(
                f"{label}.export duplicates adaptation link for {export_id!r} "
                f"already declared by {previous_link}"
            )
        adaptation_links[export_id] = label
        if item.get("chosen_by") != "user":
            raise GenerationError(
                f"{label}.chosen_by must be 'user'; material semantic "
                "adaptations require user confirmation"
            )
        key = (rust, mojo, kind)
        if key in adaptation_keys:
            raise GenerationError(
                f"{label} duplicates adaptation {rust!r} to {mojo!r}"
            )
        adaptation_keys.add(key)
    missing_adaptations = sorted(
        export.id
        for export in exports
        if export.status == "ADAPTED" and export.id not in adaptation_links
    )
    if missing_adaptations:
        raise GenerationError(
            "Every ADAPTED export requires exactly one explicit [[adaptations]].export "
            "link: " + ", ".join(missing_adaptations)
        )


def _validate_scope(
    manifest: dict[str, Any],
    manifest_label: str,
    exports: list[ExportRecord],
) -> None:
    scope = _table(manifest, "scope", manifest_label)
    unknown = sorted(set(scope) - {"requested", "trait_policy", "audited_exports"})
    if unknown:
        raise GenerationError(f"[scope] has unknown keys: {', '.join(unknown)}")
    requested = _string_array(scope, "requested", "[scope].requested")
    if not requested:
        raise GenerationError("[scope].requested must name at least one Rust API item")
    _string(scope, "trait_policy", "[scope].trait_policy")
    audited_exports = _string_array(
        scope, "audited_exports", "[scope].audited_exports"
    )
    if not audited_exports:
        raise GenerationError(
            "[scope].audited_exports must name every [[exports]] record"
        )
    audited_ids = set(audited_exports)
    export_ids = {export.id for export in exports}
    if audited_ids != export_ids:
        details: list[str] = []
        omitted = sorted(export_ids - audited_ids)
        unrelated = sorted(audited_ids - export_ids)
        if omitted:
            details.append("omits " + ", ".join(omitted))
        if unrelated:
            details.append("names unknown ids " + ", ".join(unrelated))
        raise GenerationError(
            "[scope].audited_exports must exactly equal all [[exports]].id values; "
            + "; ".join(details)
        )


def _report_list(report: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = report.get(key)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise GenerationError(f"ABI report field {key!r} must be an array of objects")
    return value


def _mojo_abi_ident(value: str) -> str:
    rendered = "".join(
        character if character.isascii() and (character.isalnum() or character == "_") else "_"
        for character in value
    )
    if not rendered or rendered[0].isdigit():
        rendered = "_" + rendered
    # Keep this in lockstep with diplomat-gen-mojo's deterministic escaping.
    if rendered in _RESERVED or rendered in set(_PRIMITIVES.values()) | {
        "Pointer",
        "OptionalPointer",
        "MutUntrackedOrigin",
        "ImmUntrackedOrigin",
        "RegisterPassable",
        "TrivialRegisterPassable",
    }:
        rendered += "_"
    return rendered


def _kind(ty: dict[str, Any], label: str) -> tuple[str, Any]:
    kind = ty.get("kind")
    if not isinstance(kind, str):
        raise GenerationError(f"{label}.kind must be a string")
    return kind, ty.get("details")


def _raw_type(ty: dict[str, Any], label: str) -> str:
    kind, details = _kind(ty, label)
    if kind == "unit":
        return "NoneType"
    if kind == "primitive":
        if details not in _PRIMITIVES:
            raise GenerationError(f"{label} has unknown primitive {details!r}")
        return _PRIMITIVES[details]
    if kind in {"struct", "enum"}:
        if not isinstance(details, str):
            raise GenerationError(f"{label}.details must name a {kind}")
        return f"_ffi.{_mojo_abi_ident(details)}"
    if kind == "struct_pointer":
        if not isinstance(details, dict) or not isinstance(details.get("name"), str):
            raise GenerationError(f"{label}.details must describe a struct pointer")
        origin = "MutUntrackedOrigin" if details.get("mutable") is True else "ImmUntrackedOrigin"
        return f"Pointer[_ffi.{_mojo_abi_ident(details['name'])}, {origin}]"
    if kind == "opaque_pointer":
        if not isinstance(details, dict) or not isinstance(details.get("name"), str):
            raise GenerationError(f"{label}.details must describe an opaque pointer")
        suffix = _opaque_suffix(details, label)
        return f"_ffi.{_mojo_abi_ident(details['name'])}{suffix}"
    if kind == "slice":
        if not isinstance(details, dict):
            raise GenerationError(f"{label}.details must describe a slice")
        element = details.get("element")
        if not isinstance(element, dict):
            raise GenerationError(f"{label}.details.element must be an ABI type")
        ownership = details.get("ownership")
        mutable = details.get("mutable")
        prefix = (
            "DiplomatOwnedSlice"
            if ownership == "owned"
            else "DiplomatSliceMut"
            if ownership == "borrowed" and mutable is True
            else "DiplomatSlice"
            if ownership == "borrowed" and mutable is False
            else None
        )
        if prefix is None:
            raise GenerationError(f"{label} has invalid slice ownership/mutability")
        return f"_ffi.{prefix}_{_type_suffix(element, label + '.element')}"
    raise GenerationError(f"{label} uses unsupported ABI type kind {kind!r}")


def _ffi_declaration_type(ty: dict[str, Any], label: str) -> str:
    """Render a report type as it must appear inside backend-owned _ffi.mojo."""

    rendered = _raw_type(ty, label)
    if rendered == "NoneType":
        return "None"
    return rendered.replace("_ffi.", "")


def _type_suffix(ty: dict[str, Any], label: str) -> str:
    kind, details = _kind(ty, label)
    if kind == "primitive" and details in _PRIMITIVES:
        return _PRIMITIVES[details]
    if kind in {"struct", "enum"} and isinstance(details, str):
        return _mojo_abi_ident(details)
    raise GenerationError(f"{label} cannot be represented as a Diplomat slice element")


def _opaque_suffix(details: dict[str, Any], label: str) -> str:
    values = (details.get("optional"), details.get("owned"), details.get("mutable"))
    if not all(isinstance(value, bool) for value in values):
        raise GenerationError(f"{label} opaque pointer flags must be booleans")
    optional, owned, mutable = values
    return {
        (False, True, False): "Handle",
        (False, True, True): "Handle",
        (False, False, False): "Ref",
        (False, False, True): "MutRef",
        (True, True, False): "OptionalHandle",
        (True, True, True): "OptionalHandle",
        (True, False, False): "OptionalRef",
        (True, False, True): "OptionalMutRef",
    }[(optional, owned, mutable)]


class Model:
    def __init__(
        self,
        manifest: dict[str, Any],
        report: dict[str, Any],
        *,
        manifest_label: str,
    ) -> None:
        if manifest.get("schema_version") != 1:
            raise GenerationError("binding.toml schema_version must be 1")
        if report.get("schema_version") != REPORT_SCHEMA_VERSION:
            raise GenerationError(
                f"ABI report schema_version must be {REPORT_SCHEMA_VERSION}"
            )
        unsupported = _report_list(report, "unsupported")
        if unsupported:
            rendered = "; ".join(
                f"{item.get('item', '<unknown>')}: {item.get('reason', '<no reason>')}"
                for item in unsupported
            )
            raise GenerationError(f"ABI report contains unsupported HIR items: {rendered}")

        binding = _table(manifest, "binding", manifest_label)
        crate = _table(manifest, "crate", manifest_label)
        ffi = _table(manifest, "ffi", manifest_label)
        mojo = _table(manifest, "mojo", manifest_label)
        self.exports = _parse_exports(manifest)
        _validate_scope(manifest, manifest_label, self.exports)
        unknown_mojo = sorted(
            set(mojo) - {"source_dir", "tests", "types", "functions"}
        )
        if unknown_mojo:
            raise GenerationError(
                f"[mojo] has unknown keys: {', '.join(unknown_mojo)}"
            )
        tools = _table(manifest, "tools", manifest_label)
        allowed_tools = {
            "generator_version",
            "abi_backend_version",
            "diplomat_version",
            "diplomat_core_version",
            "diplomat_runtime_version",
            "mojo_version",
        }
        unknown_tools = sorted(set(tools) - allowed_tools)
        if unknown_tools:
            raise GenerationError(
                f"[tools] has unknown keys: {', '.join(unknown_tools)}"
            )
        self.binding_id = _string(binding, "id", "[binding].id")
        if re.fullmatch(r"[a-z][a-z0-9_-]*", self.binding_id) is None:
            raise GenerationError(
                "[binding].id must start with a lowercase letter and contain only "
                "lowercase letters, digits, underscores, or hyphens"
            )
        self.symbol_prefix = _string(
            binding, "symbol_prefix", "[binding].symbol_prefix"
        )
        expected_prefix = "rust_mojo__" + self.binding_id.replace("-", "_") + "__"
        if self.symbol_prefix != expected_prefix:
            raise GenerationError(
                f"[binding].symbol_prefix must be the deterministic prefix "
                f"{expected_prefix!r} for binding id {self.binding_id!r}"
            )
        self.package = _identifier(
            _string(binding, "mojo_package", "[binding].mojo_package"),
            "[binding].mojo_package",
        )
        self.crate_name = _string(crate, "name", "[crate].name")
        self.crate_version = _string(crate, "version", "[crate].version")
        self.ffi_crate = _string(ffi, "crate_name", "[ffi].crate_name")
        if _IDENTIFIER.fullmatch(self.ffi_crate) is None:
            raise GenerationError("[ffi].crate_name must be a Rust library identifier")
        self.generator_version = _string(
            tools, "generator_version", "[tools].generator_version"
        )
        self.abi_backend_version = _string(
            tools, "abi_backend_version", "[tools].abi_backend_version"
        )
        self.diplomat_version = _string(
            tools, "diplomat_version", "[tools].diplomat_version"
        )
        self.diplomat_core_version = _string(
            tools, "diplomat_core_version", "[tools].diplomat_core_version"
        )
        self.diplomat_runtime_version = _string(
            tools, "diplomat_runtime_version", "[tools].diplomat_runtime_version"
        )
        self.mojo_version = _string(tools, "mojo_version", "[tools].mojo_version")
        for key, value in (
            ("generator_version", self.generator_version),
            ("abi_backend_version", self.abi_backend_version),
            ("diplomat_version", self.diplomat_version),
            ("diplomat_core_version", self.diplomat_core_version),
            ("diplomat_runtime_version", self.diplomat_runtime_version),
            ("mojo_version", self.mojo_version),
        ):
            if re.fullmatch(
                r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", value
            ) is None:
                raise GenerationError(f"[tools].{key} must be an exact version")
        backend = report.get("backend")
        if not isinstance(backend, dict) or set(backend) != {
            "name",
            "version",
            "diplomat_core_version",
        }:
            raise GenerationError(
                "ABI report backend must contain exactly name, version, and "
                "diplomat_core_version"
            )
        if backend.get("name") != "diplomat-gen-mojo":
            raise GenerationError("ABI report was not produced by diplomat-gen-mojo")
        if backend.get("version") != self.abi_backend_version:
            raise GenerationError(
                "ABI report backend version does not match "
                "[tools].abi_backend_version"
            )
        if backend.get("diplomat_core_version") != self.diplomat_core_version:
            raise GenerationError(
                "ABI report Diplomat core version does not match "
                "[tools].diplomat_core_version"
            )
        self.mojo_body_sha256 = report.get("mojo_body_sha256")
        if not isinstance(self.mojo_body_sha256, str) or re.fullmatch(
            r"[0-9a-f]{64}", self.mojo_body_sha256
        ) is None:
            raise GenerationError(
                "ABI report mojo_body_sha256 must be 64 lowercase hexadecimal characters"
            )
        self.abi_model_sha256 = report.get("abi_model_sha256")
        if not isinstance(self.abi_model_sha256, str) or re.fullmatch(
            r"[0-9a-f]{64}", self.abi_model_sha256
        ) is None:
            raise GenerationError(
                "ABI report abi_model_sha256 must be 64 lowercase hexadecimal characters"
            )
        self.types = _parse_type_mappings(mojo)
        self.functions = _parse_function_mappings(mojo)
        _validate_reviewed_decisions(manifest, self.exports)
        self.report_structs = _report_list(report, "structs")
        self.report_opaques = _report_list(report, "opaques")
        self.report_enums = _report_list(report, "enums")
        self.report_functions = _report_list(report, "functions")
        # Validate the field even though generation walks concrete declarations.
        _report_list(report, "types")
        self._validate_closed_mapping()
        if _abi_model_sha256(report) != self.abi_model_sha256:
            raise GenerationError(
                "ABI report declarations do not match abi_model_sha256; rerun "
                "diplomat-gen-mojo and do not edit abi-report.json"
            )
        self.type_by_abi = {mapping.abi_name: mapping for mapping in self.types}
        self.function_by_symbol = {
            mapping.abi_symbol: mapping for mapping in self.functions
        }

    def _validate_closed_mapping(self) -> None:
        definitions: dict[str, tuple[str, dict[str, Any]]] = {}
        abi_aliases: dict[str, str] = {}
        for report_kind, items in (
            ("value", self.report_structs),
            ("opaque", self.report_opaques),
            ("enum", self.report_enums),
        ):
            for item in items:
                name = item.get("name")
                if not isinstance(name, str) or not name:
                    raise GenerationError(f"ABI report {report_kind} has an invalid name")
                if name in definitions:
                    raise GenerationError(f"ABI report defines type {name!r} more than once")
                definitions[name] = (report_kind, item)
                if report_kind == "enum":
                    variants = item.get("variants")
                    if not isinstance(variants, list) or not variants:
                        raise GenerationError(
                            f"ABI report enum {name!r} must have at least one variant"
                        )
                    variant_names: set[str] = set()
                    discriminants: set[int] = set()
                    for variant in variants:
                        if not isinstance(variant, dict):
                            raise GenerationError(
                                f"ABI report enum {name!r} has a malformed variant"
                            )
                        variant_name = variant.get("name")
                        discriminant = variant.get("discriminant")
                        if not isinstance(variant_name, str) or not variant_name:
                            raise GenerationError(
                                f"ABI report enum {name!r} has an invalid variant name"
                            )
                        if type(discriminant) is not int:
                            raise GenerationError(
                                f"ABI report enum {name!r} variant {variant_name!r} "
                                "has a non-integer discriminant"
                            )
                        if variant_name in variant_names or discriminant in discriminants:
                            raise GenerationError(
                                f"ABI report enum {name!r} repeats a name or discriminant"
                            )
                        variant_names.add(variant_name)
                        discriminants.add(discriminant)
                if report_kind == "opaque":
                    alias = item.get("destructor_mojo_abi_alias")
                    if not isinstance(alias, str):
                        raise GenerationError(
                            f"ABI report opaque {name!r} lacks destructor_mojo_abi_alias"
                        )
                    _identifier(alias, f"ABI report opaque {name}.destructor_mojo_abi_alias")
                    if not alias.endswith("_abi"):
                        raise GenerationError(
                            f"ABI report opaque {name!r} destructor alias must end in '_abi'"
                        )
                    previous = abi_aliases.get(alias)
                    if previous is not None:
                        raise GenerationError(
                            f"ABI alias {alias!r} collides between {previous} and opaque {name}"
                        )
                    abi_aliases[alias] = f"opaque {name} destructor"

        mapped: dict[str, TypeMapping] = {}
        public_names: dict[str, str] = {}
        for mapping in self.types:
            if mapping.abi_name in mapped:
                raise GenerationError(
                    f"ABI type {mapping.abi_name!r} is mapped more than once"
                )
            mapped[mapping.abi_name] = mapping
            if mapping.abi_name not in definitions:
                raise GenerationError(
                    f"Manifest ABI type {mapping.abi_name!r} does not exist in the report"
                )
            report_kind, item = definitions[mapping.abi_name]
            allowed = {report_kind, "skip"}
            if report_kind == "opaque":
                allowed.add("iterator")
            if mapping.kind not in allowed:
                raise GenerationError(
                    f"ABI type {mapping.abi_name!r} is {report_kind}, not {mapping.kind}"
                )
            if report_kind == "opaque":
                expected = item.get("destructor_abi_name")
                if mapping.kind != "skip" and mapping.destroy_abi_symbol != expected:
                    raise GenerationError(
                        f"{mapping.abi_name} destroy_abi_symbol must be {expected!r}"
                    )
                if isinstance(expected, str) and not expected.startswith(self.symbol_prefix):
                    raise GenerationError(
                        f"Opaque destructor {expected!r} does not use "
                        f"[binding].symbol_prefix {self.symbol_prefix!r}"
                    )
            elif mapping.destroy_abi_symbol is not None:
                raise GenerationError(
                    f"Non-opaque {mapping.abi_name} must not declare destroy_abi_symbol"
                )
            if mapping.mojo_name is not None:
                previous = public_names.get(mapping.mojo_name)
                if previous is not None:
                    raise GenerationError(
                        f"Mojo type name {mapping.mojo_name!r} collides between "
                        f"{previous!r} and {mapping.abi_name!r}"
                    )
                public_names[mapping.mojo_name] = mapping.abi_name
        missing = sorted(set(definitions) - set(mapped))
        if missing:
            raise GenerationError(
                "Closed mapping requires [[mojo.types]] entries for: " + ", ".join(missing)
            )

        report_by_symbol: dict[str, dict[str, Any]] = {}
        for function in self.report_functions:
            symbol = function.get("abi_name")
            if not isinstance(symbol, str) or not symbol:
                raise GenerationError("ABI report function has an invalid abi_name")
            if symbol in report_by_symbol:
                raise GenerationError(f"ABI report repeats ABI symbol {symbol!r}")
            if not symbol.startswith(self.symbol_prefix):
                raise GenerationError(
                    f"ABI symbol {symbol!r} does not use [binding].symbol_prefix "
                    f"{self.symbol_prefix!r}"
                )
            alias = function.get("mojo_abi_alias")
            if not isinstance(alias, str):
                raise GenerationError(f"ABI function {symbol!r} lacks mojo_abi_alias")
            _identifier(alias, f"ABI function {symbol}.mojo_abi_alias")
            if not alias.endswith("_abi"):
                raise GenerationError(
                    f"ABI function {symbol!r} mojo_abi_alias must end in '_abi'"
                )
            previous = abi_aliases.get(alias)
            if previous is not None:
                raise GenerationError(
                    f"ABI alias {alias!r} collides between {previous} and function {symbol}"
                )
            abi_aliases[alias] = f"function {symbol}"
            report_by_symbol[symbol] = function
        mapped_symbols: dict[str, FunctionMapping] = {}
        method_names: dict[tuple[str | None, str], str] = {}
        for mapping in self.functions:
            if mapping.abi_symbol in mapped_symbols:
                raise GenerationError(
                    f"ABI symbol {mapping.abi_symbol!r} is mapped more than once"
                )
            mapped_symbols[mapping.abi_symbol] = mapping
            function = report_by_symbol.get(mapping.abi_symbol)
            if function is None:
                raise GenerationError(
                    f"Manifest ABI symbol {mapping.abi_symbol!r} does not exist in the report"
                )
            if function.get("owner") != mapping.abi_owner or function.get("rust_name") != mapping.rust_name:
                raise GenerationError(
                    f"Manifest symbol {mapping.abi_symbol!r} owner/name does not match "
                    f"report ({function.get('owner')!r}, {function.get('rust_name')!r})"
                )
            if mapping.kind != "skip" and mapping.mojo_name is not None:
                namespace = mapping.abi_owner if mapping.kind != "free" else None
                key = (namespace, mapping.mojo_name)
                previous = method_names.get(key)
                if previous is not None:
                    raise GenerationError(
                        f"Mojo callable {mapping.mojo_name!r} collides between ABI symbols "
                        f"{previous!r} and {mapping.abi_symbol!r}"
                    )
                method_names[key] = mapping.abi_symbol
                if mapping.kind == "constructor" and mapping.mojo_name != "__init__":
                    raise GenerationError(
                        f"Constructor {mapping.abi_symbol} mojo_name must be '__init__'"
                    )
                if mapping.kind == "iterator_next" and mapping.mojo_name != "__next__":
                    raise GenerationError(
                        f"Iterator next {mapping.abi_symbol} mojo_name must be '__next__'"
                    )
        missing_symbols = sorted(set(report_by_symbol) - set(mapped_symbols))
        if missing_symbols:
            raise GenerationError(
                "Closed mapping requires [[mojo.functions]] entries for: "
                + ", ".join(missing_symbols)
            )
        for (namespace, public_name), symbol in method_names.items():
            if namespace is None and public_name in public_names:
                raise GenerationError(
                    f"Top-level Mojo name {public_name!r} collides between type "
                    f"{public_names[public_name]!r} and function {symbol!r}"
                )
        synthetic_names: dict[str, str] = {}
        for mapping in self.functions:
            policy = mapping.result
            if policy is None or policy.mojo_type is None:
                continue
            previous = synthetic_names.get(policy.mojo_type)
            if previous is not None:
                raise GenerationError(
                    f"Synthetic Mojo result type {policy.mojo_type!r} is declared by "
                    f"both {previous!r} and {mapping.abi_symbol!r}"
                )
            if policy.mojo_type in public_names:
                raise GenerationError(
                    f"Synthetic Mojo result type {policy.mojo_type!r} collides with "
                    f"ABI type {public_names[policy.mojo_type]!r}"
                )
            free_symbol = method_names.get((None, policy.mojo_type))
            if free_symbol is not None:
                raise GenerationError(
                    f"Synthetic Mojo result type {policy.mojo_type!r} collides with "
                    f"free function {free_symbol!r}"
                )
            synthetic_names[policy.mojo_type] = mapping.abi_symbol
        destroy_symbols = {
            mapping.destroy_abi_symbol
            for mapping in self.types
            if mapping.destroy_abi_symbol is not None
        }
        overlap = sorted(destroy_symbols & set(report_by_symbol))
        if overlap:
            raise GenerationError(
                "Opaque destructor symbols collide with callable ABI symbols: "
                + ", ".join(overlap)
            )
        self._validate_export_ledger(mapped, mapped_symbols)
        self._validate_value_types(definitions, mapped)
        self._validate_function_shapes(definitions, report_by_symbol, mapped)

    def _validate_export_ledger(
        self,
        types: dict[str, TypeMapping],
        functions: dict[str, FunctionMapping],
    ) -> None:
        """Tie public mappings to the upstream API audit without conflating symbols."""
        by_id: dict[str, ExportRecord] = {}
        for export in self.exports:
            if export.id in by_id:
                raise GenerationError(
                    f"Export id {export.id!r} is declared more than once"
                )
            by_id[export.id] = export

        used: dict[str, list[str]] = {}
        surfaces: dict[str, set[str]] = {}
        mapped_surfaces: dict[str, list[tuple[str, str | None, str | None]]] = {}
        iterator_factories: dict[str, set[str]] = {}
        owner_names = {
            mapping.abi_name: mapping.mojo_name
            for mapping in types.values()
            if mapping.kind != "skip" and mapping.mojo_name is not None
        }
        for mapping in types.values():
            if mapping.kind == "skip":
                continue
            assert mapping.export_id is not None
            used.setdefault(mapping.export_id, []).append(
                f"ABI type {mapping.abi_name}"
            )
            assert mapping.mojo_name is not None
            surfaces.setdefault(mapping.export_id, set()).add(mapping.mojo_name)
            mapped_surfaces.setdefault(mapping.export_id, []).append(
                (
                    f"ABI type {mapping.abi_name}",
                    mapping.mojo_name,
                    mapping.abi_name if mapping.kind == "iterator" else None,
                )
            )
        for symbol, mapping in functions.items():
            if mapping.kind == "skip":
                continue
            assert mapping.export_id is not None
            used.setdefault(mapping.export_id, []).append(f"ABI callable {symbol}")
            assert mapping.mojo_name is not None
            surface: str | None
            if mapping.kind == "free":
                surface = mapping.mojo_name
            else:
                owner_name = owner_names.get(mapping.abi_owner or "")
                if mapping.kind == "constructor":
                    surface = owner_name
                elif owner_name is None:
                    surface = None
                else:
                    surface = f"{owner_name}.{mapping.mojo_name}"
            if surface is not None:
                surfaces.setdefault(mapping.export_id, set()).add(surface)
            mapped_surfaces.setdefault(mapping.export_id, []).append(
                (
                    f"ABI callable {symbol}",
                    surface,
                    mapping.abi_owner if mapping.kind == "iterator_next" else None,
                )
            )
            output = self.report_function(symbol).get("output")
            if isinstance(output, dict):
                output_kind, output_details = _kind(output, f"{symbol}.output")
                if (
                    output_kind == "opaque_pointer"
                    and isinstance(output_details, dict)
                    and output_details.get("owned") is True
                ):
                    iterator_name = output_details.get("name")
                    iterator_mapping = (
                        types.get(iterator_name)
                        if isinstance(iterator_name, str)
                        else None
                    )
                    if (
                        iterator_mapping is not None
                        and iterator_mapping.kind == "iterator"
                    ):
                        iterator_factories.setdefault(mapping.export_id, set()).add(
                            iterator_mapping.abi_name
                        )

        unknown = sorted(set(used) - set(by_id))
        if unknown:
            raise GenerationError(
                "Public Mojo mappings reference unknown export ids: " + ", ".join(unknown)
            )
        for export_id, consumers in used.items():
            export = by_id[export_id]
            if export.status == "SKIPPED":
                raise GenerationError(
                    f"SKIPPED export {export_id!r} has public Mojo mappings: "
                    + ", ".join(consumers)
                )
        unused = sorted(
            export.id
            for export in self.exports
            if export.status != "SKIPPED" and export.id not in used
        )
        if unused:
            raise GenerationError(
                "Every non-SKIPPED export requires at least one public Mojo mapping: "
                + ", ".join(unused)
            )
        for export in self.exports:
            if export.status == "SKIPPED":
                continue
            assert export.mojo is not None
            rendered = surfaces.get(export.id, set())
            for description, surface, support_iterator in mapped_surfaces.get(
                export.id, []
            ):
                if surface == export.mojo:
                    continue
                if (
                    support_iterator is not None
                    and export.status == "ADAPTED"
                    and support_iterator in iterator_factories.get(export.id, set())
                ):
                    continue
                raise GenerationError(
                    f"{description} renders public Mojo surface {surface or '<none>'!r}, "
                    f"which does not match linked export {export.id!r} surface "
                    f"{export.mojo!r}"
                )
            if export.mojo not in rendered:
                available = ", ".join(sorted(rendered)) if rendered else "<none>"
                raise GenerationError(
                    f"Export {export.id!r} declares Mojo surface {export.mojo!r}, "
                    f"but its linked public mappings render: {available}"
                )

    def _validate_value_types(
        self,
        definitions: dict[str, tuple[str, dict[str, Any]]],
        mappings: dict[str, TypeMapping],
    ) -> None:
        for name, mapping in mappings.items():
            if mapping.kind != "value":
                continue
            _, definition = definitions[name]
            fields = definition.get("fields")
            if not isinstance(fields, list) or not fields or not all(
                isinstance(field, dict) for field in fields
            ):
                raise GenerationError(f"Value struct {name!r} must have fields")
            rendered_names: set[str] = set()
            for index, field in enumerate(fields):
                field_name = field.get("name")
                if not isinstance(field_name, str) or not field_name:
                    raise GenerationError(f"{name}.fields[{index}] has an invalid name")
                rendered = _mojo_abi_ident(field_name)
                if rendered in rendered_names:
                    raise GenerationError(
                        f"Value struct {name!r} has colliding Mojo field {rendered!r}"
                    )
                rendered_names.add(rendered)
                ty = field.get("ty")
                if not isinstance(ty, dict):
                    raise GenerationError(f"{name}.{field_name} has no ABI type")
                kind, details = _kind(ty, f"{name}.{field_name}")
                if kind == "primitive":
                    _raw_type(ty, f"{name}.{field_name}")
                elif kind in {"struct", "enum"}:
                    nested = mappings.get(details)
                    expected = "value" if kind == "struct" else "enum"
                    if nested is None or nested.kind != expected:
                        raise GenerationError(
                            f"Public value field {name}.{field_name} exposes unmapped "
                            f"{kind} {details!r}"
                        )
                else:
                    raise GenerationError(
                        f"Public value field {name}.{field_name} has unsupported {kind} type"
                    )

    def _validate_function_shapes(
        self,
        definitions: dict[str, tuple[str, dict[str, Any]]],
        functions: dict[str, dict[str, Any]],
        mappings: dict[str, TypeMapping],
    ) -> None:
        iterator_next: dict[str, FunctionMapping] = {}
        for mapping in self.functions:
            function = functions[mapping.abi_symbol]
            params = function.get("params")
            output = function.get("output")
            if not isinstance(params, list) or not all(isinstance(item, dict) for item in params):
                raise GenerationError(f"{mapping.abi_symbol} params must be an array")
            if not isinstance(output, dict):
                raise GenerationError(f"{mapping.abi_symbol} output must be an ABI type")
            if mapping.kind == "skip":
                continue
            self._validate_function_adapters(
                mapping, function, definitions, mappings
            )
            owner_mapping = mappings.get(mapping.abi_owner) if mapping.abi_owner else None
            if mapping.kind == "free":
                if mapping.abi_owner is not None:
                    raise GenerationError(f"Free function {mapping.abi_symbol} must not have an owner")
            else:
                if owner_mapping is None or owner_mapping.kind not in {
                    "value",
                    "opaque",
                    "iterator",
                }:
                    raise GenerationError(
                        f"{mapping.abi_symbol} owner must be an exported opaque/iterator"
                    )
            if mapping.kind in {"constructor", "named_constructor"}:
                if owner_mapping is None or owner_mapping.kind not in {
                    "opaque",
                    "iterator",
                }:
                    raise GenerationError(
                        f"Constructor {mapping.abi_symbol} owner must be opaque or iterator"
                    )
                if not self._is_owned_opaque(output, mapping.abi_owner):
                    raise GenerationError(
                        f"Constructor {mapping.abi_symbol} must return its owned opaque owner"
                    )
                if any(self._is_self_param(param, mapping.abi_owner) for param in params):
                    raise GenerationError(f"Constructor {mapping.abi_symbol} must not take self")
            elif mapping.kind == "static":
                if any(self._is_self_param(param, mapping.abi_owner) for param in params):
                    raise GenerationError(
                        f"Static function {mapping.abi_symbol} must not take self"
                    )
            elif mapping.kind in {"method", "iterator_next"}:
                if not params or not self._is_self_param(params[0], mapping.abi_owner):
                    raise GenerationError(
                        f"{mapping.kind} {mapping.abi_symbol} must have owner self as first parameter"
                    )
                if any(self._is_self_param(param, mapping.abi_owner) for param in params[1:]):
                    raise GenerationError(f"{mapping.abi_symbol} contains a second self parameter")
            if mapping.kind == "iterator_next":
                if owner_mapping is None or owner_mapping.kind != "iterator":
                    raise GenerationError(
                        f"iterator_next {mapping.abi_symbol} owner is not kind=iterator"
                    )
                if mapping.abi_owner in iterator_next:
                    raise GenerationError(
                        f"Iterator {mapping.abi_owner} has more than one iterator_next mapping"
                    )
                iterator_next[mapping.abi_owner] = mapping
                self._validate_status_out_iterator(
                    owner_mapping, mapping, params, output, definitions, mappings
                )
            hidden_params = {
                raw_name
                for adapter in mapping.scalarized_params
                for raw_name in (adapter.data_param, adapter.len_param)
            }
            if mapping.out_param is not None:
                hidden_params.add(mapping.out_param)
            public_params = params
            if mapping.kind == "iterator_next":
                public_params = params[:1]
            for index, param in enumerate(public_params):
                if index == 0 and self._is_self_param(param, mapping.abi_owner):
                    continue
                if param.get("name") in hidden_params:
                    continue
                self._validate_public_argument(param.get("ty"), f"{mapping.abi_symbol}.params[{index}]")
            if mapping.kind != "iterator_next" and mapping.result is None:
                self._validate_public_output(output, mapping)
        for mapping in self.types:
            if mapping.kind == "iterator" and mapping.abi_name not in iterator_next:
                raise GenerationError(
                    f"Iterator {mapping.abi_name} requires exactly one iterator_next function"
                )

    def _validate_function_adapters(
        self,
        mapping: FunctionMapping,
        function: dict[str, Any],
        definitions: dict[str, tuple[str, dict[str, Any]]],
        mappings: dict[str, TypeMapping],
    ) -> None:
        params = function["params"]
        names = [param.get("name") for param in params]
        if not all(isinstance(name, str) and name for name in names):
            raise GenerationError(f"{mapping.abi_symbol} has an invalid parameter name")
        if len(names) != len(set(names)):
            raise GenerationError(f"{mapping.abi_symbol} repeats a parameter name")
        by_name = {str(param["name"]): param for param in params}
        scalarized_raw: set[str] = set()
        for adapter in mapping.scalarized_params:
            for raw_name in (adapter.data_param, adapter.len_param):
                param = by_name.get(raw_name)
                if param is None:
                    raise GenerationError(
                        f"{mapping.abi_symbol} scalarized adapter names unknown ABI "
                        f"parameter {raw_name!r}"
                    )
                if param.get("ty") != {"kind": "primitive", "details": "u_int"}:
                    raise GenerationError(
                        f"{mapping.abi_symbol}.{raw_name} must be primitive u_int for "
                        "a scalarized pointer/length adapter"
                    )
                scalarized_raw.add(raw_name)
            if adapter.kind == "copied-value-slice":
                target = mappings.get(adapter.element)
                if target is None or target.kind != "value":
                    raise GenerationError(
                        f"{mapping.abi_symbol} copied-value-slice element "
                        f"{adapter.element!r} is not a public value ABI type"
                    )

        ordinary_public = {
            _mojo_abi_ident(str(name))
            for name in names
            if name not in scalarized_raw
            and name != mapping.out_param
            and name != "self"
        }
        adapter_public = {adapter.name for adapter in mapping.scalarized_params}
        overlap = sorted(ordinary_public & adapter_public)
        if overlap:
            raise GenerationError(
                f"{mapping.abi_symbol} scalarized parameter names collide with ordinary "
                f"public parameters: {', '.join(overlap)}"
            )

        if mapping.out_param is not None:
            if mapping.out_param in scalarized_raw:
                raise GenerationError(
                    f"{mapping.abi_symbol}.{mapping.out_param} cannot be both out_param "
                    "and scalarized input"
                )
            out = by_name.get(mapping.out_param)
            if out is None:
                raise GenerationError(
                    f"{mapping.abi_symbol} out_param {mapping.out_param!r} does not exist"
                )
            out_ty = out.get("ty")
            if not isinstance(out_ty, dict):
                raise GenerationError(f"{mapping.abi_symbol} out_param has no ABI type")
            out_kind, out_details = _kind(out_ty, f"{mapping.abi_symbol}.out_param")
            if (
                out_kind != "struct_pointer"
                or not isinstance(out_details, dict)
                or out_details.get("mutable") is not True
                or definitions.get(out_details.get("name"), (None,))[0] != "value"
            ):
                raise GenerationError(
                    f"{mapping.abi_symbol}.out_param must be a mutable value struct pointer"
                )
            if mapping.result is None:
                raise GenerationError(
                    f"{mapping.abi_symbol}.out_param requires a scalar result status policy"
                )
        if mapping.result is not None:
            self._validate_result_policy(
                mapping, function["output"], definitions, mappings
            )
        if mapping.optional_none_error is not None:
            output_kind, output_details = _kind(
                function["output"], f"{mapping.abi_symbol}.output"
            )
            if (
                output_kind != "opaque_pointer"
                or not isinstance(output_details, dict)
                or output_details.get("owned") is not True
                or output_details.get("optional") is not True
            ):
                raise GenerationError(
                    f"{mapping.abi_symbol}.optional_none_error requires an optional "
                    "owned opaque return"
                )

    def _validate_result_policy(
        self,
        mapping: FunctionMapping,
        output: dict[str, Any],
        definitions: dict[str, tuple[str, dict[str, Any]]],
        mappings: dict[str, TypeMapping],
    ) -> None:
        assert mapping.result is not None
        policy = mapping.result
        if policy.status_field is not None:
            raise GenerationError(
                f"{mapping.abi_symbol} result.status_field would require an aggregate "
                "return by value; Mojo 1.0 wrapper schema requires a direct scalar "
                "status plus function out_param"
            )
        status_ty = output
        if policy.out_value:
            if mapping.out_param is None:
                raise GenerationError(
                    f"{mapping.abi_symbol} result.out_value requires function out_param"
                )
            out = next(
                param
                for param in self.report_function(mapping.abi_symbol)["params"]
                if param.get("name") == mapping.out_param
            )
            out_name = out["ty"]["details"]["name"]
            target = mappings.get(out_name)
            if target is None or target.kind != "value":
                raise GenerationError(
                    f"{mapping.abi_symbol} result.out_value target {out_name!r} "
                    "must be a public value type"
                )
        elif policy.value_fields:
            if mapping.out_param is None:
                raise GenerationError(
                    f"{mapping.abi_symbol} direct status result can extract fields only "
                    "from a declared out_param"
                )
            out = next(
                param
                for param in self.report_function(mapping.abi_symbol)["params"]
                if param.get("name") == mapping.out_param
            )
            out_name = out["ty"]["details"]["name"]
            definition = definitions[out_name][1]
            fields = definition.get("fields")
            if not isinstance(fields, list):
                raise GenerationError(f"{mapping.abi_symbol} out struct has invalid fields")
            by_name = {field.get("name"): field for field in fields}
            for field_name in policy.value_fields:
                field = by_name.get(field_name)
                if field is None:
                    raise GenerationError(
                        f"{mapping.abi_symbol} out struct lacks field {field_name!r}"
                    )
                field_ty = field.get("ty")
                if not isinstance(field_ty, dict):
                    raise GenerationError(
                        f"{mapping.abi_symbol} out field {field_name!r} has no ABI type"
                    )
                field_kind, field_details = _kind(
                    field_ty, f"{mapping.abi_symbol}.{field_name}"
                )
                if field_kind not in {"primitive", "struct", "enum"}:
                    raise GenerationError(
                        f"{mapping.abi_symbol} out field {field_name!r} is unsupported"
                    )
                if field_kind != "primitive":
                    target = mappings.get(field_details)
                    expected = "value" if field_kind == "struct" else "enum"
                    if target is None or target.kind != expected:
                        raise GenerationError(
                            f"{mapping.abi_symbol} out field type {field_details!r} is not exported"
                        )
        elif mapping.out_param is not None:
            raise GenerationError(
                f"{mapping.abi_symbol}.out_param requires at least one result.value_fields entry"
            )
        status_kind, status_details = _kind(status_ty, f"{mapping.abi_symbol}.status")
        if status_kind == "enum":
            definition = definitions.get(status_details)
            if definition is None or definition[0] != "enum":
                raise GenerationError(f"{mapping.abi_symbol} status enum is not defined")
            actual = {variant.get("discriminant") for variant in definition[1]["variants"]}
            declared = {policy.ok_discriminant, *(code for code, _ in policy.errors)}
            if actual != declared:
                raise GenerationError(
                    f"{mapping.abi_symbol} result policy does not cover status enum: "
                    f"declared {sorted(declared)}, report has {sorted(actual)}"
                )
        elif status_kind != "primitive" or status_details not in {
            "int8", "u_int8", "int16", "u_int16", "int32", "u_int32", "int64", "u_int64", "int", "u_int"
        }:
            raise GenerationError(f"{mapping.abi_symbol} result status is not integer/enum")

    @staticmethod
    def _is_self_param(param: dict[str, Any], owner: str | None) -> bool:
        if owner is None or param.get("name") != "self" or not isinstance(param.get("ty"), dict):
            return False
        kind, details = _kind(param["ty"], "self")
        return (
            kind in {"opaque_pointer", "struct_pointer"}
            and isinstance(details, dict)
            and details.get("name") == owner
        )

    @staticmethod
    def _is_owned_opaque(ty: dict[str, Any], owner: str | None) -> bool:
        kind, details = _kind(ty, "constructor output")
        return (
            kind == "opaque_pointer"
            and isinstance(details, dict)
            and details.get("name") == owner
            and details.get("owned") is True
        )

    def _validate_iterator_step(
        self,
        iterator: TypeMapping,
        step: dict[str, Any],
        definitions: dict[str, tuple[str, dict[str, Any]]],
        mappings: dict[str, TypeMapping],
    ) -> None:
        fields = step.get("fields")
        if not isinstance(fields, list) or not all(isinstance(field, dict) for field in fields):
            raise GenerationError(f"Iterator step for {iterator.abi_name} has invalid fields")
        by_name = {field.get("name"): field for field in fields}
        if len(by_name) != len(fields):
            raise GenerationError(f"Iterator step for {iterator.abi_name} repeats a field")
        status = by_name.get(iterator.status_field)
        item = by_name.get(iterator.item_field)
        if status is None or item is None:
            raise GenerationError(
                f"Iterator {iterator.abi_name} step lacks declared status/item fields"
            )
        status_kind, status_details = _kind(status.get("ty", {}), "iterator status")
        if status_kind == "enum":
            status_mapping = mappings.get(status_details)
            if status_mapping is None or status_mapping.kind not in {"enum", "skip"}:
                raise GenerationError("Iterator status enum is not mapped")
            self._validate_iterator_enum_discriminants(
                iterator, status_details, definitions
            )
        elif status_kind != "primitive" or status_details not in {
            "int8", "u_int8", "int16", "u_int16", "int32", "u_int32", "int64", "u_int64", "int", "u_int"
        }:
            raise GenerationError("Iterator status field must be an integer primitive or enum")
        item_kind, item_details = _kind(item.get("ty", {}), "iterator item")
        if item_kind not in {"primitive", "struct", "enum"}:
            raise GenerationError("Iterator item must be a primitive, value struct, or enum")
        if item_kind != "primitive":
            item_mapping = mappings.get(item_details)
            if item_mapping is None or item_mapping.kind not in {"value", "enum"}:
                raise GenerationError("Iterator item type is not exported")

    def _validate_status_out_iterator(
        self,
        iterator: TypeMapping,
        mapping: FunctionMapping,
        params: list[dict[str, Any]],
        output: dict[str, Any],
        definitions: dict[str, tuple[str, dict[str, Any]]],
        mappings: dict[str, TypeMapping],
    ) -> None:
        if len(params) != 2 or params[1].get("name") != iterator.out_param:
            raise GenerationError(
                f"Status-out iterator {mapping.abi_symbol} must take self plus declared "
                f"out_param {iterator.out_param!r}"
            )
        out_ty = params[1].get("ty")
        if not isinstance(out_ty, dict):
            raise GenerationError(f"{mapping.abi_symbol} out parameter has no ABI type")
        out_kind, out_details = _kind(out_ty, f"{mapping.abi_symbol}.out")
        if (
            out_kind != "struct_pointer"
            or not isinstance(out_details, dict)
            or out_details.get("mutable") is not True
            or not isinstance(out_details.get("name"), str)
        ):
            raise GenerationError(
                f"Status-out iterator {mapping.abi_symbol} out_param must be a mutable value pointer"
            )
        item_name = out_details["name"]
        if definitions.get(item_name, (None,))[0] != "value":
            raise GenerationError(f"Iterator item {item_name!r} is not a value struct")
        item_mapping = mappings.get(item_name)
        if item_mapping is None or item_mapping.kind != "value":
            raise GenerationError(f"Iterator item {item_name!r} is not exported")
        status_kind, status_details = _kind(output, f"{mapping.abi_symbol}.output")
        if status_kind == "enum":
            status_mapping = mappings.get(status_details)
            if status_mapping is None or status_mapping.kind not in {"enum", "skip"}:
                raise GenerationError("Iterator status enum is not mapped")
            self._validate_iterator_enum_discriminants(
                iterator, status_details, definitions
            )
        elif status_kind != "primitive" or status_details not in {
            "int8", "u_int8", "int16", "u_int16", "int32", "u_int32", "int64", "u_int64", "int", "u_int"
        }:
            raise GenerationError("Status-out iterator must return an integer primitive or enum")

    @staticmethod
    def _validate_iterator_enum_discriminants(
        iterator: TypeMapping,
        enum_name: str,
        definitions: dict[str, tuple[str, dict[str, Any]]],
    ) -> None:
        definition = definitions.get(enum_name)
        if definition is None or definition[0] != "enum":
            raise GenerationError(f"Iterator status enum {enum_name!r} is not defined")
        variants = definition[1].get("variants")
        if not isinstance(variants, list):
            raise GenerationError(f"Iterator status enum {enum_name!r} has invalid variants")
        actual = {variant.get("discriminant") for variant in variants}
        declared = {
            iterator.item_discriminant,
            iterator.finished_discriminant,
            *(code for code, _ in iterator.error_discriminants),
        }
        if actual != declared:
            raise GenerationError(
                f"Iterator {iterator.abi_name} status policy does not cover enum "
                f"{enum_name}: declared {sorted(declared)}, report has {sorted(actual)}"
            )

    def _validate_public_argument(self, ty: Any, label: str) -> None:
        if not isinstance(ty, dict):
            raise GenerationError(f"{label} must be an ABI type")
        kind, details = _kind(ty, label)
        if kind in {"primitive", "enum", "struct_pointer"}:
            _raw_type(ty, label)
            return
        if kind == "struct":
            raise GenerationError(
                f"{label}: value structs cannot be passed by value through Mojo 1.0 "
                "dynamic FFI; project the argument as a struct pointer"
            )
        if kind == "opaque_pointer" and isinstance(details, dict):
            if details.get("optional") or details.get("owned"):
                raise GenerationError(
                    f"{label}: optional or ownership-consuming opaque arguments are unsupported"
                )
            _raw_type(ty, label)
            return
        if kind == "slice" and isinstance(details, dict):
            raise GenerationError(
                f"{label}: raw slice carriers cannot be passed by value through Mojo "
                "1.0 dynamic FFI; use scalarized_params with u_int address/length"
            )
        raise GenerationError(f"{label}: unsupported public argument shape {kind!r}")

    def _validate_public_output(self, ty: dict[str, Any], mapping: FunctionMapping) -> None:
        kind, details = _kind(ty, f"{mapping.abi_symbol}.output")
        if kind in {"unit", "primitive", "enum"}:
            _raw_type(ty, f"{mapping.abi_symbol}.output")
            return
        if kind == "struct":
            raise GenerationError(
                f"{mapping.abi_symbol}: value structs cannot be returned by value through "
                "Mojo 1.0 dynamic FFI; return scalar status and declare out_param"
            )
        if kind == "opaque_pointer" and isinstance(details, dict):
            if details.get("owned") is not True:
                raise GenerationError(
                    f"{mapping.abi_symbol}: borrowed opaque returns need a lifetime policy"
                )
            target = next(
                (item for item in self.types if item.abi_name == details.get("name")),
                None,
            )
            if target is None or target.kind not in {"opaque", "iterator"}:
                raise GenerationError(
                    f"{mapping.abi_symbol}: owned opaque return target is not exported"
                )
            return
        if kind == "slice" and isinstance(details, dict):
            element = details.get("element")
            if details.get("ownership") == "owned":
                element_description = "u8" if element == {
                    "kind": "primitive",
                    "details": "u_int8",
                } else "slice"
                raise GenerationError(
                    f"{mapping.abi_symbol}: owned {element_description} returns are not "
                    "supported by wrapper schema v1; project the value as an opaque "
                    "owner or a lazy iterator"
                )
            raise GenerationError(
                f"{mapping.abi_symbol}: borrowed slice returns require an explicit "
                "owner/origin policy and are unsupported by wrapper schema v1"
            )
        raise GenerationError(
            f"{mapping.abi_symbol}: unsupported public return shape {kind!r}"
        )

    def report_function(self, symbol: str) -> dict[str, Any]:
        for function in self.report_functions:
            if function.get("abi_name") == symbol:
                return function
        raise AssertionError(symbol)

    def report_type(self, abi_name: str) -> tuple[str, dict[str, Any]]:
        for kind, values in (
            ("value", self.report_structs),
            ("opaque", self.report_opaques),
            ("enum", self.report_enums),
        ):
            for value in values:
                if value.get("name") == abi_name:
                    return kind, value
        raise AssertionError(abi_name)


def _header(model: Model) -> str:
    return (
        "# GENERATED FILE — DO NOT EDIT DIRECTLY\n"
        f"# Source crate: {model.crate_name} {model.crate_version}\n"
        "# Binding manifest: binding.toml\n"
        f"# Generator: {GENERATOR_NAME} {GENERATOR_VERSION} "
        f"(projection {model.generator_version})\n"
        f"# Diplomat: {model.diplomat_version}\n"
        f"# Diplomat core: {model.diplomat_core_version}\n"
        f"# Diplomat runtime: {model.diplomat_runtime_version}\n"
        f"# Mojo compiler: {model.mojo_version}\n"
    )


def _finalize_ffi_header(model: Model, content: str) -> str:
    match = _RAW_BACKEND_HEADER.match(content)
    if match is None:
        match = _FINAL_BACKEND_HEADER.match(content)
    if match is None:
        raise GenerationError(
            "_ffi.mojo has an unrecognized backend header; refusing to rewrite its ABI body"
        )
    backend_version, core_version, raw_model_sha256 = match.groups()
    if backend_version != model.abi_backend_version:
        raise GenerationError(
            f"_ffi.mojo backend {backend_version!r} does not match binding.toml "
            f"{model.abi_backend_version!r}"
        )
    if core_version != model.diplomat_core_version:
        raise GenerationError(
            f"_ffi.mojo Diplomat core {core_version!r} does not match binding.toml "
            f"{model.diplomat_core_version!r}"
        )
    if raw_model_sha256 != model.abi_model_sha256:
        raise GenerationError(
            "_ffi.mojo ABI model digest does not match abi-report.json; rerun "
            "diplomat-gen-mojo and keep its raw Mojo/report outputs together"
        )
    prefix = _header(model) + (
        f"# ABI backend: diplomat-gen-mojo {backend_version}\n"
        f"# ABI backend Diplomat core: {core_version}\n"
        f"# ABI model SHA-256: {raw_model_sha256}\n\n"
    )
    body = content[match.end() :]
    actual_body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if actual_body_sha256 != model.mojo_body_sha256:
        raise GenerationError(
            "_ffi.mojo ABI body does not match abi-report.json; rerun "
            "diplomat-gen-mojo and do not edit the raw ABI layer"
        )
    _validate_ffi_aliases(model, body)
    return prefix + body


def _validate_ffi_aliases(model: Model, body: str) -> None:
    expected: list[tuple[str, str, str]] = []
    for opaque in model.report_opaques:
        alias = opaque.get("destructor_mojo_abi_alias")
        symbol = opaque.get("destructor_abi_name")
        name = opaque.get("name")
        if (
            isinstance(alias, str)
            and isinstance(symbol, str)
            and isinstance(name, str)
        ):
            declaration = (
                f"comptime {alias} = def({_mojo_abi_ident(name)}Handle) "
                'thin abi("C") -> None'
            )
            expected.append((alias, symbol, declaration))
    for function in model.report_functions:
        alias = function.get("mojo_abi_alias")
        symbol = function.get("abi_name")
        if isinstance(alias, str) and isinstance(symbol, str):
            params = function.get("params")
            output = function.get("output")
            if not isinstance(params, list) or not all(
                isinstance(param, dict) and isinstance(param.get("ty"), dict)
                for param in params
            ):
                raise GenerationError(
                    f"ABI report function {symbol!r} has malformed parameters"
                )
            if not isinstance(output, dict):
                raise GenerationError(
                    f"ABI report function {symbol!r} has a malformed output"
                )
            rendered_params = ", ".join(
                _ffi_declaration_type(
                    param["ty"], f"ABI function {symbol}.parameter"
                )
                for param in params
            )
            declaration = (
                f"comptime {alias} = def({rendered_params}) thin abi(\"C\") -> "
                f"{_ffi_declaration_type(output, f'ABI function {symbol}.output')}"
            )
            expected.append((alias, symbol, declaration))
    for alias, symbol, declaration in expected:
        block = (
            declaration
            + "\n# symbol "
            + alias
            + " = "
            + json.dumps(symbol)
        )
        if body.count(block) != 1:
            raise GenerationError(
                f"_ffi.mojo does not contain exactly one report-declared C ABI "
                f"signature for alias {alias!r} and symbol {symbol!r}"
            )


def _render_runtime(model: Model) -> str:
    environment = "RUST_MOJO_" + re.sub(r"[^A-Za-z0-9]", "_", model.binding_id).upper() + "_LIBRARY"
    return _header(model) + f'''\nfrom std.ffi import OwnedDLHandle, external_call
from std.os import getenv
from std.os.path import dirname, join, realpath
from std.sys import CompilationTarget, argv


def _library_filename() -> String:
    comptime if CompilationTarget.is_linux():
        return "lib{model.ffi_crate}.so"
    elif CompilationTarget.is_macos():
        return "lib{model.ffi_crate}.dylib"
    else:
        return ""


def _macos_executable_path() raises -> String:
    comptime if CompilationTarget.is_macos():
        # _NSGetExecutablePath reports the required NUL-terminated buffer size
        # when the supplied buffer is too small. realpath then resolves any
        # symlink or relative components returned by dyld.
        var size = UInt32(1)
        var buffer = String(unsafe_uninit_length=1)
        var status = external_call["_NSGetExecutablePath", Int32](
            buffer.unsafe_as_bytes_mut().unsafe_ptr(), Pointer(to=size)
        )
        if status == 0:
            return realpath(
                String(unsafe_from_utf8_ptr=buffer.as_bytes().unsafe_ptr())
            )
        if size <= 1:
            raise Error("_NSGetExecutablePath did not report a buffer size")
        buffer = String(unsafe_uninit_length=Int(size))
        status = external_call["_NSGetExecutablePath", Int32](
            buffer.unsafe_as_bytes_mut().unsafe_ptr(), Pointer(to=size)
        )
        if status != 0:
            raise Error("_NSGetExecutablePath failed")
        return realpath(
            String(unsafe_from_utf8_ptr=buffer.as_bytes().unsafe_ptr())
        )
    else:
        raise Error("_NSGetExecutablePath is available only on macOS")


def _argv_executable_path() raises -> String:
    var arguments = argv()
    if len(arguments) == 0 or arguments[0] == "":
        raise Error("process executable path is unavailable")
    var invoked = String(arguments[0])
    if invoked.rfind("/") >= 0:
        return realpath(invoked)
    var search_path = getenv("PATH")
    for component in search_path.split(":"):
        var directory = String(component)
        if directory == "":
            directory = "."
        try:
            return realpath(join(directory, invoked))
        except:
            pass
    raise Error("cannot resolve executable from argv[0] and PATH")


def _executable_path() raises -> String:
    comptime if CompilationTarget.is_linux():
        try:
            return realpath("/proc/self/exe")
        except:
            pass
    elif CompilationTarget.is_macos():
        try:
            return _macos_executable_path()
        except:
            pass
    return _argv_executable_path()


def _open_library() raises -> OwnedDLHandle:
    comptime if not (CompilationTarget.is_linux() or CompilationTarget.is_macos()):
        raise Error("{model.package} supports Linux and macOS in binding schema v1")
    var filename = _library_filename()
    var override = getenv("{environment}")
    if override != "":
        return OwnedDLHandle(override)
    try:
        var executable_relative = join(
            dirname(_executable_path()), "..", "lib", filename
        )
        return OwnedDLHandle(executable_relative)
    except:
        pass
    var prefix = getenv("CONDA_PREFIX")
    if prefix != "":
        try:
            return OwnedDLHandle(join(prefix, "lib", filename))
        except:
            pass
    return OwnedDLHandle(filename)
'''


def _render_types(model: Model) -> str:
    lines = [
        _header(model),
        "from std.collections import ImmSpan, MutSpan, Span",
        "from std.ffi import OwnedDLHandle",
        f"import {model.package}._ffi as _ffi",
        f"import {model.package}._runtime as _runtime",
        "",
    ]
    # Enums are ABI integer aliases and may be referenced by value fields.
    for mapping in sorted(model.types, key=lambda item: item.mojo_name or item.abi_name):
        if mapping.kind != "enum":
            continue
        raw_name = _mojo_abi_ident(mapping.abi_name)
        lines.append(f"comptime {mapping.mojo_name} = _ffi.{raw_name}")
        _, enumeration = model.report_type(mapping.abi_name)
        for variant in enumeration.get("variants", []):
            variant_name = variant.get("name")
            if not isinstance(variant_name, str):
                raise GenerationError(f"Enum {mapping.abi_name} has invalid variant")
            rendered = _mojo_abi_ident(variant_name)
            lines.append(
                f"comptime {mapping.mojo_name}_{rendered}: {mapping.mojo_name} = "
                f"_ffi.{raw_name}_{rendered}"
            )
        lines.extend(
            [
                "",
                f"def _validate_{mapping.mojo_name}(value: {mapping.mojo_name}) raises:",
            ]
        )
        for variant in enumeration["variants"]:
            rendered = _mojo_abi_ident(variant["name"])
            lines.extend(
                [
                    f"    if value == {mapping.mojo_name}_{rendered}:",
                    "        return",
                ]
            )
        lines.append(
            f'    raise Error("invalid {mapping.mojo_name} discriminant")'
        )
        lines.append("")

    for mapping in sorted(model.types, key=lambda item: item.mojo_name or item.abi_name):
        if mapping.kind != "value":
            continue
        _, definition = model.report_type(mapping.abi_name)
        fields = definition["fields"]
        raw_name = _mojo_abi_ident(mapping.abi_name)
        lines.extend(
            [
                "@fieldwise_init",
                f"struct {mapping.mojo_name}(Copyable, Movable):",
            ]
        )
        for field in fields:
            field_name = _mojo_abi_ident(field["name"])
            lines.append(
                f"    var {field_name}: "
                f"{_local_public_type(model, field['ty'], mapping.abi_name + '.' + field['name'])}"
            )
        lines.extend(
            [
                "",
                f"    def __init__(out self, *, _from_ffi: _ffi.{raw_name}):",
            ]
        )
        for field in fields:
            field_name = _mojo_abi_ident(field["name"])
            lines.append(
                f"        self.{field_name} = "
                + _from_ffi_expr(
                    model,
                    field["ty"],
                    f"_from_ffi.{field_name}",
                    local_types=True,
                )
            )
        arguments = ", ".join(
            f"{_mojo_abi_ident(field['name'])}="
            + _to_ffi_expr(
                model, field["ty"], f"self.{_mojo_abi_ident(field['name'])}"
            )
            for field in fields
        )
        lines.extend(["", f"    def _to_ffi(self) raises -> _ffi.{raw_name}:"])
        for field in fields:
            field_kind, field_details = _kind(
                field["ty"], f"{mapping.abi_name}.{field['name']}"
            )
            if field_kind == "enum":
                enum_mapping = model.type_by_abi[field_details]
                lines.append(
                    f"        _validate_{enum_mapping.mojo_name}("
                    f"self.{_mojo_abi_ident(field['name'])})"
                )
        lines.append(f"        return _ffi.{raw_name}({arguments})")
        methods = [
            function
            for function in model.functions
            if function.abi_owner == mapping.abi_name
            and function.kind in {"method", "static"}
        ]
        for method in methods:
            lines.append("")
            lines.extend(
                _render_callable(
                    model,
                    method,
                    model.report_function(method.abi_symbol),
                    indent="    ",
                    local_types=True,
                )
            )
        lines.append("")

    synthetic: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for function in model.functions:
        policy = function.result
        if policy is None or policy.mojo_type is None:
            continue
        output = model.report_function(function.abi_symbol)["output"]
        public_names = dict(policy.value_names)
        synthetic[policy.mojo_type] = [
            (
                public_names[field_name],
                _result_field(model, output, field_name, function.abi_symbol)["ty"],
            )
            for field_name in policy.value_fields
        ]
    for type_name, fields in sorted(synthetic.items()):
        lines.extend(["@fieldwise_init", f"struct {type_name}(Copyable, Movable):"])
        for field_name, field_ty in fields:
            lines.append(
                f"    var {_mojo_abi_ident(field_name)}: "
                f"{_local_public_type(model, field_ty, type_name + '.' + field_name)}"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _public_type(model: Model, ty: dict[str, Any], label: str) -> str:
    kind, details = _kind(ty, label)
    if kind == "unit":
        return "None"
    if kind == "primitive":
        return _raw_type(ty, label)
    if kind in {"struct", "enum"}:
        mapping = model.type_by_abi.get(details)
        if mapping is None or mapping.kind not in {"value", "enum"}:
            raise GenerationError(f"{label}: type {details!r} is not publicly mapped")
        return f"_types.{mapping.mojo_name}"
    if kind == "struct_pointer":
        if not isinstance(details, dict):
            raise GenerationError(f"{label}: invalid struct pointer")
        mapping = model.type_by_abi.get(details.get("name"))
        if mapping is None or mapping.kind != "value":
            raise GenerationError(f"{label}: struct pointer target is not publicly mapped")
        return f"_types.{mapping.mojo_name}"
    if kind == "opaque_pointer":
        if not isinstance(details, dict):
            raise GenerationError(f"{label}: invalid opaque pointer")
        mapping = model.type_by_abi.get(details.get("name"))
        if mapping is None or mapping.kind not in {"opaque", "iterator"}:
            raise GenerationError(f"{label}: opaque target is not publicly mapped")
        return str(mapping.mojo_name)
    if kind == "slice":
        if not isinstance(details, dict) or not isinstance(details.get("element"), dict):
            raise GenerationError(f"{label}: invalid slice")
        element = _public_type(model, details["element"], label + ".element")
        return f"MutSpan[{element}, _]" if details.get("mutable") else f"ImmSpan[{element}, _]"
    raise GenerationError(f"{label}: unsupported type {kind!r}")


def _local_public_type(model: Model, ty: dict[str, Any], label: str) -> str:
    rendered = _public_type(model, ty, label)
    return rendered.removeprefix("_types.")


def _from_ffi_expr(
    model: Model, ty: dict[str, Any], expression: str, *, local_types: bool = False
) -> str:
    kind, details = _kind(ty, "FFI conversion")
    if kind in {"primitive", "enum"}:
        return expression
    if kind == "struct":
        mapping = model.type_by_abi.get(details)
        if mapping is None or mapping.kind != "value":
            raise GenerationError(f"Cannot convert internal ABI struct {details!r} publicly")
        prefix = "" if local_types else "_types."
        return f"{prefix}{mapping.mojo_name}(_from_ffi={expression})"
    raise GenerationError(f"Cannot convert ABI {kind} value to a public value")


def _to_ffi_expr(
    model: Model, ty: dict[str, Any], expression: str
) -> str:
    kind, _ = _kind(ty, "FFI conversion")
    if kind in {"primitive", "enum"}:
        return expression
    if kind == "struct":
        return f"{expression}._to_ffi()"
    raise GenerationError(f"Cannot convert public {kind} value to ABI")


def _zero_ffi_expr(model: Model, ty: dict[str, Any], label: str) -> str:
    kind, details = _kind(ty, label)
    if kind == "primitive":
        return f"{_raw_type(ty, label)}()"
    if kind == "enum":
        if not isinstance(details, str):
            raise GenerationError(f"{label} has an invalid enum type")
        _, enumeration = model.report_type(details)
        variants = enumeration["variants"]
        discriminant = variants[0]["discriminant"]
        return f"{_raw_type(ty, label)}({discriminant})"
    if kind == "struct" and isinstance(details, str):
        _, definition = model.report_type(details)
        fields = definition.get("fields", [])
        arguments = ", ".join(
            f"{_mojo_abi_ident(str(field['name']))}="
            + _zero_ffi_expr(model, field["ty"], f"{label}.{field['name']}")
            for field in fields
        )
        return f"_ffi.{_mojo_abi_ident(details)}({arguments})"
    raise GenerationError(f"{label} cannot be safely zero-initialized")


def _argument_decl(
    model: Model,
    mapping: FunctionMapping,
    param: dict[str, Any],
    label: str,
    *,
    local_types: bool = False,
) -> str:
    name = _mojo_abi_ident(_string(param, "name", label + ".name"))
    ty = param.get("ty")
    if not isinstance(ty, dict):
        raise GenerationError(f"{label}.ty must be an ABI type")
    kind, details = _kind(ty, label + ".ty")
    convention = ""
    if kind == "opaque_pointer" and isinstance(details, dict) and details.get("mutable"):
        convention = "mut "
    elif kind == "struct_pointer" and isinstance(details, dict) and details.get("mutable"):
        convention = "mut "
    type_name = (
        _local_public_type(model, ty, label + ".ty")
        if local_types
        else _public_type(model, ty, label + ".ty")
    )
    return f"{convention}{name}: {type_name}"


def _prepare_call_argument(
    model: Model,
    mapping: FunctionMapping,
    param: dict[str, Any],
    label: str,
    *,
    local_types: bool = False,
) -> tuple[list[str], str, list[str]]:
    name = _mojo_abi_ident(_string(param, "name", label + ".name"))
    ty = param.get("ty")
    if not isinstance(ty, dict):
        raise GenerationError(f"{label}.ty must be an ABI type")
    kind, details = _kind(ty, label + ".ty")
    if kind == "primitive":
        return [], name, []
    if kind == "enum":
        enum_mapping = model.type_by_abi.get(details)
        if enum_mapping is None or enum_mapping.kind != "enum":
            raise GenerationError(f"{label}: enum input is not publicly mapped")
        prefix = "" if local_types else "_types."
        return [f"{prefix}_validate_{enum_mapping.mojo_name}({name})"], name, []
    if kind == "struct":
        local = f"_ffi_{name}"
        return [f"var {local} = {name}._to_ffi()"], local, []
    if kind == "struct_pointer":
        mutable = isinstance(details, dict) and details.get("mutable") is True
        if mutable:
            raise GenerationError(
                f"{label}: mutable public value references require an explicit projection"
            )
        origin = "MutUntrackedOrigin" if mutable else "ImmUntrackedOrigin"
        local = f"_ffi_{name}"
        lines = [f"var {local} = {name}._to_ffi()"]
        expression = (
            f"Pointer(to={local}).unsafe_mut_cast[False]()"
            f".unsafe_origin_cast[{origin}]()"
        )
        return lines, expression, [f"_ = Pointer(to={local})"]
    if kind == "opaque_pointer":
        mutable = isinstance(details, dict) and details.get("mutable") is True
        value = f"{name}._require_handle()"
        return [], value if mutable else value + ".unsafe_mut_cast[False]()", []
    if kind == "slice" and isinstance(details, dict):
        raise GenerationError(
            f"{label}: raw slice ABI arguments are forbidden; use scalarized_params"
        )
    raise GenerationError(f"{label}: unsupported call argument")


def _scalarized_argument_decl(
    model: Model, adapter: ScalarizedParam, *, local_types: bool
) -> str:
    if adapter.kind == "utf8-string":
        return f"{adapter.name}: String"
    if adapter.kind == "borrowed-primitive-slice":
        element = _PRIMITIVES[adapter.element]
    else:
        target = model.type_by_abi[adapter.element]
        element = ("" if local_types else "_types.") + str(target.mojo_name)
    return f"{adapter.name}: ImmSpan[{element}, _]"


def _prepare_scalarized_argument(
    model: Model, adapter: ScalarizedParam
) -> tuple[list[str], dict[str, str], list[str]]:
    name = adapter.name
    if adapter.kind == "utf8-string":
        storage = f"_bytes_{name}"
        lines = [f"var {storage} = {name}.as_bytes()"]
        keepalives = [f"_ = len({storage})"]
    elif adapter.kind == "copied-value-slice":
        target = model.type_by_abi[adapter.element]
        raw_type = f"_ffi.{_mojo_abi_ident(adapter.element)}"
        values = f"_ffi_{name}"
        storage = f"_ffi_{name}_span"
        lines = [
            f"var {values} = List[{raw_type}]()",
            f"for _value in {name}:",
            f"    {values}.append(_value._to_ffi())",
            f"var {storage} = Span({values})",
        ]
        if target.kind != "value":
            raise AssertionError(adapter.element)
        # The untracked integer address comes from the Span, while the List owns
        # the copied elements. Keep both locals observably live after the call.
        keepalives = [f"_ = len({storage})", f"_ = len({values})"]
    else:
        storage = name
        lines = []
        keepalives = [f"_ = len({storage})"]
    return (
        lines,
        {
            adapter.data_param: f"UInt(Int({storage}.unsafe_ptr()))",
            adapter.len_param: f"UInt(len({storage}))",
        },
        keepalives,
    )


def _call_output(
    model: Model,
    function: dict[str, Any],
    call: str,
    mapping: FunctionMapping,
    *,
    local_types: bool = False,
) -> list[str]:
    output = function["output"]
    kind, details = _kind(output, mapping.abi_symbol + ".output")
    if mapping.result is not None:
        policy = mapping.result
        lines = [f"var _result = {call}"]
        status = "_result"
        if policy.status_field is not None:
            status = f"_result.{_mojo_abi_ident(policy.status_field)}"
        for code, message in policy.errors:
            lines.extend(
                [
                    f"if Int({status}) == {code}:",
                    f"    raise Error({json.dumps(message)})",
                ]
            )
        lines.extend(
            [
                f"if Int({status}) != {policy.ok_discriminant}:",
                f"    raise Error({json.dumps('unexpected FFI result status')})",
            ]
        )
        if policy.out_value:
            out_ty = _out_value_type(model, mapping)
            lines.append(
                "return "
                + _from_ffi_expr(
                    model, out_ty, "_out", local_types=local_types
                )
            )
        elif not policy.value_fields:
            lines.append("return")
        elif len(policy.value_fields) == 1:
            field_name = policy.value_fields[0]
            field = _result_field(model, output, field_name, mapping.abi_symbol)
            lines.append(
                "return "
                + _from_ffi_expr(
                    model,
                    field["ty"],
                    (
                        f"_out.{_mojo_abi_ident(field_name)}"
                        if mapping.out_param is not None
                        else f"_result.{_mojo_abi_ident(field_name)}"
                    ),
                    local_types=local_types,
                )
            )
        else:
            assert policy.mojo_type is not None
            arguments = []
            public_names = dict(policy.value_names)
            for field_name in policy.value_fields:
                field = _result_field(model, output, field_name, mapping.abi_symbol)
                arguments.append(
                    f"{public_names[field_name]}="
                    + _from_ffi_expr(
                        model,
                        field["ty"],
                        (
                            f"_out.{_mojo_abi_ident(field_name)}"
                            if mapping.out_param is not None
                            else f"_result.{_mojo_abi_ident(field_name)}"
                        ),
                        local_types=local_types,
                    )
                )
            prefix = "" if local_types else "_types."
            lines.append(f"return {prefix}{policy.mojo_type}({', '.join(arguments)})")
        return lines
    if kind == "unit":
        return [call]
    if kind in {"primitive", "enum"}:
        return [f"return {call}"]
    if kind == "struct":
        return [
            f"return {_from_ffi_expr(model, output, call, local_types=local_types)}"
        ]
    if kind == "opaque_pointer" and isinstance(details, dict):
        target = model.type_by_abi[details["name"]]
        if details.get("optional"):
            none_error = mapping.optional_none_error
            if mapping.kind in {"constructor", "named_constructor"}:
                none_error = none_error or f"{target.mojo_name} construction failed"
            if none_error is not None:
                return [
                    f"var _result = {call}",
                    "if not _result:",
                    f"    raise Error({json.dumps(none_error)})",
                    f"return {target.mojo_name}(_from_abi=_result.unsafe_value())",
                ]
            return [
                f"var _result = {call}",
                "if not _result:",
                "    return None",
                f"return {target.mojo_name}(_from_abi=_result.unsafe_value())",
            ]
        return [f"return {target.mojo_name}(_from_abi={call})"]
    raise AssertionError(kind)


def _result_field(
    model: Model, output: dict[str, Any], field_name: str, symbol: str
) -> dict[str, Any]:
    mapping = model.function_by_symbol[symbol]
    source = output
    if mapping.out_param is not None:
        function = model.report_function(symbol)
        out = next(
            param for param in function["params"] if param.get("name") == mapping.out_param
        )
        out_details = out["ty"].get("details")
        if not isinstance(out_details, dict):
            raise GenerationError(f"{symbol} out_param has invalid details")
        source = {"kind": "struct", "details": out_details.get("name")}
    kind, details = _kind(source, symbol + ".result value source")
    if kind != "struct" or not isinstance(details, str):
        raise GenerationError(f"{symbol} result fields require a struct output")
    _, definition = model.report_type(details)
    return next(field for field in definition["fields"] if field.get("name") == field_name)


def _out_value_type(model: Model, mapping: FunctionMapping) -> dict[str, Any]:
    if mapping.out_param is None:
        raise AssertionError(mapping.abi_symbol)
    function = model.report_function(mapping.abi_symbol)
    out = next(
        param for param in function["params"] if param.get("name") == mapping.out_param
    )
    details = out["ty"].get("details")
    if not isinstance(details, dict) or not isinstance(details.get("name"), str):
        raise GenerationError(f"{mapping.abi_symbol} out_param has invalid type")
    return {"kind": "struct", "details": details["name"]}


def _function_params(function: dict[str, Any], owner: str | None) -> list[dict[str, Any]]:
    params = function["params"]
    if owner is not None and params and Model._is_self_param(params[0], owner):
        return params[1:]
    return params


def _function_abi_alias(function: dict[str, Any]) -> str:
    alias = function.get("mojo_abi_alias")
    if not isinstance(alias, str):
        raise GenerationError("ABI report function lacks mojo_abi_alias")
    _identifier(alias, "ABI report function mojo_abi_alias")
    if not alias.endswith("_abi"):
        raise GenerationError("ABI report function mojo_abi_alias must end in '_abi'")
    return f"_ffi.{alias}"


def _self_mutable(function: dict[str, Any], owner: str) -> bool:
    params = function["params"]
    if not params or not Model._is_self_param(params[0], owner):
        return False
    details = params[0]["ty"].get("details")
    return isinstance(details, dict) and details.get("mutable") is True


def _function_return_type(
    model: Model,
    mapping: FunctionMapping,
    function: dict[str, Any],
    *,
    local_types: bool,
) -> str | None:
    output = function["output"]
    kind, details = _kind(output, mapping.abi_symbol + ".output")
    if mapping.result is not None:
        policy = mapping.result
        if policy.out_value:
            out_ty = _out_value_type(model, mapping)
            return (
                _local_public_type(model, out_ty, mapping.abi_symbol + ".out_value")
                if local_types
                else _public_type(model, out_ty, mapping.abi_symbol + ".out_value")
            )
        if not policy.value_fields:
            return None
        if len(policy.value_fields) == 1:
            field = _result_field(
                model, output, policy.value_fields[0], mapping.abi_symbol
            )
            return (
                _local_public_type(model, field["ty"], mapping.abi_symbol + ".result")
                if local_types
                else _public_type(model, field["ty"], mapping.abi_symbol + ".result")
            )
        assert policy.mojo_type is not None
        return ("" if local_types else "_types.") + policy.mojo_type
    if kind == "unit":
        return None
    rendered = (
        _local_public_type(model, output, mapping.abi_symbol + ".output")
        if local_types
        else _public_type(model, output, mapping.abi_symbol + ".output")
    )
    if (
        kind == "opaque_pointer"
        and isinstance(details, dict)
        and details.get("optional")
        and mapping.kind not in {"constructor", "named_constructor"}
        and mapping.optional_none_error is None
    ):
        return f"Optional[{rendered}]"
    return rendered


def _render_callable(
    model: Model,
    mapping: FunctionMapping,
    function: dict[str, Any],
    *,
    indent: str,
    local_types: bool = False,
) -> list[str]:
    params = _function_params(function, mapping.abi_owner)
    adapter_by_raw = {
        raw_name: adapter
        for adapter in mapping.scalarized_params
        for raw_name in (adapter.data_param, adapter.len_param)
    }
    declarations: list[str] = []
    declared_adapters: set[str] = set()
    for index, param in enumerate(params):
        raw_name = param.get("name")
        if raw_name == mapping.out_param:
            continue
        adapter = adapter_by_raw.get(str(raw_name))
        if adapter is not None:
            if adapter.name not in declared_adapters:
                declarations.append(
                    _scalarized_argument_decl(
                        model, adapter, local_types=local_types
                    )
                )
                declared_adapters.add(adapter.name)
            continue
        declarations.append(
            _argument_decl(
                model,
                mapping,
                param,
                f"{mapping.abi_symbol}.params[{index}]",
                local_types=local_types,
            )
        )
    output = function["output"]
    public_return = _function_return_type(
        model, mapping, function, local_types=local_types
    )
    return_type = f" -> {public_return}" if public_return is not None else ""
    raises = " raises"
    if mapping.kind == "constructor":
        signature = f"def __init__(out self{', ' if declarations else ''}{', '.join(declarations)}) raises:"
    elif mapping.kind in {"named_constructor", "static"}:
        signature = (
            f"def {mapping.mojo_name}({', '.join(declarations)})"
            f"{raises}{return_type}:"
        )
    elif mapping.kind == "free":
        signature = f"def {mapping.mojo_name}({', '.join(declarations)}){raises}{return_type}:"
    else:
        self_decl = "mut self" if _self_mutable(function, str(mapping.abi_owner)) else "self"
        signature = (
            f"def {mapping.mojo_name}({self_decl}"
            f"{', ' if declarations else ''}{', '.join(declarations)}){raises}{return_type}:"
        )
    lines = []
    if mapping.kind in {"named_constructor", "static"}:
        lines.append(indent + "@staticmethod")
    lines.append(indent + signature)
    body_indent = indent + "    "
    raw_return = _raw_type(output, mapping.abi_symbol + ".output")
    owner_mapping = (
        model.type_by_abi.get(mapping.abi_owner) if mapping.abi_owner else None
    )
    local_library = mapping.kind in {"free", "named_constructor", "static"} or (
        mapping.kind == "method"
        and owner_mapping is not None
        and owner_mapping.kind == "value"
    )
    library_expr = "_library" if local_library else "self._library"
    if mapping.kind == "constructor":
        lines.append(body_indent + "self._library = _runtime._open_library()")
    elif local_library:
        lines.append(body_indent + "var _library = _runtime._open_library()")
    lines.append(
        body_indent
        + f'var _call = {library_expr}.get_function[{raw_return}]'
        + f'("{mapping.abi_symbol}")'
    )
    arguments: list[str] = []
    keepalives: list[str] = []
    if mapping.kind in {"method", "iterator_next"}:
        if owner_mapping is not None and owner_mapping.kind == "value":
            if _self_mutable(function, str(mapping.abi_owner)):
                raise GenerationError(
                    f"{mapping.abi_symbol}: mutable value self methods are not supported"
                )
            lines.append(body_indent + "var _ffi_self = self._to_ffi()")
            self_arg = (
                "Pointer(to=_ffi_self).unsafe_mut_cast[False]()"
                ".unsafe_origin_cast[ImmUntrackedOrigin]()"
            )
            keepalives.append("_ = Pointer(to=_ffi_self)")
        else:
            self_arg = "self._require_handle()"
            if not _self_mutable(function, str(mapping.abi_owner)):
                self_arg += ".unsafe_mut_cast[False]()"
        arguments.append(self_arg)
    prepared_adapters: dict[str, dict[str, str]] = {}
    for index, param in enumerate(params):
        raw_name = str(param.get("name"))
        adapter = adapter_by_raw.get(raw_name)
        if adapter is not None:
            expressions = prepared_adapters.get(adapter.name)
            if expressions is None:
                pre, expressions, adapter_keepalives = _prepare_scalarized_argument(
                    model, adapter
                )
                lines.extend(body_indent + line for line in pre)
                prepared_adapters[adapter.name] = expressions
                keepalives.extend(adapter_keepalives)
            arguments.append(expressions[raw_name])
            continue
        if raw_name == mapping.out_param:
            out_ty = param.get("ty")
            if not isinstance(out_ty, dict):
                raise GenerationError(f"{mapping.abi_symbol}.out_param has no ABI type")
            details = out_ty.get("details")
            if not isinstance(details, dict) or not isinstance(details.get("name"), str):
                raise GenerationError(f"{mapping.abi_symbol}.out_param is malformed")
            value_ty = {"kind": "struct", "details": details["name"]}
            lines.append(
                body_indent
                + f"var _out = {_zero_ffi_expr(model, value_ty, mapping.abi_symbol + '.out')}"
            )
            arguments.append(
                "Pointer(to=_out).unsafe_origin_cast[MutUntrackedOrigin]()"
            )
            keepalives.append("_ = Pointer(to=_out)")
            continue
        pre, expression, post = _prepare_call_argument(
            model,
            mapping,
            param,
            f"{mapping.abi_symbol}.params[{index}]",
            local_types=local_types,
        )
        lines.extend(body_indent + line for line in pre)
        arguments.append(expression)
        keepalives.extend(post)
    call = f"_call({', '.join(arguments)})"
    if mapping.kind == "constructor":
        kind, details = _kind(output, mapping.abi_symbol + ".output")
        if kind != "opaque_pointer" or not isinstance(details, dict):
            raise AssertionError(kind)
        if details.get("optional"):
            none_error = mapping.optional_none_error or f"{mapping.abi_owner} construction failed"
            lines.extend(
                [
                    body_indent + f"var _result = {call}",
                    *(body_indent + item for item in keepalives),
                    body_indent + "if not _result:",
                    body_indent + f"    raise Error({json.dumps(none_error)})",
                    body_indent + "self._handle = _result",
                ]
            )
        else:
            if keepalives:
                lines.append(body_indent + f"var _result = {call}")
                lines.extend(body_indent + item for item in keepalives)
                lines.append(body_indent + "self._handle = _result")
            else:
                lines.append(body_indent + f"self._handle = {call}")
        return lines
    output_kind, _ = _kind(output, mapping.abi_symbol + ".output")
    if output_kind == "unit":
        lines.append(body_indent + call)
        lines.extend(body_indent + item for item in keepalives)
        return lines
    if keepalives:
        # Pointer-to-integer conversion and explicit untracked-origin casts sever
        # Mojo's provenance. Evaluate once, then keep every backing local live
        # before inspecting a status or returning the dynamic-call result.
        lines.append(body_indent + f"var _ffi_call_result = {call}")
        lines.extend(body_indent + item for item in keepalives)
        call = "_ffi_call_result"
    output_lines = _call_output(
        model, function, call, mapping, local_types=local_types
    )
    lines.extend(body_indent + body_line for body_line in output_lines)
    return lines


def _iterator_item_type(model: Model, iterator: TypeMapping, next_mapping: FunctionMapping) -> str:
    function = model.report_function(next_mapping.abi_symbol)
    if iterator.next_style == "status-out":
        out = next(
            param for param in function["params"] if param.get("name") == iterator.out_param
        )
        details = out["ty"]["details"]
        target = model.type_by_abi[details["name"]]
        return f"_types.{target.mojo_name}"
    _, step_name = _kind(function["output"], next_mapping.abi_symbol + ".output")
    _, step = model.report_type(step_name)
    field = next(
        field for field in step["fields"] if field.get("name") == iterator.item_field
    )
    return _public_type(model, field["ty"], "iterator item")


def _render_opaque(model: Model, mapping: TypeMapping) -> list[str]:
    assert mapping.mojo_name is not None
    constructors = [
        item
        for item in model.functions
        if item.abi_owner == mapping.abi_name
        and item.kind in {"constructor", "named_constructor"}
    ]
    methods = [
        item
        for item in model.functions
        if item.abi_owner == mapping.abi_name
        and item.kind in {"method", "static"}
    ]
    iterator_next = next(
        (
            item
            for item in model.functions
            if item.abi_owner == mapping.abi_name and item.kind == "iterator_next"
        ),
        None,
    )
    conformances = "Movable"
    if mapping.kind == "iterator":
        conformances = "IterableOwned, Iterator, Movable"
    raw = _mojo_abi_ident(mapping.abi_name)
    lines = [f"struct {mapping.mojo_name}({conformances}):"]
    if mapping.kind == "iterator" and iterator_next is not None:
        item_type = _iterator_item_type(model, mapping, iterator_next)
        lines.extend(
            [
                f"    comptime Element = {item_type}",
                "    comptime IteratorOwnedType = Self",
            ]
        )
    lines.extend(
        [
            f"    var _handle: _ffi.{raw}OptionalHandle",
            "    var _library: OwnedDLHandle",
            "",
            f"    def __init__(out self, *, _from_abi: _ffi.{raw}Handle) raises:",
            "        self._library = _runtime._open_library()",
            "        self._handle = _from_abi",
            "",
            "    def __init__(out self, *, deinit move: Self):",
            "        self._handle = move._handle^",
            "        self._library = move._library^",
            "",
            "    def _require_handle(self) raises -> _ffi." + raw + "Handle:",
            "        if not self._handle:",
            f'            raise Error("{mapping.mojo_name} was moved or destroyed")',
            "        return self._handle.unsafe_value()",
            "",
            "    def __deinit__(deinit self):",
            "        if self._handle:",
            "            try:",
            f'                var _destroy = self._library.get_function[NoneType]("{mapping.destroy_abi_symbol}")',
            "                _destroy(self._handle.unsafe_value())",
            "            except:",
            "                abort()",
        ]
    )
    for constructor in constructors:
        lines.append("")
        lines.extend(
            _render_callable(
                model, constructor, model.report_function(constructor.abi_symbol), indent="    "
            )
        )
    for method in methods:
        lines.append("")
        lines.extend(
            _render_callable(model, method, model.report_function(method.abi_symbol), indent="    ")
        )
    if mapping.kind == "iterator" and iterator_next is not None:
        function = model.report_function(iterator_next.abi_symbol)
        lines.extend(
            [
                "",
                "    def __iter__(var self) -> Self.IteratorOwnedType:",
                "        return self^",
                "",
                "    # One Rust next() call per item; no eager collection is materialized.",
                "    # Exhaustion is None. Boundary failures remain ordinary Mojo Errors.",
                "    def try_next(mut self) raises -> Optional[Self.Element]:",
                "        var _handle = self._require_handle()",
            ]
        )
        if mapping.next_style == "step-return":
            status_field = _mojo_abi_ident(str(mapping.status_field))
            item_field = _mojo_abi_ident(str(mapping.item_field))
            _, step_name = _kind(
                function["output"], iterator_next.abi_symbol + ".output"
            )
            step_ty = {"kind": "struct", "details": step_name}
            lines.extend(
                [
                    f"        var _step = {_zero_ffi_expr(model, step_ty, mapping.abi_name + '.step')}",
                    f'        var _next = self._library.get_function[{_raw_type(function["output"], iterator_next.abi_symbol + ".output")}]("{iterator_next.abi_symbol}")',
                    "        _step = _next(_handle)",
                    f"        var _status = Int(_step.{status_field})",
                    f"        if _status == {mapping.finished_discriminant}:",
                    "            return None",
                ]
            )
            for code, message in mapping.error_discriminants:
                lines.extend(
                    [
                        f"        if _status == {code}:",
                        f"            raise Error({json.dumps(message)})",
                    ]
                )
            lines.extend(
                [
                    f"        if _status != {mapping.item_discriminant}:",
                    f"            raise Error({json.dumps('unexpected FFI iterator status')})",
                ]
            )
            _, step = model.report_type(step_name)
            item = next(
                field
                for field in step["fields"]
                if field.get("name") == mapping.item_field
            )
            lines.append(
                "        return "
                + _from_ffi_expr(model, item["ty"], f"_step.{item_field}")
            )
        else:
            out = next(
                param for param in function["params"] if param.get("name") == mapping.out_param
            )
            out_details = out["ty"]["details"]
            item_ty = {"kind": "struct", "details": out_details["name"]}
            raw_status = _raw_type(function["output"], iterator_next.abi_symbol + ".output")
            lines.extend(
                [
                    f"        var _item = {_zero_ffi_expr(model, item_ty, mapping.abi_name + '.item')}",
                    f"        var _status_value = {raw_status}(0)",
                    f'        var _next = self._library.get_function[{raw_status}]("{iterator_next.abi_symbol}")',
                    "        _status_value = _next(",
                    "            _handle,",
                    "            Pointer(to=_item).unsafe_origin_cast[MutUntrackedOrigin](),",
                    "        )",
                    "        _ = Pointer(to=_item)",
                    "        var _status = Int(_status_value)",
                    f"        if _status == {mapping.finished_discriminant}:",
                    "            return None",
                ]
            )
            for code, message in mapping.error_discriminants:
                lines.extend(
                    [
                        f"        if _status == {code}:",
                        f"            raise Error({json.dumps(message)})",
                    ]
                )
            lines.extend(
                [
                    f"        if _status != {mapping.item_discriminant}:",
                    f"            raise Error({json.dumps('unexpected FFI iterator status')})",
                    f"        return {_from_ffi_expr(model, item_ty, '_item')}",
                ]
            )
        lines.extend(
            [
                "",
                "    # Mojo's Iterator protocol can only raise StopIteration. A foreign",
                "    # boundary failure is therefore fatal here; call try_next() to inspect it.",
                "    def __next__(mut self) raises StopIteration -> Self.Element:",
                "        var _result: Optional[Self.Element]",
                "        try:",
                "            _result = self.try_next()",
                "        except error:",
                "            print(error)",
                "            abort()",
                "        if not _result:",
                "            raise StopIteration()",
                "        var _item = _result.take()",
                "        return _item^",
            ]
        )
    return lines


def _render_wrappers(model: Model) -> str:
    lines = [
        _header(model),
        "from std.collections import ImmSpan, MutSpan, Span",
        "from std.ffi import OwnedDLHandle",
        "from std.os import abort",
        f"import {model.package}._ffi as _ffi",
        f"import {model.package}._runtime as _runtime",
        f"import {model.package}._types as _types",
        "",
    ]
    for mapping in sorted(model.types, key=lambda item: item.mojo_name or item.abi_name):
        if mapping.kind not in {"opaque", "iterator"}:
            continue
        lines.extend(_render_opaque(model, mapping))
        lines.append("")
    for mapping in sorted(model.functions, key=lambda item: item.mojo_name or item.abi_symbol):
        if mapping.kind != "free":
            continue
        lines.extend(
            _render_callable(model, mapping, model.report_function(mapping.abi_symbol), indent="")
        )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_init(model: Model) -> str:
    type_names = sorted(
        {
            mapping.mojo_name
            for mapping in model.types
            if mapping.kind in {"value", "enum"} and mapping.mojo_name is not None
        }
        | {
            mapping.result.mojo_type
            for mapping in model.functions
            if mapping.result is not None and mapping.result.mojo_type is not None
        }
    )
    wrapper_names = sorted(
        [
            mapping.mojo_name
            for mapping in model.types
            if mapping.kind in {"opaque", "iterator"} and mapping.mojo_name is not None
        ]
        + [
            mapping.mojo_name
            for mapping in model.functions
            if mapping.kind == "free" and mapping.mojo_name is not None
        ]
    )
    lines = [_header(model)]
    if type_names:
        lines.append("from ._types import " + ", ".join(type_names))
        enum_constants: list[str] = []
        for mapping in model.types:
            if mapping.kind != "enum" or mapping.mojo_name is None:
                continue
            _, enumeration = model.report_type(mapping.abi_name)
            enum_constants.extend(
                f"{mapping.mojo_name}_{_mojo_abi_ident(str(variant['name']))}"
                for variant in enumeration["variants"]
            )
        if enum_constants:
            lines.append("from ._types import " + ", ".join(sorted(enum_constants)))
    if wrapper_names:
        lines.append("from ._wrappers import " + ", ".join(wrapper_names))
    return "\n".join(lines).rstrip() + "\n"


def _render_model(model: Model) -> dict[str, str]:
    return {
        "_runtime.mojo": _render_runtime(model),
        "_types.mojo": _render_types(model),
        "_wrappers.mojo": _render_wrappers(model),
        "__init__.mojo": _render_init(model),
    }


def generate(manifest: dict[str, Any], report: dict[str, Any], *, manifest_label: str = "binding.toml") -> dict[str, str]:
    model = Model(manifest, report, manifest_label=manifest_label)
    return _render_model(model)


def _write_outputs(output: Path, files: dict[str, str], *, check: bool) -> None:
    if output.exists() and (not output.is_dir() or output.is_symlink()):
        raise GenerationError(f"Output must be a real directory: {output}")
    if output.is_dir():
        unexpected = sorted(
            path.relative_to(output).as_posix()
            for path in output.rglob("*.mojo")
            if path.relative_to(output).as_posix() not in files
        )
        if unexpected:
            raise GenerationError(
                "Generated Mojo package contains unexpected source files: "
                + ", ".join(unexpected)
            )
    if check:
        differences = [
            name
            for name, content in files.items()
            if (output / name).is_symlink()
            or not (output / name).is_file()
            or (output / name).read_text(encoding="utf-8") != content
        ]
        if differences:
            raise GenerationError("Generated Mojo package is stale: " + ", ".join(differences))
        return
    output.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        destination = output / name
        if destination.is_symlink():
            raise GenerationError(f"Refusing to overwrite output symlink: {destination}")
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output, prefix=f".{name}.", delete=False
        ) as handle:
            handle.write(content)
            temporary = Path(handle.name)
        temporary.chmod(0o644)
        temporary.replace(destination)


def _parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--ffi",
        type=Path,
        help="Raw or previously finalized backend _ffi.mojo (defaults to OUTPUT/_ffi.mojo)",
    )
    parser.add_argument("--check", action="store_true")
    return parser.parse_args(list(argv))


def main(argv: Iterable[str] | None = None) -> int:
    options = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        manifest = _load_toml(options.binding)
        report = _load_json(options.report)
        model = Model(manifest, report, manifest_label=str(options.binding))
        files = _render_model(model)
        ffi_source = options.ffi or options.output_dir / "_ffi.mojo"
        try:
            ffi_content = ffi_source.read_text(encoding="utf-8")
        except FileNotFoundError as error:
            raise GenerationError(f"Missing backend _ffi.mojo: {ffi_source}") from error
        except (OSError, UnicodeDecodeError) as error:
            raise GenerationError(f"Cannot read backend _ffi.mojo {ffi_source}: {error}") from error
        files["_ffi.mojo"] = _finalize_ffi_header(model, ffi_content)
        _write_outputs(options.output_dir, files, check=options.check)
    except GenerationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
