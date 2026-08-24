from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import tomllib
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "generate_mojo_package.py"
REPOSITORY_ROOT = SCRIPT.parents[1]
SPEC = importlib.util.spec_from_file_location("generate_mojo_package", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
generator = importlib.util.module_from_spec(SPEC)
# Python 3.12's dataclass implementation resolves postponed annotations through
# sys.modules while the module is executing.
sys.modules[SPEC.name] = generator
SPEC.loader.exec_module(generator)


PREFIX = "rust_mojo__demo_bind__"


def primitive(name: str) -> dict[str, object]:
    return {"kind": "primitive", "details": name}


def struct_type(name: str) -> dict[str, object]:
    return {"kind": "struct", "details": name}


def enum_type(name: str) -> dict[str, object]:
    return {"kind": "enum", "details": name}


def struct_pointer(name: str, *, mutable: bool) -> dict[str, object]:
    return {
        "kind": "struct_pointer",
        "details": {"name": name, "mutable": mutable},
    }


def opaque_pointer(
    name: str, *, mutable: bool, owned: bool, optional: bool = False
) -> dict[str, object]:
    return {
        "kind": "opaque_pointer",
        "details": {
            "name": name,
            "mutable": mutable,
            "owned": owned,
            "optional": optional,
        },
    }


def parameter(name: str, ty: dict[str, object]) -> dict[str, object]:
    return {"name": name, "ty": ty}


def function(
    owner: str | None,
    rust_name: str,
    suffix: str,
    alias: str,
    params: list[dict[str, object]],
    output: dict[str, object],
) -> dict[str, object]:
    return {
        "owner": owner,
        "rust_name": rust_name,
        "abi_name": PREFIX + suffix,
        "mojo_abi_alias": alias,
        "params": params,
        "output": output,
    }


def fixture_report() -> dict[str, object]:
    record_fields = [
        {"name": "start", "ty": primitive("u_int")},
        {"name": "value", "ty": primitive("u_int32")},
        {"name": "kind", "ty": enum_type("ReadStatus")},
    ]
    pair_fields = [
        {"name": "total_value", "ty": primitive("u_int")},
        {"name": "matched_value", "ty": primitive("u_int")},
    ]
    owner_ref = opaque_pointer("Owner", mutable=False, owned=False)
    owner_mut = opaque_pointer("Owner", mutable=True, owned=False)
    iterator_mut = opaque_pointer("RecordIterator", mutable=True, owned=False)
    functions = [
        function(
            "Owner",
            "new",
            "owner_new",
            "Owner_new_abi",
            [
                parameter("records_data", primitive("u_int")),
                parameter("records_len", primitive("u_int")),
                parameter("label_data", primitive("u_int")),
                parameter("label_len", primitive("u_int")),
            ],
            opaque_pointer("Owner", mutable=True, owned=True, optional=True),
        ),
        function(
            "Owner",
            "from_count",
            "owner_from_count",
            "Owner_from_count_abi",
            [parameter("count", primitive("u_int"))],
            opaque_pointer("Owner", mutable=True, owned=True),
        ),
        function(
            "Owner",
            "len",
            "owner_len",
            "Owner_len_abi",
            [parameter("self", owner_ref)],
            primitive("u_int"),
        ),
        function(
            "Owner",
            "score_record",
            "owner_score_record",
            "Owner_score_record_abi",
            [
                parameter("self", owner_ref),
                parameter("record", struct_pointer("Record", mutable=False)),
            ],
            primitive("u_int"),
        ),
        function(
            "Owner",
            "summary",
            "owner_summary",
            "Owner_summary_abi",
            [
                parameter("self", owner_ref),
                parameter("out", struct_pointer("PairOut", mutable=True)),
            ],
            enum_type("ResultStatus"),
        ),
        function(
            "Owner",
            "first",
            "owner_first",
            "Owner_first_abi",
            [
                parameter("self", owner_ref),
                parameter("out", struct_pointer("Record", mutable=True)),
            ],
            enum_type("ResultStatus"),
        ),
        function(
            "Owner",
            "accept_numbers",
            "owner_accept_numbers",
            "Owner_accept_numbers_abi",
            [
                parameter("self", owner_mut),
                parameter("numbers_data", primitive("u_int")),
                parameter("numbers_len", primitive("u_int")),
                parameter("factor", primitive("u_int32")),
            ],
            {"kind": "unit"},
        ),
        function(
            "Owner",
            "records",
            "owner_records",
            "Owner_records_abi",
            [parameter("self", owner_ref)],
            opaque_pointer("RecordIterator", mutable=True, owned=True),
        ),
        function(
            "Owner",
            "validate",
            "owner_validate",
            "Owner_validate_abi",
            [parameter("self", owner_ref)],
            enum_type("ResultStatus"),
        ),
        function(
            "Owner",
            "max_count",
            "owner_max_count",
            "Owner_max_count_abi",
            [
                parameter("left", primitive("u_int")),
                parameter("right", primitive("u_int")),
            ],
            primitive("u_int"),
        ),
        function(
            "Record",
            "overlaps",
            "record_overlaps",
            "Record_overlaps_abi",
            [
                parameter("self", struct_pointer("Record", mutable=False)),
                parameter("other", struct_pointer("Record", mutable=False)),
            ],
            primitive("bool"),
        ),
        function(
            "RecordIterator",
            "next",
            "record_iterator_next",
            "RecordIterator_next_abi",
            [
                parameter("self", iterator_mut),
                parameter("out", struct_pointer("Record", mutable=True)),
            ],
            enum_type("ReadStatus"),
        ),
        function(
            None,
            "parse_owner",
            "parse_owner",
            "parse_owner_abi",
            [
                parameter("text_data", primitive("u_int")),
                parameter("text_len", primitive("u_int")),
            ],
            opaque_pointer("Owner", mutable=True, owned=True, optional=True),
        ),
        function(
            None,
            "internal_helper",
            "internal_helper",
            "internal_helper_abi",
            [],
            {"kind": "unit"},
        ),
    ]
    report = {
        "schema_version": 3,
        "backend": {
            "name": "diplomat-gen-mojo",
            "version": "0.1.0",
            "diplomat_core_version": "0.16.1",
        },
        "mojo_body_sha256": hashlib.sha256(
            RAW_FFI.split("\n\n", 1)[1].encode("utf-8")
        ).hexdigest(),
        "structs": [
            {"name": "Record", "fields": record_fields},
            {"name": "PairOut", "fields": pair_fields},
        ],
        "opaques": [
            {
                "name": "Owner",
                "destructor_abi_name": PREFIX + "owner_destroy",
                "destructor_mojo_abi_alias": "Owner_destroy_abi",
            },
            {
                "name": "RecordIterator",
                "destructor_abi_name": PREFIX + "record_iterator_destroy",
                "destructor_mojo_abi_alias": "RecordIterator_destroy_abi",
            },
        ],
        "enums": [
            {
                "name": "ReadStatus",
                "variants": [
                    {"name": "Item", "discriminant": 0},
                    {"name": "Finished", "discriminant": 1},
                    {"name": "Panic", "discriminant": 2},
                ],
            },
            {
                "name": "ResultStatus",
                "variants": [
                    {"name": "Ok", "discriminant": 0},
                    {"name": "Panic", "discriminant": 1},
                ],
            },
        ],
        "functions": functions,
        "types": [],
        "unsupported": [],
    }
    report["abi_model_sha256"] = generator._abi_model_sha256(report)
    return report


def refresh_report_hash(report: dict[str, object]) -> None:
    report["abi_model_sha256"] = generator._abi_model_sha256(report)


MANIFEST_TOML = textwrap.dedent(
    f'''\
    schema_version = 1

    [binding]
    id = "demo-bind"
    mojo_package = "demo_bind"
    symbol_prefix = "{PREFIX}"

    [crate]
    name = "demo-crate"
    version = "=1.2.3"

    [ffi]
    crate_name = "demo_bind_ffi"

    [mojo]
    source_dir = "mojo/demo_bind"
    tests = ["tests/smoke.mojo"]

    [scope]
    requested = ["demo_crate::Owner", "demo_crate::Record"]
    trait_policy = "Only the iterator behavior named by this fixture is in scope."
    audited_exports = [
      "owner.type",
      "record.type",
      "record.status",
      "owner.len",
      "owner.from_count",
      "owner.max_count",
      "record.overlaps",
      "owner.score",
      "owner.summary",
      "owner.first",
      "owner.accept",
      "owner.records",
      "owner.parse",
      "owner.validate",
      "helper.skip",
    ]

    [tools]
    generator_version = "0.4.0"
    abi_backend_version = "0.1.0"
    diplomat_version = "0.16.1"
    diplomat_core_version = "0.16.1"
    diplomat_runtime_version = "0.16.0"
    mojo_version = "1.0.0"

    [[adaptations]]
    export = "owner.score"
    rust = "demo_crate::Owner::score_record"
    mojo = "Owner.score_record"
    kind = "value-argument-pointer"
    effect = "The value argument crosses through an immutable pointer."
    chosen_by = "user"

    [[adaptations]]
    export = "owner.summary"
    rust = "demo_crate::Owner::summary"
    mojo = "Owner.summary"
    kind = "status-out"
    effect = "The aggregate result uses scalar status plus caller-owned output."
    chosen_by = "user"

    [[adaptations]]
    export = "owner.first"
    rust = "demo_crate::Owner::first"
    mojo = "Owner.first"
    kind = "status-out"
    effect = "The value result uses scalar status plus caller-owned output."
    chosen_by = "user"

    [[adaptations]]
    export = "owner.accept"
    rust = "demo_crate::Owner::accept_numbers"
    mojo = "Owner.accept_numbers"
    kind = "scalarized-slice"
    effect = "The borrowed slice is scalarized to address and length."
    chosen_by = "user"

    [[adaptations]]
    export = "owner.records"
    rust = "demo_crate::Owner::records"
    mojo = "Owner.records"
    kind = "lazy-rust-iterator"
    effect = "A retained Rust iterator advances lazily across the C ABI."
    chosen_by = "user"

    [[adaptations]]
    export = "owner.parse"
    rust = "demo_crate::parse_owner"
    mojo = "parse_owner"
    kind = "scalarized-utf8"
    effect = "The UTF-8 string is scalarized to address and byte length."
    chosen_by = "user"

    [[adaptations]]
    export = "owner.validate"
    rust = "demo_crate::Owner::validate"
    mojo = "Owner.validate"
    kind = "raising-status"
    effect = "The scalar status becomes a raising Mojo method."
    chosen_by = "user"

    [[exports]]
    id = "owner.type"
    rust = "demo_crate::Owner"
    mojo = "Owner"
    status = "OPAQUE"
    reason = "Rust layout remains opaque."

    [[exports]]
    id = "record.type"
    rust = "demo_crate::Record"
    mojo = "Record"
    status = "DIRECT"
    reason = "Fields have an explicit value projection."

    [[exports]]
    id = "record.status"
    rust = "demo_crate::ReadStatus"
    mojo = "ReadStatus"
    status = "DIRECT"
    reason = "Fieldless enum with complete discriminants."

    [[exports]]
    id = "owner.len"
    rust = "demo_crate::Owner::len"
    mojo = "Owner.len"
    status = "DIRECT"
    reason = "Scalar method."

    [[exports]]
    id = "owner.from_count"
    rust = "demo_crate::Owner::from_count"
    mojo = "Owner.from_count"
    status = "DIRECT"
    reason = "Named constructor."

    [[exports]]
    id = "owner.max_count"
    rust = "demo_crate::Owner::max_count"
    mojo = "Owner.max_count"
    status = "DIRECT"
    reason = "Static scalar helper."

    [[exports]]
    id = "record.overlaps"
    rust = "demo_crate::Record::overlaps"
    mojo = "Record.overlaps"
    status = "DIRECT"
    reason = "Value method with an explicit pointer projection."

    [[exports]]
    id = "owner.score"
    rust = "demo_crate::Owner::score_record"
    mojo = "Owner.score_record"
    status = "ADAPTED"
    reason = "Value argument crosses through an immutable pointer."

    [[exports]]
    id = "owner.summary"
    rust = "demo_crate::Owner::summary"
    mojo = "Owner.summary"
    status = "ADAPTED"
    reason = "Aggregate result uses scalar status plus caller-owned output."

    [[exports]]
    id = "owner.first"
    rust = "demo_crate::Owner::first"
    mojo = "Owner.first"
    status = "ADAPTED"
    reason = "Value result uses scalar status plus caller-owned output."

    [[exports]]
    id = "owner.accept"
    rust = "demo_crate::Owner::accept_numbers"
    mojo = "Owner.accept_numbers"
    status = "ADAPTED"
    reason = "Borrowed slice is scalarized to address and length."

    [[exports]]
    id = "owner.records"
    rust = "demo_crate::Owner::records"
    mojo = "Owner.records"
    status = "ADAPTED"
    reason = "Retained Rust iterator advances lazily."

    [[exports]]
    id = "owner.parse"
    rust = "demo_crate::parse_owner"
    mojo = "parse_owner"
    status = "ADAPTED"
    reason = "UTF-8 string is scalarized to address and byte length."

    [[exports]]
    id = "owner.validate"
    rust = "demo_crate::Owner::validate"
    mojo = "Owner.validate"
    status = "ADAPTED"
    reason = "Scalar status becomes a raising Mojo method."

    [[exports]]
    id = "helper.skip"
    rust = "demo_crate::internal_helper"
    status = "SKIPPED"
    reason = "Private bridge diagnostic."

    [[mojo.types]]
    abi_name = "Record"
    mojo_name = "Record"
    kind = "value"
    export = "record.type"

    [[mojo.types]]
    abi_name = "PairOut"
    kind = "skip"
    reason = "Internal scalar-status output carrier."

    [[mojo.types]]
    abi_name = "ReadStatus"
    mojo_name = "ReadStatus"
    kind = "enum"
    export = "record.status"

    [[mojo.types]]
    abi_name = "ResultStatus"
    kind = "skip"
    reason = "Internal status translated to Mojo errors."

    [[mojo.types]]
    abi_name = "Owner"
    mojo_name = "Owner"
    kind = "opaque"
    destroy_abi_symbol = "{PREFIX}owner_destroy"
    export = "owner.type"

    [[mojo.types]]
    abi_name = "RecordIterator"
    mojo_name = "RecordIterator"
    kind = "iterator"
    destroy_abi_symbol = "{PREFIX}record_iterator_destroy"
    next_style = "status-out"
    out_param = "out"
    item_discriminant = 0
    finished_discriminant = 1
    error_discriminants = {{ "2" = "Rust iterator panicked" }}
    export = "owner.records"

    [[mojo.functions]]
    abi_owner = "Owner"
    rust_name = "new"
    abi_symbol = "{PREFIX}owner_new"
    mojo_name = "__init__"
    kind = "constructor"
    scalarized_params = [
      {{ name = "records", kind = "copied-value-slice", element = "Record", data_param = "records_data", len_param = "records_len" }},
      {{ name = "label", kind = "utf8-string", element = "u_int8", data_param = "label_data", len_param = "label_len" }},
    ]
    optional_none_error = "Owner construction failed"
    export = "owner.type"

    [[mojo.functions]]
    abi_owner = "Owner"
    rust_name = "from_count"
    abi_symbol = "{PREFIX}owner_from_count"
    mojo_name = "from_count"
    kind = "named_constructor"
    export = "owner.from_count"

    [[mojo.functions]]
    abi_owner = "Owner"
    rust_name = "len"
    abi_symbol = "{PREFIX}owner_len"
    mojo_name = "len"
    kind = "method"
    export = "owner.len"

    [[mojo.functions]]
    abi_owner = "Owner"
    rust_name = "score_record"
    abi_symbol = "{PREFIX}owner_score_record"
    mojo_name = "score_record"
    kind = "method"
    export = "owner.score"

    [[mojo.functions]]
    abi_owner = "Owner"
    rust_name = "summary"
    abi_symbol = "{PREFIX}owner_summary"
    mojo_name = "summary"
    kind = "method"
    out_param = "out"
    result = {{ ok_discriminant = 0, errors = {{ "1" = "Rust summary panicked" }}, value_fields = ["total_value", "matched_value"], mojo_type = "Summary", value_names = {{ total_value = "total", matched_value = "matched" }} }}
    export = "owner.summary"

    [[mojo.functions]]
    abi_owner = "Owner"
    rust_name = "first"
    abi_symbol = "{PREFIX}owner_first"
    mojo_name = "first"
    kind = "method"
    out_param = "out"
    result = {{ ok_discriminant = 0, errors = {{ "1" = "Rust first panicked" }}, out_value = true }}
    export = "owner.first"

    [[mojo.functions]]
    abi_owner = "Owner"
    rust_name = "accept_numbers"
    abi_symbol = "{PREFIX}owner_accept_numbers"
    mojo_name = "accept_numbers"
    kind = "method"
    scalarized_params = [
      {{ name = "numbers", kind = "borrowed-primitive-slice", element = "u_int32", data_param = "numbers_data", len_param = "numbers_len" }},
    ]
    export = "owner.accept"

    [[mojo.functions]]
    abi_owner = "Owner"
    rust_name = "records"
    abi_symbol = "{PREFIX}owner_records"
    mojo_name = "records"
    kind = "method"
    export = "owner.records"

    [[mojo.functions]]
    abi_owner = "Owner"
    rust_name = "validate"
    abi_symbol = "{PREFIX}owner_validate"
    mojo_name = "validate"
    kind = "method"
    result = {{ ok_discriminant = 0, errors = {{ "1" = "Rust validation panicked" }}, value_fields = [] }}
    export = "owner.validate"

    [[mojo.functions]]
    abi_owner = "Owner"
    rust_name = "max_count"
    abi_symbol = "{PREFIX}owner_max_count"
    mojo_name = "max_count"
    kind = "static"
    export = "owner.max_count"

    [[mojo.functions]]
    abi_owner = "Record"
    rust_name = "overlaps"
    abi_symbol = "{PREFIX}record_overlaps"
    mojo_name = "overlaps"
    kind = "method"
    export = "record.overlaps"

    [[mojo.functions]]
    abi_owner = "RecordIterator"
    rust_name = "next"
    abi_symbol = "{PREFIX}record_iterator_next"
    mojo_name = "__next__"
    kind = "iterator_next"
    export = "owner.records"

    [[mojo.functions]]
    rust_name = "parse_owner"
    abi_symbol = "{PREFIX}parse_owner"
    mojo_name = "parse_owner"
    kind = "free"
    scalarized_params = [
      {{ name = "text", kind = "utf8-string", element = "u_int8", data_param = "text_data", len_param = "text_len" }},
    ]
    export = "owner.parse"

    [[mojo.functions]]
    rust_name = "internal_helper"
    abi_symbol = "{PREFIX}internal_helper"
    kind = "skip"
    reason = "Private bridge diagnostic."
    '''
)


RAW_FFI = textwrap.dedent(
    f'''\
    # GENERATED FILE — DO NOT EDIT DIRECTLY
    # Generator: diplomat-gen-mojo 0.1.0
    # Diplomat core: 0.16.1
    # ABI model SHA-256: 49b4642e37eda76bb4875ca9ed9067b396893041d4f8be644aef5fce88f64746

    comptime ReadStatus = Int32
    comptime ReadStatus_Item: ReadStatus = 0
    comptime ReadStatus_Finished: ReadStatus = 1
    comptime ReadStatus_Panic: ReadStatus = 2

    comptime ResultStatus = Int32
    comptime ResultStatus_Ok: ResultStatus = 0
    comptime ResultStatus_Panic: ResultStatus = 1

    @fieldwise_init
    struct Record(TrivialRegisterPassable):
        var start: UInt
        var value: UInt32
        var kind: ReadStatus

    @fieldwise_init
    struct PairOut(TrivialRegisterPassable):
        var total_value: UInt
        var matched_value: UInt

    comptime OwnerHandle = Pointer[UInt8, MutUntrackedOrigin]
    comptime OwnerRef = Pointer[UInt8, ImmUntrackedOrigin]
    comptime OwnerMutRef = Pointer[UInt8, MutUntrackedOrigin]
    comptime OwnerOptionalHandle = OptionalPointer[UInt8, MutUntrackedOrigin]
    comptime Owner_destroy_abi = def(OwnerHandle) thin abi("C") -> None
    # symbol Owner_destroy_abi = "{PREFIX}owner_destroy"

    comptime RecordIteratorHandle = Pointer[UInt8, MutUntrackedOrigin]
    comptime RecordIteratorRef = Pointer[UInt8, ImmUntrackedOrigin]
    comptime RecordIteratorMutRef = Pointer[UInt8, MutUntrackedOrigin]
    comptime RecordIteratorOptionalHandle = OptionalPointer[UInt8, MutUntrackedOrigin]
    comptime RecordIterator_destroy_abi = def(RecordIteratorHandle) thin abi("C") -> None
    # symbol RecordIterator_destroy_abi = "{PREFIX}record_iterator_destroy"

    comptime Owner_new_abi = def(UInt, UInt, UInt, UInt) thin abi("C") -> OwnerOptionalHandle
    # symbol Owner_new_abi = "{PREFIX}owner_new"
    comptime Owner_from_count_abi = def(UInt) thin abi("C") -> OwnerHandle
    # symbol Owner_from_count_abi = "{PREFIX}owner_from_count"
    comptime Owner_len_abi = def(OwnerRef) thin abi("C") -> UInt
    # symbol Owner_len_abi = "{PREFIX}owner_len"
    comptime Owner_score_record_abi = def(OwnerRef, Pointer[Record, ImmUntrackedOrigin]) thin abi("C") -> UInt
    # symbol Owner_score_record_abi = "{PREFIX}owner_score_record"
    comptime Owner_summary_abi = def(OwnerRef, Pointer[PairOut, MutUntrackedOrigin]) thin abi("C") -> ResultStatus
    # symbol Owner_summary_abi = "{PREFIX}owner_summary"
    comptime Owner_first_abi = def(OwnerRef, Pointer[Record, MutUntrackedOrigin]) thin abi("C") -> ResultStatus
    # symbol Owner_first_abi = "{PREFIX}owner_first"
    comptime Owner_accept_numbers_abi = def(OwnerMutRef, UInt, UInt, UInt32) thin abi("C") -> None
    # symbol Owner_accept_numbers_abi = "{PREFIX}owner_accept_numbers"
    comptime Owner_records_abi = def(OwnerRef) thin abi("C") -> RecordIteratorHandle
    # symbol Owner_records_abi = "{PREFIX}owner_records"
    comptime Owner_validate_abi = def(OwnerRef) thin abi("C") -> ResultStatus
    # symbol Owner_validate_abi = "{PREFIX}owner_validate"
    comptime Owner_max_count_abi = def(UInt, UInt) thin abi("C") -> UInt
    # symbol Owner_max_count_abi = "{PREFIX}owner_max_count"
    comptime Record_overlaps_abi = def(Pointer[Record, ImmUntrackedOrigin], Pointer[Record, ImmUntrackedOrigin]) thin abi("C") -> Bool
    # symbol Record_overlaps_abi = "{PREFIX}record_overlaps"
    comptime RecordIterator_next_abi = def(RecordIteratorMutRef, Pointer[Record, MutUntrackedOrigin]) thin abi("C") -> ReadStatus
    # symbol RecordIterator_next_abi = "{PREFIX}record_iterator_next"
    comptime parse_owner_abi = def(UInt, UInt) thin abi("C") -> OwnerOptionalHandle
    # symbol parse_owner_abi = "{PREFIX}parse_owner"
    comptime internal_helper_abi = def() thin abi("C") -> None
    # symbol internal_helper_abi = "{PREFIX}internal_helper"
    '''
)


def fixture_manifest() -> dict[str, object]:
    return tomllib.loads(MANIFEST_TOML)


def function_mapping(manifest: dict[str, object], symbol_suffix: str) -> dict[str, object]:
    mojo = manifest["mojo"]
    assert isinstance(mojo, dict)
    functions = mojo["functions"]
    assert isinstance(functions, list)
    symbol = PREFIX + symbol_suffix
    return next(item for item in functions if item.get("abi_symbol") == symbol)


def report_function(report: dict[str, object], symbol_suffix: str) -> dict[str, object]:
    functions = report["functions"]
    assert isinstance(functions, list)
    symbol = PREFIX + symbol_suffix
    return next(item for item in functions if item.get("abi_name") == symbol)


def export_record(manifest: dict[str, object], export_id: str) -> dict[str, object]:
    exports = manifest["exports"]
    assert isinstance(exports, list)
    return next(item for item in exports if item.get("id") == export_id)


def adaptation_record(
    manifest: dict[str, object], export_id: str
) -> dict[str, object]:
    adaptations = manifest["adaptations"]
    assert isinstance(adaptations, list)
    return next(item for item in adaptations if item.get("export") == export_id)


def section(source: str, start: str, end: str | None = None) -> str:
    begin = source.index(start)
    finish = len(source) if end is None else source.index(end, begin + len(start))
    return source[begin:finish]


class SuccessfulGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.files = generator.generate(fixture_manifest(), fixture_report())

    def test_value_enum_and_synthetic_result_types_are_rendered(self) -> None:
        types = self.files["_types.mojo"]
        self.assertIn("comptime ReadStatus = _ffi.ReadStatus", types)
        self.assertIn("comptime ReadStatus_Item: ReadStatus", types)
        self.assertIn("struct Record(Copyable, Movable):", types)
        self.assertIn("def __init__(out self, *, _from_ffi: _ffi.Record):", types)
        self.assertIn("def _to_ffi(self) raises -> _ffi.Record:", types)
        self.assertIn("def _validate_ReadStatus(value: ReadStatus) raises:", types)
        self.assertIn('raise Error("invalid ReadStatus discriminant")', types)
        self.assertIn("_validate_ReadStatus(self.kind)", types)
        self.assertIn("struct Summary(Copyable, Movable):", types)
        self.assertIn("var total: UInt", types)
        self.assertIn("var matched: UInt", types)
        self.assertNotIn("PairOut", self.files["__init__.mojo"])
        self.assertNotIn("ResultStatus", self.files["__init__.mojo"])

    def test_opaque_wrappers_are_move_only_raii_owners(self) -> None:
        wrappers = self.files["_wrappers.mojo"]
        owner = section(wrappers, "struct Owner(Movable):", "struct RecordIterator")
        self.assertIn("var _handle: _ffi.OwnerOptionalHandle", owner)
        self.assertIn("def __init__(out self, *, deinit move: Self):", owner)
        self.assertIn("self._handle = move._handle^", owner)
        self.assertNotIn("_handle = None", owner)
        self.assertIn("def __deinit__(deinit self):", owner)
        self.assertIn(
            f'get_function[NoneType]("{PREFIX}owner_destroy")', owner
        )
        self.assertIn("_destroy(self._handle.unsafe_value())", owner)
        self.assertIn("abort()", owner)
        self.assertNotIn("Copyable", owner.splitlines()[0])

    def test_runtime_lookups_use_return_type_only(self) -> None:
        generated = self.files["_types.mojo"] + self.files["_wrappers.mojo"]
        self.assertIn(f'get_function[UInt]("{PREFIX}owner_len")', generated)
        self.assertIn(
            f'get_function[_ffi.ResultStatus]("{PREFIX}owner_summary")', generated
        )
        self.assertNotRegex(generated, r"get_function\[_ffi\.[A-Za-z0-9_]+_abi\]")
        self.assertNotIn("Owner_len_abi", generated)

    def test_runtime_lookup_uses_the_canonical_process_executable(self) -> None:
        runtime = self.files["_runtime.mojo"]
        self.assertIn('realpath("/proc/self/exe")', runtime)
        self.assertIn('external_call["_NSGetExecutablePath", Int32]', runtime)
        self.assertIn('for component in search_path.split(":"):', runtime)
        self.assertIn('if invoked.rfind("/") >= 0:', runtime)
        self.assertIn("dirname(_executable_path())", runtime)
        self.assertNotIn("dirname(String(arguments[0]))", runtime)
        override = runtime.index('getenv("RUST_MOJO_DEMO_BIND_LIBRARY")')
        executable = runtime.index("dirname(_executable_path())")
        prefix = runtime.index('getenv("CONDA_PREFIX")')
        self.assertLess(override, executable)
        self.assertLess(executable, prefix)

    def test_raw_abi_identifier_escaping_matches_the_backend_contract(self) -> None:
        for name in ("mut", "out", "read", "ref", "std", "TrivialRegisterPassable"):
            with self.subTest(name=name):
                self.assertEqual(generator._mojo_abi_ident(name), name + "_")

    def test_scalarized_inputs_have_public_semantic_signatures(self) -> None:
        wrappers = self.files["_wrappers.mojo"]
        self.assertIn(
            "def __init__(out self, records: ImmSpan[_types.Record, _], label: String) raises:",
            wrappers,
        )
        self.assertIn("var _ffi_records = List[_ffi.Record]()", wrappers)
        self.assertIn("var _ffi_records_span = Span(_ffi_records)", wrappers)
        self.assertIn("var _bytes_label = label.as_bytes()", wrappers)
        self.assertIn("UInt(Int(_ffi_records_span.unsafe_ptr()))", wrappers)
        self.assertIn("UInt(Int(_bytes_label.unsafe_ptr()))", wrappers)
        self.assertIn(
            "def accept_numbers(mut self, numbers: ImmSpan[UInt32, _], factor: UInt32) raises:",
            wrappers,
        )
        self.assertIn("UInt(Int(numbers.unsafe_ptr()))", wrappers)
        self.assertIn("def parse_owner(text: String) raises -> Optional[Owner]:", wrappers)

    def test_scalar_status_out_results_raise_before_reading_output(self) -> None:
        wrappers = self.files["_wrappers.mojo"]
        summary = section(wrappers, "    def summary", "    def validate")
        self.assertIn("raises -> _types.Summary", summary)
        self.assertIn("var _out = _ffi.PairOut", summary)
        self.assertIn("var _result = _ffi_call_result", summary)
        self.assertIn('raise Error("Rust summary panicked")', summary)
        self.assertIn(
            "return _types.Summary(total=_out.total_value, matched=_out.matched_value)",
            summary,
        )
        self.assertLess(summary.index("if Int(_result)"), summary.index("return _types.Summary"))
        first = section(wrappers, "    def first", "    @staticmethod")
        self.assertIn("raises -> _types.Record", first)
        self.assertIn("return _types.Record(_from_ffi=_out)", first)
        validate = section(wrappers, "    def validate", "struct RecordIterator")
        self.assertIn('raise Error("Rust validation panicked")', validate)
        self.assertRegex(validate, r"if Int\(_result\) != 0:[\s\S]*?raise Error")

    def test_lazy_iterator_calls_rust_once_per_step_and_never_reads_failed_out(self) -> None:
        wrappers = self.files["_wrappers.mojo"]
        iterator = section(wrappers, "struct RecordIterator", "def parse_owner")
        self.assertIn("IterableOwned, Iterator, Movable", iterator)
        self.assertIn("comptime Element = _types.Record", iterator)
        self.assertIn("def try_next(mut self) raises -> Optional[Self.Element]:", iterator)
        self.assertIn("def __next__(mut self) raises StopIteration -> Self.Element:", iterator)
        self.assertEqual(iterator.count("_status_value = _next("), 1)
        call = iterator.index("_status_value = _next(")
        keepalive = iterator.index("_ = Pointer(to=_item)", call)
        status = iterator.index("var _status = Int(_status_value)", keepalive)
        failed = iterator.index('raise Error("Rust iterator panicked")', status)
        converted = iterator.index("_types.Record(_from_ffi=_item)", failed)
        self.assertLess(call, keepalive)
        self.assertLess(keepalive, status)
        self.assertLess(failed, converted)
        self.assertNotIn("List[_types.Record]", iterator)

    def test_static_named_constructor_and_free_function_are_rendered(self) -> None:
        wrappers = self.files["_wrappers.mojo"]
        self.assertIn("@staticmethod\n    def from_count(count: UInt) raises -> Owner:", wrappers)
        self.assertIn("@staticmethod\n    def max_count(left: UInt, right: UInt) raises -> UInt:", wrappers)
        self.assertIn("def parse_owner(text: String) raises -> Optional[Owner]:", wrappers)

    def test_public_init_reexports_only_public_names(self) -> None:
        init = self.files["__init__.mojo"]
        self.assertIn("from ._types import ReadStatus, Record, Summary", init)
        self.assertIn("from ._wrappers import Owner, RecordIterator, parse_owner", init)
        self.assertNotIn("internal_helper", init)


class KeepaliveOrderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.files = generator.generate(fixture_manifest(), fixture_report())

    def assert_ordered(self, source: str, *needles: str) -> None:
        position = -1
        for needle in needles:
            found = source.index(needle, position + 1)
            self.assertGreater(found, position, needle)
            position = found

    def test_constructor_keeps_copied_list_span_and_string_bytes_after_call(self) -> None:
        owner = section(
            self.files["_wrappers.mojo"],
            "    def __init__(out self, records:",
            "    @staticmethod",
        )
        self.assert_ordered(
            owner,
            "var _result = _call(",
            "_ = len(_ffi_records_span)",
            "_ = len(_ffi_records)",
            "_ = len(_bytes_label)",
            "if not _result:",
        )

    def test_constructor_keeps_an_ordinary_value_pointer_after_call(self) -> None:
        manifest = fixture_manifest()
        report = fixture_report()
        constructor = function_mapping(manifest, "owner_new")
        constructor.pop("scalarized_params")
        report_function(report, "owner_new")["params"] = [
            parameter("record", struct_pointer("Record", mutable=False))
        ]
        refresh_report_hash(report)
        wrappers = generator.generate(manifest, report)["_wrappers.mojo"]
        owner = section(
            wrappers,
            "    def __init__(out self, record:",
            "    @staticmethod",
        )
        self.assert_ordered(
            owner,
            "var _result = _call(",
            "_ = Pointer(to=_ffi_record)",
            "if not _result:",
        )

    def test_value_self_and_value_argument_are_kept_after_returning_call(self) -> None:
        overlaps = section(self.files["_types.mojo"], "    def overlaps", None)
        self.assert_ordered(
            overlaps,
            "var _ffi_call_result = _call(",
            "_ = Pointer(to=_ffi_self)",
            "_ = Pointer(to=_ffi_other)",
            "return _ffi_call_result",
        )

    def test_ordinary_value_argument_is_kept_before_scalar_return(self) -> None:
        score = section(
            self.files["_wrappers.mojo"], "    def score_record", "    def summary"
        )
        self.assert_ordered(
            score,
            "var _ffi_call_result = _call(",
            "_ = Pointer(to=_ffi_record)",
            "return _ffi_call_result",
        )

    def test_out_storage_is_kept_before_status_error_branch(self) -> None:
        summary = section(
            self.files["_wrappers.mojo"], "    def summary", "    def validate"
        )
        self.assert_ordered(
            summary,
            "var _ffi_call_result = _call(",
            "_ = Pointer(to=_out)",
            "var _result = _ffi_call_result",
            "if Int(_result) == 1:",
        )

    def test_borrowed_span_is_kept_after_unit_call(self) -> None:
        method = section(
            self.files["_wrappers.mojo"],
            "    def accept_numbers",
            "    def records",
        )
        self.assert_ordered(method, "_call(", "_ = len(numbers)")

    def test_string_bytes_are_kept_before_optional_branch(self) -> None:
        free = section(self.files["_wrappers.mojo"], "def parse_owner", None)
        self.assert_ordered(
            free,
            "var _ffi_call_result = _call(",
            "_ = len(_bytes_text)",
            "var _result = _ffi_call_result",
            "if not _result:",
        )


class EnumSafetyTests(unittest.TestCase):
    def test_direct_public_enum_input_is_checked_before_the_ffi_call(self) -> None:
        report = fixture_report()
        length = report_function(report, "owner_len")
        length["params"].append(parameter("status", enum_type("ReadStatus")))
        refresh_report_hash(report)
        wrappers = generator.generate(fixture_manifest(), report)["_wrappers.mojo"]
        method = section(wrappers, "    def len", "    @staticmethod")
        self.assertIn("status: _types.ReadStatus", method)
        self.assertLess(
            method.index("_types._validate_ReadStatus(status)"),
            method.index("_ffi_call_result = _call("),
        )

    def test_out_storage_uses_a_declared_nonzero_enum_variant(self) -> None:
        report = fixture_report()
        status = next(item for item in report["enums"] if item["name"] == "ReadStatus")
        for variant, discriminant in zip(status["variants"], (7, 8, 9), strict=True):
            variant["discriminant"] = discriminant
        manifest = fixture_manifest()
        mojo = manifest["mojo"]
        assert isinstance(mojo, dict)
        iterator = next(
            item for item in mojo["types"] if item["abi_name"] == "RecordIterator"
        )
        iterator["item_discriminant"] = 7
        iterator["finished_discriminant"] = 8
        iterator["error_discriminants"] = {"9": "Rust iterator panicked"}
        refresh_report_hash(report)
        wrappers = generator.generate(manifest, report)["_wrappers.mojo"]
        iterator_source = section(wrappers, "struct RecordIterator", "def parse_owner")
        self.assertIn("kind=_ffi.ReadStatus(7)", iterator_source)
        self.assertNotIn("kind=_ffi.ReadStatus(0)", iterator_source)


class ClosedSchemaValidationTests(unittest.TestCase):
    def assert_generation_error(
        self,
        manifest: dict[str, object],
        report: dict[str, object],
        message: str,
    ) -> None:
        with self.assertRaisesRegex(generator.GenerationError, message):
            generator.generate(manifest, report)

    def test_report_schema_three_is_required(self) -> None:
        report = fixture_report()
        report["schema_version"] = 1
        self.assert_generation_error(fixture_manifest(), report, "schema_version must be 3")

    def test_tool_and_backend_provenance_must_match_exactly(self) -> None:
        manifest = fixture_manifest()
        tools = manifest["tools"]
        assert isinstance(tools, dict)
        tools.pop("abi_backend_version")
        self.assert_generation_error(
            manifest, fixture_report(), "abi_backend_version must be a non-empty"
        )

        report = fixture_report()
        backend = report["backend"]
        assert isinstance(backend, dict)
        backend["version"] = "9.9.9"
        self.assert_generation_error(
            fixture_manifest(), report, "backend version does not match"
        )

        report = fixture_report()
        report["mojo_body_sha256"] = "not-a-hash"
        self.assert_generation_error(
            fixture_manifest(), report, "64 lowercase hexadecimal"
        )

    def test_report_declaration_edits_are_rejected_by_model_digest(self) -> None:
        tampered_reports: dict[str, dict[str, object]] = {}

        report = fixture_report()
        structs = report["structs"]
        assert isinstance(structs, list)
        record = next(item for item in structs if item["name"] == "Record")
        record["fields"][0]["ty"] = primitive("u_int8")
        tampered_reports["struct field type"] = report

        report = fixture_report()
        enums = report["enums"]
        assert isinstance(enums, list)
        status = next(item for item in enums if item["name"] == "ReadStatus")
        status["variants"][0]["name"] = "Available"
        tampered_reports["enum variant"] = report

        report = fixture_report()
        opaques = report["opaques"]
        assert isinstance(opaques, list)
        owner = next(item for item in opaques if item["name"] == "Owner")
        owner["destructor_mojo_abi_alias"] = "Owner_drop_abi"
        tampered_reports["opaque carrier metadata"] = report

        for label, report in tampered_reports.items():
            with self.subTest(label=label):
                self.assert_generation_error(
                    fixture_manifest(),
                    report,
                    "declarations do not match abi_model_sha256",
                )

    def test_requested_scope_and_trait_policy_are_required(self) -> None:
        manifest = fixture_manifest()
        manifest.pop("scope")
        self.assert_generation_error(
            manifest, fixture_report(), r"requires a \[scope\] table"
        )
        manifest = fixture_manifest()
        scope = manifest["scope"]
        assert isinstance(scope, dict)
        scope["requested"] = []
        self.assert_generation_error(manifest, fixture_report(), "must name at least one")

    def test_scope_audited_exports_is_required_and_exact(self) -> None:
        manifest = fixture_manifest()
        scope = manifest["scope"]
        assert isinstance(scope, dict)
        scope.pop("audited_exports")
        self.assert_generation_error(
            manifest, fixture_report(), "audited_exports must name every"
        )

        manifest = fixture_manifest()
        scope = manifest["scope"]
        assert isinstance(scope, dict)
        audited = scope["audited_exports"]
        assert isinstance(audited, list)
        audited.remove("owner.validate")
        self.assert_generation_error(
            manifest, fixture_report(), "audited_exports must exactly equal.*owner.validate"
        )

    def test_scope_audited_exports_rejects_duplicates(self) -> None:
        manifest = fixture_manifest()
        scope = manifest["scope"]
        assert isinstance(scope, dict)
        audited = scope["audited_exports"]
        assert isinstance(audited, list)
        audited.append("owner.type")
        self.assert_generation_error(
            manifest, fixture_report(), "audited_exports must not contain duplicates"
        )

    def test_unsupported_report_items_fail_closed(self) -> None:
        report = fixture_report()
        report["unsupported"] = [{"item": "Owner::async", "reason": "async Rust"}]
        self.assert_generation_error(fixture_manifest(), report, "Owner::async: async Rust")

    def test_every_report_type_requires_exactly_one_mapping(self) -> None:
        manifest = fixture_manifest()
        mojo = manifest["mojo"]
        assert isinstance(mojo, dict)
        types = mojo["types"]
        assert isinstance(types, list)
        types[:] = [item for item in types if item["abi_name"] != "PairOut"]
        self.assert_generation_error(manifest, fixture_report(), "entries for: PairOut")

    def test_every_report_function_requires_exactly_one_mapping(self) -> None:
        manifest = fixture_manifest()
        mojo = manifest["mojo"]
        assert isinstance(mojo, dict)
        functions = mojo["functions"]
        assert isinstance(functions, list)
        functions[:] = [
            item for item in functions if item.get("abi_symbol") != PREFIX + "owner_len"
        ]
        self.assert_generation_error(manifest, fixture_report(), "owner_len")

    def test_mapping_owner_and_rust_name_must_match_report(self) -> None:
        manifest = fixture_manifest()
        function_mapping(manifest, "owner_len")["rust_name"] = "size"
        self.assert_generation_error(manifest, fixture_report(), "owner/name does not match")

    def test_static_mapping_cannot_silently_drop_a_self_parameter(self) -> None:
        report = fixture_report()
        maximum = report_function(report, "owner_max_count")
        maximum["params"].insert(
            0,
            parameter(
                "self", opaque_pointer("Owner", mutable=False, owned=False)
            ),
        )
        self.assert_generation_error(
            fixture_manifest(), report, "Static function .* must not take self"
        )

    def test_public_mapping_must_link_a_known_export(self) -> None:
        manifest = fixture_manifest()
        function_mapping(manifest, "owner_len")["export"] = "missing.export"
        self.assert_generation_error(manifest, fixture_report(), "unknown export ids")

    def test_non_skipped_export_must_have_a_mapping(self) -> None:
        manifest = fixture_manifest()
        exports = manifest["exports"]
        assert isinstance(exports, list)
        exports.append(
            {
                "id": "unused.api",
                "rust": "demo_crate::unused",
                "mojo": "unused",
                "status": "DIRECT",
                "reason": "test",
            }
        )
        scope = manifest["scope"]
        assert isinstance(scope, dict)
        audited = scope["audited_exports"]
        assert isinstance(audited, list)
        audited.append("unused.api")
        self.assert_generation_error(manifest, fixture_report(), "unused.api")

    def test_export_mojo_surface_must_match_a_linked_type_or_callable(self) -> None:
        manifest = fixture_manifest()
        export_record(manifest, "owner.len")["mojo"] = "Owner.size"
        self.assert_generation_error(
            manifest,
            fixture_report(),
            "renders public Mojo surface 'Owner.len'.*Owner.size",
        )

        manifest = fixture_manifest()
        export_record(manifest, "record.type")["mojo"] = "DifferentRecord"
        self.assert_generation_error(
            manifest,
            fixture_report(),
            "renders public Mojo surface 'Record'.*DifferentRecord",
        )

    def test_public_mapping_cannot_hide_under_an_unrelated_export(self) -> None:
        manifest = fixture_manifest()
        function_mapping(manifest, "owner_records")["export"] = "owner.type"
        self.assert_generation_error(
            manifest,
            fixture_report(),
            "owner_records.*Owner.records.*owner.type.*Owner",
        )

    def test_iterator_helpers_do_not_replace_the_exported_factory_surface(self) -> None:
        manifest = fixture_manifest()
        factory = function_mapping(manifest, "owner_records")
        factory["kind"] = "skip"
        factory["reason"] = "Removed factory for negative ledger test."
        factory.pop("mojo_name")
        factory.pop("export")
        self.assert_generation_error(
            manifest,
            fixture_report(),
            "RecordIterator.*does not match linked export 'owner.records'",
        )

    def test_skipped_export_cannot_be_linked_publicly(self) -> None:
        manifest = fixture_manifest()
        function_mapping(manifest, "owner_len")["export"] = "helper.skip"
        self.assert_generation_error(manifest, fixture_report(), "SKIPPED export")

    def test_skipped_abi_items_require_a_reason_and_no_export(self) -> None:
        manifest = fixture_manifest()
        skipped = function_mapping(manifest, "internal_helper")
        skipped.pop("reason")
        self.assert_generation_error(manifest, fixture_report(), "requires reason")
        manifest = fixture_manifest()
        function_mapping(manifest, "internal_helper")["export"] = "helper.skip"
        self.assert_generation_error(manifest, fixture_report(), "must not link an export")

    def test_aliases_are_unique_and_have_abi_suffix(self) -> None:
        report = fixture_report()
        report_function(report, "owner_len")["mojo_abi_alias"] = "bad"
        self.assert_generation_error(fixture_manifest(), report, "must end in '_abi'")
        report = fixture_report()
        report_function(report, "owner_len")["mojo_abi_alias"] = "Owner_new_abi"
        self.assert_generation_error(fixture_manifest(), report, "collides")

    def test_symbol_prefix_is_derived_from_binding_id(self) -> None:
        manifest = fixture_manifest()
        binding = manifest["binding"]
        assert isinstance(binding, dict)
        binding["symbol_prefix"] = "rust_mojo__something_else__"
        self.assert_generation_error(
            manifest, fixture_report(), "must be the deterministic prefix"
        )

    def test_unknown_manifest_keys_are_rejected(self) -> None:
        manifest = fixture_manifest()
        function_mapping(manifest, "owner_len")["mystery"] = True
        self.assert_generation_error(manifest, fixture_report(), "unknown keys: mystery")

    def test_concrete_specializations_require_user_confirmation(self) -> None:
        manifest = fixture_manifest()
        manifest["specializations"] = [
            {
                "rust_type": "demo_crate::Generic<T>",
                "mojo_name": "Generic",
                "parameters": {"T": "u32"},
                "exports": ["owner.len"],
                "chosen_by": "agent",
            }
        ]
        self.assert_generation_error(
            manifest, fixture_report(), "concrete generic choices require user confirmation"
        )

    def test_monomorphized_exports_require_a_specialization_record(self) -> None:
        manifest = fixture_manifest()
        exports = manifest["exports"]
        assert isinstance(exports, list)
        next(item for item in exports if item["id"] == "owner.len")[
            "status"
        ] = "MONOMORPHIZED"
        self.assert_generation_error(
            manifest, fixture_report(), "MONOMORPHIZED export requires"
        )

    def test_specialization_requires_explicit_related_export_links(self) -> None:
        manifest = fixture_manifest()
        export_record(manifest, "owner.len")["status"] = "MONOMORPHIZED"
        manifest["specializations"] = [
            {
                "rust_type": "demo_crate::Generic<T>",
                "mojo_name": "Generic",
                "parameters": {"T": "u32"},
                "exports": ["owner.score"],
                "chosen_by": "user",
            }
        ]
        self.assert_generation_error(
            manifest,
            fixture_report(),
            "MONOMORPHIZED export requires.*owner.len",
        )

        manifest = fixture_manifest()
        manifest["specializations"] = [
            {
                "rust_type": "demo_crate::Generic<T>",
                "mojo_name": "Generic",
                "parameters": {"T": "u32"},
                "exports": ["helper.skip"],
                "chosen_by": "user",
            }
        ]
        self.assert_generation_error(
            manifest, fixture_report(), "must not reference SKIPPED export"
        )

    def test_specialization_export_links_are_required_and_unique(self) -> None:
        specialization = {
            "rust_type": "demo_crate::Generic<T>",
            "mojo_name": "Generic",
            "parameters": {"T": "u32"},
            "chosen_by": "user",
        }
        manifest = fixture_manifest()
        manifest["specializations"] = [specialization]
        self.assert_generation_error(
            manifest, fixture_report(), "exports must name at least one export"
        )

        manifest = fixture_manifest()
        specialization = copy.deepcopy(specialization)
        specialization["exports"] = ["owner.len", "owner.len"]
        manifest["specializations"] = [specialization]
        self.assert_generation_error(
            manifest, fixture_report(), "exports must not contain duplicates"
        )

    def test_material_adaptations_require_user_confirmation(self) -> None:
        manifest = fixture_manifest()
        adaptations = manifest["adaptations"]
        assert isinstance(adaptations, list)
        adaptations[0]["chosen_by"] = "agent"
        self.assert_generation_error(
            manifest,
            fixture_report(),
            "material semantic adaptations require user confirmation",
        )

    def test_adaptation_requires_one_exact_adapted_export_link(self) -> None:
        manifest = fixture_manifest()
        adaptation_record(manifest, "owner.score").pop("export")
        self.assert_generation_error(
            manifest, fixture_report(), "export must be a non-empty trimmed string"
        )

        manifest = fixture_manifest()
        adaptation_record(manifest, "owner.score")["export"] = "owner.len"
        self.assert_generation_error(
            manifest, fixture_report(), "must name an ADAPTED export"
        )

        manifest = fixture_manifest()
        adaptation_record(manifest, "owner.score")["export"] = "missing.export"
        self.assert_generation_error(
            manifest, fixture_report(), "references unknown export id"
        )

    def test_adaptation_rust_and_mojo_must_match_its_export(self) -> None:
        manifest = fixture_manifest()
        adaptation_record(manifest, "owner.score")["rust"] = "demo_crate::Other"
        self.assert_generation_error(
            manifest, fixture_report(), "rust must exactly match"
        )

        manifest = fixture_manifest()
        adaptation_record(manifest, "owner.score")["mojo"] = "Owner.other"
        self.assert_generation_error(
            manifest, fixture_report(), "mojo must exactly match"
        )

    def test_every_adapted_export_has_exactly_one_adaptation(self) -> None:
        manifest = fixture_manifest()
        adaptations = manifest["adaptations"]
        assert isinstance(adaptations, list)
        adaptations[:] = [
            item for item in adaptations if item.get("export") != "owner.validate"
        ]
        self.assert_generation_error(
            manifest,
            fixture_report(),
            "ADAPTED export requires exactly one.*owner.validate",
        )

        manifest = fixture_manifest()
        adaptations = manifest["adaptations"]
        assert isinstance(adaptations, list)
        adaptations.append(copy.deepcopy(adaptation_record(manifest, "owner.score")))
        self.assert_generation_error(
            manifest, fixture_report(), "duplicates adaptation link for 'owner.score'"
        )


class DynamicBoundaryValidationTests(unittest.TestCase):
    def assert_generation_error(
        self,
        manifest: dict[str, object],
        report: dict[str, object],
        message: str,
    ) -> None:
        with self.assertRaisesRegex(generator.GenerationError, message):
            generator.generate(manifest, report)

    def test_value_struct_argument_by_value_is_rejected(self) -> None:
        report = fixture_report()
        score = report_function(report, "owner_score_record")
        score["params"][1]["ty"] = struct_type("Record")
        self.assert_generation_error(
            fixture_manifest(), report, "value structs cannot be passed by value"
        )

    def test_value_struct_return_by_value_is_rejected(self) -> None:
        report = fixture_report()
        report_function(report, "owner_len")["output"] = struct_type("Record")
        self.assert_generation_error(
            fixture_manifest(), report, "value structs cannot be returned by value"
        )

    def test_raw_slice_argument_by_value_is_rejected(self) -> None:
        report = fixture_report()
        score = report_function(report, "owner_score_record")
        score["params"][1]["ty"] = {
            "kind": "slice",
            "details": {
                "element": primitive("u_int32"),
                "ownership": "borrowed",
                "mutable": False,
            },
        }
        self.assert_generation_error(
            fixture_manifest(), report, "raw slice carriers cannot be passed by value"
        )

    def test_raw_slice_return_by_value_is_rejected(self) -> None:
        report = fixture_report()
        report_function(report, "owner_len")["output"] = {
            "kind": "slice",
            "details": {
                "element": primitive("u_int8"),
                "ownership": "owned",
                "mutable": True,
            },
        }
        self.assert_generation_error(
            fixture_manifest(), report, "owned u8 returns are not supported"
        )

    def test_mutable_value_pointer_requires_an_explicit_projection(self) -> None:
        report = fixture_report()
        score = report_function(report, "owner_score_record")
        score["params"][1]["ty"]["details"]["mutable"] = True
        refresh_report_hash(report)
        self.assert_generation_error(
            fixture_manifest(), report, "mutable public value references require"
        )

    def test_aggregate_result_status_field_is_rejected(self) -> None:
        manifest = fixture_manifest()
        function_mapping(manifest, "owner_summary")["result"]["status_field"] = "status"
        self.assert_generation_error(
            manifest, fixture_report(), "would require an aggregate return by value"
        )

    def test_out_param_requires_scalar_status_and_complete_result_policy(self) -> None:
        manifest = fixture_manifest()
        function_mapping(manifest, "owner_summary").pop("result")
        self.assert_generation_error(manifest, fixture_report(), "requires a scalar result")
        manifest = fixture_manifest()
        result = function_mapping(manifest, "owner_summary")["result"]
        result["errors"] = {}
        self.assert_generation_error(manifest, fixture_report(), "does not cover status enum")

    def test_scalarized_params_must_name_usize_pairs_once(self) -> None:
        report = fixture_report()
        report_function(report, "owner_new")["params"][0]["ty"] = primitive("u_int64")
        self.assert_generation_error(fixture_manifest(), report, "must be primitive u_int")
        manifest = fixture_manifest()
        adapters = function_mapping(manifest, "owner_new")["scalarized_params"]
        adapters[1]["data_param"] = "records_data"
        self.assert_generation_error(manifest, fixture_report(), "reuses scalarized ABI")

    def test_iterator_policy_must_cover_every_status_without_overlap(self) -> None:
        manifest = fixture_manifest()
        mojo = manifest["mojo"]
        assert isinstance(mojo, dict)
        iterator = next(item for item in mojo["types"] if item["abi_name"] == "RecordIterator")
        iterator["error_discriminants"] = {}
        self.assert_generation_error(manifest, fixture_report(), "does not cover enum")
        manifest = fixture_manifest()
        mojo = manifest["mojo"]
        iterator = next(item for item in mojo["types"] if item["abi_name"] == "RecordIterator")
        iterator["finished_discriminant"] = 0
        self.assert_generation_error(manifest, fixture_report(), "must be distinct")

    def test_iterator_next_cannot_declare_ignored_function_adapters(self) -> None:
        manifest = fixture_manifest()
        function_mapping(manifest, "record_iterator_next")["out_param"] = "out"
        self.assert_generation_error(
            manifest,
            fixture_report(),
            "iterator_next adapters belong on its kind=iterator type mapping",
        )


class HeaderAndCliTests(unittest.TestCase):
    def test_finalization_is_deterministic_and_preserves_only_the_abi_body(self) -> None:
        model = generator.Model(
            fixture_manifest(), fixture_report(), manifest_label="binding.toml"
        )
        first = generator._finalize_ffi_header(model, RAW_FFI)
        second = generator._finalize_ffi_header(model, first)
        self.assertEqual(first, second)
        self.assertIn("# Source crate: demo-crate =1.2.3", first)
        self.assertIn("# ABI backend: diplomat-gen-mojo 0.1.0", first)
        self.assertEqual(first.count("comptime Owner_new_abi"), 1)
        self.assertNotIn("# Generator: diplomat-gen-mojo 0.1.0\n# Diplomat core", first)

    def test_finalization_rejects_missing_alias_provenance(self) -> None:
        tampered = RAW_FFI.replace(
            f'# symbol Owner_len_abi = "{PREFIX}owner_len"',
            '# symbol Owner_len_abi = "wrong"',
        )
        report = fixture_report()
        report["mojo_body_sha256"] = hashlib.sha256(
            tampered.split("\n\n", 1)[1].encode("utf-8")
        ).hexdigest()
        model = generator.Model(
            fixture_manifest(), report, manifest_label="binding.toml"
        )
        with self.assertRaisesRegex(generator.GenerationError, "Owner_len_abi"):
            generator._finalize_ffi_header(model, tampered)

    def test_finalization_rejects_alias_signature_drift(self) -> None:
        tampered = RAW_FFI.replace(
            "comptime Owner_len_abi = def(OwnerRef) thin abi(\"C\") -> UInt",
            "comptime Owner_len_abi = def(OwnerRef) thin abi(\"C\") -> UInt64",
        )
        report = fixture_report()
        report["mojo_body_sha256"] = hashlib.sha256(
            tampered.split("\n\n", 1)[1].encode("utf-8")
        ).hexdigest()
        model = generator.Model(
            fixture_manifest(), report, manifest_label="binding.toml"
        )
        with self.assertRaisesRegex(
            generator.GenerationError, "signature for alias 'Owner_len_abi'"
        ):
            generator._finalize_ffi_header(model, tampered)

    def test_finalization_rejects_any_unreported_abi_body_change(self) -> None:
        model = generator.Model(
            fixture_manifest(), fixture_report(), manifest_label="binding.toml"
        )
        tampered_inputs = {
            "value field layout": RAW_FFI.replace(
                "    var start: UInt\n", "    var start: UInt8\n", 1
            ),
            "enum carrier": RAW_FFI.replace(
                "comptime ReadStatus = Int32", "comptime ReadStatus = UInt8", 1
            ),
            "extra symbol": RAW_FFI
            + '\ncomptime hidden_abi = def() thin abi("C") -> None\n'
            + '# symbol hidden_abi = "rust_mojo__demo_bind__hidden"\n',
        }
        for label, tampered in tampered_inputs.items():
            with self.subTest(label=label), self.assertRaisesRegex(
                generator.GenerationError, "ABI body does not match abi-report.json"
            ):
                generator._finalize_ffi_header(model, tampered)

    def test_finalization_rejects_a_rehashed_report_paired_with_old_raw_abi(self) -> None:
        report = fixture_report()
        structs = report["structs"]
        assert isinstance(structs, list)
        record = next(item for item in structs if item["name"] == "Record")
        record["fields"][0]["ty"] = primitive("u_int8")
        refresh_report_hash(report)
        model = generator.Model(
            fixture_manifest(), report, manifest_label="binding.toml"
        )
        with self.assertRaisesRegex(
            generator.GenerationError, "ABI model digest does not match"
        ):
            generator._finalize_ffi_header(model, RAW_FFI)

    def test_cli_write_check_stale_and_repair_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binding = root / "binding.toml"
            report = root / "abi-report.json"
            raw = root / "raw.mojo"
            output = root / "mojo" / "demo_bind"
            binding.write_text(MANIFEST_TOML, encoding="utf-8")
            report.write_text(
                json.dumps(fixture_report(), indent=2) + "\n", encoding="utf-8"
            )
            raw.write_text(RAW_FFI, encoding="utf-8")
            base = [
                sys.executable,
                str(SCRIPT),
                "--binding",
                str(binding),
                "--report",
                str(report),
                "--output-dir",
                str(output),
            ]
            written = subprocess.run(
                [*base, "--ffi", str(raw)], capture_output=True, text=True, check=False
            )
            self.assertEqual(written.returncode, 0, written.stderr)
            expected = {
                "_ffi.mojo",
                "_runtime.mojo",
                "_types.mojo",
                "_wrappers.mojo",
                "__init__.mojo",
            }
            self.assertEqual({path.name for path in output.iterdir()}, expected)
            before = {path.name: path.read_bytes() for path in output.iterdir()}
            checked = subprocess.run(
                [*base, "--check"], capture_output=True, text=True, check=False
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertEqual(
                before, {path.name: path.read_bytes() for path in output.iterdir()}
            )

            wrappers = output / "_wrappers.mojo"
            wrappers.write_text(wrappers.read_text(encoding="utf-8") + "# stale\n")
            stale = subprocess.run(
                [*base, "--check"], capture_output=True, text=True, check=False
            )
            self.assertEqual(stale.returncode, 2)
            self.assertIn("Generated Mojo package is stale: _wrappers.mojo", stale.stderr)
            repaired = subprocess.run(
                base, capture_output=True, text=True, check=False
            )
            self.assertEqual(repaired.returncode, 0, repaired.stderr)
            self.assertNotIn("# stale", wrappers.read_text(encoding="utf-8"))

            extra = output / "obsolete.mojo"
            extra.write_text("# stale generated module\n", encoding="utf-8")
            for command in ([*base, "--check"], base):
                rejected = subprocess.run(
                    command, capture_output=True, text=True, check=False
                )
                self.assertEqual(rejected.returncode, 2)
                self.assertIn(
                    "unexpected source files: obsolete.mojo", rejected.stderr
                )

    def test_cli_errors_are_concise_and_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binding = root / "binding.toml"
            report = root / "abi-report.json"
            output = root / "output"
            binding.write_text(MANIFEST_TOML, encoding="utf-8")
            broken = fixture_report()
            broken["schema_version"] = 1
            report.write_text(json.dumps(broken), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--binding",
                    str(binding),
                    "--report",
                    str(report),
                    "--output-dir",
                    str(output),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("error: ABI report schema_version must be 3", result.stderr)
            self.assertNotIn("Traceback", result.stderr)


@unittest.skipUnless(os.environ.get("MOJO_BIN"), "set MOJO_BIN to compile generated Mojo")
class MojoCompilationTests(unittest.TestCase):
    def test_backend_escaped_identifiers_compile_as_a_mojo_package(self) -> None:
        mojo = os.environ["MOJO_BIN"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "reserved_abi"
            package.mkdir()
            report = root / "reserved-report.json"
            cargo = REPOSITORY_ROOT / "crates/diplomat-gen-mojo/Cargo.toml"
            fixture = (
                REPOSITORY_ROOT
                / "crates/diplomat-gen-mojo/tests/fixtures/reserved_identifiers.rs"
            )
            generated = subprocess.run(
                [
                    "cargo",
                    "run",
                    "--quiet",
                    "--locked",
                    "--manifest-path",
                    str(cargo),
                    "--",
                    str(fixture),
                    "--output",
                    str(package / "__init__.mojo"),
                    "--report",
                    str(report),
                    "--deny-unsupported",
                ],
                capture_output=True,
                text=True,
                check=False,
                env={
                    **os.environ,
                    "CARGO_TARGET_DIR": str(root / "cargo-target"),
                },
            )
            self.assertEqual(generated.returncode, 0, generated.stdout + generated.stderr)
            raw = (package / "__init__.mojo").read_text(encoding="utf-8")
            self.assertIn(
                "struct TrivialRegisterPassable_(TrivialRegisterPassable):", raw
            )
            self.assertIn("    var read_: UInt8", raw)
            self.assertIn("    var out_: UInt8", raw)
            smoke = root / "reserved_smoke.mojo"
            smoke.write_text(
                textwrap.dedent(
                    '''\
                    from reserved_abi import TrivialRegisterPassable_
                    from std.os import abort


                    def main():
                        var value = TrivialRegisterPassable_(read_=1, out_=2)
                        if value.read_ != 1 or value.out_ != 2:
                            abort()
                    '''
                ),
                encoding="utf-8",
            )
            compiled = subprocess.run(
                [
                    mojo,
                    "build",
                    "--Werror",
                    "-I",
                    str(root),
                    str(smoke),
                    "-o",
                    str(root / "reserved-smoke"),
                ],
                capture_output=True,
                text=True,
                check=False,
                env={
                    **os.environ,
                    "MODULAR_HOME": str(
                        Path(mojo).resolve().parent.parent / "share" / "max"
                    ),
                    "PATH": str(Path(mojo).resolve().parent)
                    + os.pathsep
                    + os.environ.get("PATH", ""),
                },
            )
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)

    def test_generated_package_compiles_with_mojo_1_0(self) -> None:
        mojo = os.environ["MOJO_BIN"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "demo_bind"
            package.mkdir()
            files = generator.generate(fixture_manifest(), fixture_report())
            model = generator.Model(
                fixture_manifest(), fixture_report(), manifest_label="binding.toml"
            )
            files["_ffi.mojo"] = generator._finalize_ffi_header(model, RAW_FFI)
            for name, content in files.items():
                (package / name).write_text(content, encoding="utf-8")
            smoke = root / "smoke.mojo"
            smoke.write_text(
                textwrap.dedent(
                    '''\
                    from demo_bind import (
                        Owner,
                        ReadStatus,
                        ReadStatus_Item,
                        Record,
                        RecordIterator,
                        Summary,
                        parse_owner,
                    )


                    def consume(var iterator: RecordIterator):
                        for record in iterator^:
                            _ = record.start


                    def typecheck_calls(mut owner: Owner, record: Record) raises:
                        _ = owner.len()
                        _ = owner.score_record(record)
                        _ = owner.summary()
                        _ = owner.first()
                        var numbers: List[UInt32] = [1, 2, 3]
                        owner.accept_numbers(numbers, 2)
                        _ = owner.validate()
                        _ = Owner.max_count(1, 2)
                        _ = record.overlaps(record)
                        _ = parse_owner("demo")


                    def main() raises:
                        var record = Record(start=1, value=2, kind=ReadStatus_Item)
                        var summary = Summary(total=3, matched=2)
                        _ = record.start
                        _ = summary.total
                        var invalid = Record(start=1, value=2, kind=ReadStatus(123))
                        try:
                            _ = invalid._to_ffi()
                        except:
                            return
                        raise Error("invalid enum input was accepted")
                    '''
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    mojo,
                    "build",
                    "--Werror",
                    "-I",
                    str(root),
                    str(smoke),
                    "-o",
                    str(root / "smoke"),
                ],
                capture_output=True,
                text=True,
                check=False,
                env={
                    **os.environ,
                    "CONDA_PREFIX": str(Path(mojo).resolve().parent.parent),
                    "MODULAR_HOME": str(
                        Path(mojo).resolve().parent.parent / "share" / "max"
                    ),
                    "PATH": str(Path(mojo).resolve().parent)
                    + os.pathsep
                    + os.environ.get("PATH", ""),
                },
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            executed = subprocess.run(
                [str(root / "smoke")], capture_output=True, text=True, check=False
            )
            self.assertEqual(executed.returncode, 0, executed.stdout + executed.stderr)

    def test_macos_runtime_path_branch_typechecks_for_apple_silicon(self) -> None:
        mojo = os.environ["MOJO_BIN"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "demo_bind"
            package.mkdir()
            manifest = fixture_manifest()
            files = generator.generate(manifest, fixture_report())
            model = generator.Model(
                manifest, fixture_report(), manifest_label="binding.toml"
            )
            files["_ffi.mojo"] = generator._finalize_ffi_header(model, RAW_FFI)
            for name, content in files.items():
                (package / name).write_text(content, encoding="utf-8")
            smoke = root / "macos_runtime.mojo"
            smoke.write_text(
                textwrap.dedent(
                    '''\
                    import demo_bind._runtime as _runtime


                    def main() raises:
                        _ = _runtime._macos_executable_path()
                    '''
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    mojo,
                    "build",
                    "--Werror",
                    "--emit",
                    "object",
                    "--target-triple",
                    "arm64-apple-macosx",
                    "-I",
                    str(root),
                    str(smoke),
                    "-o",
                    str(root / "macos_runtime.o"),
                ],
                capture_output=True,
                text=True,
                check=False,
                env={
                    **os.environ,
                    "CONDA_PREFIX": str(Path(mojo).resolve().parent.parent),
                    "MODULAR_HOME": str(
                        Path(mojo).resolve().parent.parent / "share" / "max"
                    ),
                    "PATH": str(Path(mojo).resolve().parent)
                    + os.pathsep
                    + os.environ.get("PATH", ""),
                },
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_linux_installed_binary_finds_library_by_path_and_symlink(self) -> None:
        if not sys.platform.startswith("linux"):
            self.skipTest("Linux /proc/self/exe runtime regression")
        compiler = shutil.which("cc")
        if compiler is None:
            self.skipTest("a C compiler is required for the runtime regression")
        mojo = os.environ["MOJO_BIN"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "demo_bind"
            package.mkdir()
            manifest = fixture_manifest()
            ffi = manifest["ffi"]
            assert isinstance(ffi, dict)
            ffi["crate_name"] = "runtime_path_probe_ffi"
            files = generator.generate(manifest, fixture_report())
            model = generator.Model(
                manifest, fixture_report(), manifest_label="binding.toml"
            )
            files["_ffi.mojo"] = generator._finalize_ffi_header(model, RAW_FFI)
            for name, content in files.items():
                (package / name).write_text(content, encoding="utf-8")

            prefix = root / "prefix"
            binary_dir = prefix / "bin"
            library_dir = prefix / "lib"
            binary_dir.mkdir(parents=True)
            library_dir.mkdir(parents=True)
            c_source = root / "runtime_probe.c"
            c_source.write_text(
                "int rust_mojo_runtime_path_probe(void) { return 1; }\n",
                encoding="utf-8",
            )
            library = library_dir / "libruntime_path_probe_ffi.so"
            compiled_library = subprocess.run(
                [compiler, "-shared", "-fPIC", str(c_source), "-o", str(library)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                compiled_library.returncode,
                0,
                compiled_library.stdout + compiled_library.stderr,
            )

            smoke = root / "runtime_probe.mojo"
            smoke.write_text(
                textwrap.dedent(
                    '''\
                    import demo_bind._runtime as _runtime


                    def main() raises:
                        var library = _runtime._open_library()
                        print("runtime lookup passed")
                    '''
                ),
                encoding="utf-8",
            )
            executable = binary_dir / "runtime-probe"
            built = subprocess.run(
                [
                    mojo,
                    "build",
                    "--Werror",
                    "-I",
                    str(root),
                    str(smoke),
                    "-o",
                    str(executable),
                ],
                capture_output=True,
                text=True,
                check=False,
                env={
                    **os.environ,
                    "CONDA_PREFIX": str(Path(mojo).resolve().parent.parent),
                    "MODULAR_HOME": str(
                        Path(mojo).resolve().parent.parent / "share" / "max"
                    ),
                    "PATH": str(Path(mojo).resolve().parent)
                    + os.pathsep
                    + os.environ.get("PATH", ""),
                },
            )
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)

            elsewhere = root / "elsewhere"
            elsewhere.mkdir()
            runtime_environment = {"PATH": str(binary_dir) + os.pathsep + "/usr/bin"}
            by_path = subprocess.run(
                ["runtime-probe"],
                cwd=elsewhere,
                env=runtime_environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(by_path.returncode, 0, by_path.stdout + by_path.stderr)
            self.assertIn("runtime lookup passed", by_path.stdout)

            link_dir = root / "links"
            link_dir.mkdir()
            (link_dir / "runtime-probe-link").symlink_to(executable)
            through_symlink = subprocess.run(
                ["runtime-probe-link"],
                cwd=elsewhere,
                env={"PATH": str(link_dir) + os.pathsep + "/usr/bin"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                through_symlink.returncode,
                0,
                through_symlink.stdout + through_symlink.stderr,
            )
            self.assertIn("runtime lookup passed", through_symlink.stdout)


if __name__ == "__main__":
    unittest.main()
