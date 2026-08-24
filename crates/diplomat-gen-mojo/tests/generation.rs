use diplomat_gen_mojo::{
    abi_report_json, generate_from_source, AbiType, MojoPrimitive, SliceOwnership,
    ABI_REPORT_SCHEMA_VERSION,
};
use sha2::{Digest, Sha256};

const BRIDGE: &str = r#"
#[diplomat::bridge]
mod ffi {
    #[diplomat::opaque]
    pub struct Widget(u32);

    pub struct Pair {
        pub left: u32,
        pub right: usize,
    }

    impl Widget {
        pub fn new(seed: u32) -> Box<Self> { unimplemented!() }
        pub fn value(&self, delta: i32) -> u32 { unimplemented!() }
        pub fn visit(input: &[u8]) { unimplemented!() }
        pub fn owned_bytes(input: Box<[u8]>) { unimplemented!() }
        pub fn make_bytes() -> Box<[u8]> { unimplemented!() }
    }
}
"#;

#[test]
fn output_is_deterministic() {
    let first = generate_from_source(BRIDGE).unwrap();
    let second = generate_from_source(BRIDGE).unwrap();
    assert_eq!(first, second);
    assert!(first.mojo.contains("comptime Widget_new_abi"));
    assert!(first.mojo.contains("thin abi(\"C\")"));
    assert!(first.mojo.contains("DiplomatSlice_UInt8"));
    assert!(first.mojo.contains("DiplomatOwnedSlice_UInt8"));
    assert!(first
        .mojo
        .contains("struct DiplomatSlice_UInt8(TrivialRegisterPassable)"));
    assert!(first
        .mojo
        .contains("var data: Pointer[UInt8, ImmUntrackedOrigin]"));
    assert!(first.mojo.contains("comptime diplomat_alloc_abi"));
    assert!(first
        .mojo
        .contains("# symbol diplomat_alloc_abi = \"diplomat_alloc\""));
    assert!(first.mojo.contains("comptime diplomat_free_abi"));
}

#[test]
fn lowers_primitive_struct_and_opaque_methods() {
    let generated = generate_from_source(BRIDGE).unwrap();
    assert!(
        generated.unsupported.is_empty(),
        "{:#?}",
        generated.unsupported
    );
    assert_eq!(generated.module.structs[0].name, "Pair");
    assert_eq!(
        generated.module.structs[0].fields[0].ty,
        AbiType::Primitive(MojoPrimitive::UInt32)
    );
    assert_eq!(generated.module.opaques[0].name, "Widget");
    assert_eq!(generated.module.functions.len(), 5);
}

#[test]
fn reports_unsupported_result_in_output_and_model() {
    let source = r#"
        #[diplomat::bridge]
        mod ffi {
            #[diplomat::opaque]
            pub struct Widget;
            impl Widget {
                pub fn checked() -> Result<u32, u8> { unimplemented!() }
            }
        }
    "#;
    let generated = generate_from_source(source).unwrap();
    assert_eq!(generated.unsupported.len(), 1);
    assert!(generated.unsupported[0].reason.contains("Result"));
    assert!(generated.mojo.contains("# Unsupported HIR items"));
    assert!(generated.mojo.contains("Widget::checked"));
}

#[test]
fn lowers_borrowed_and_mutable_primitive_struct_references() {
    let source = include_str!("fixtures/struct_refs.rs");
    let generated = generate_from_source(source).unwrap();
    assert!(
        generated.unsupported.is_empty(),
        "{:#?}",
        generated.unsupported
    );

    let function = generated
        .module
        .functions
        .iter()
        .find(|function| function.abi_name == "generic_fixture__vector_dot")
        .unwrap();
    assert_eq!(
        function.params[0].ty,
        AbiType::StructPointer {
            name: "Vector2".into(),
            mutable: false,
        }
    );
    assert_eq!(
        function.params[1].ty,
        AbiType::StructPointer {
            name: "Vector2".into(),
            mutable: false,
        }
    );
    assert_eq!(
        function.params[2].ty,
        AbiType::StructPointer {
            name: "FloatOut".into(),
            mutable: true,
        }
    );
    assert!(generated
        .mojo
        .contains("Pointer[Vector2, ImmUntrackedOrigin]"));
    assert!(generated
        .mojo
        .contains("Pointer[FloatOut, MutUntrackedOrigin]"));
}

#[test]
fn self_contained_supported_surface_bridge_reaches_backend_lowering() {
    let generated = generate_from_source(include_str!("fixtures/supported_surface.rs")).unwrap();
    let entry = generated
        .module
        .structs
        .iter()
        .find(|ty| ty.name == "Entry")
        .unwrap();
    assert_eq!(entry.fields[2].ty, AbiType::Enum("ReadStatus".into()));
    assert!(generated
        .module
        .opaques
        .iter()
        .any(|ty| ty.name == "EntryIterator"));
    assert!(generated
        .module
        .enums
        .iter()
        .any(|ty| ty.name == "ReadStatus"));
    for abi_name in [
        "generic_fixture__entry_iterator_new",
        "generic_fixture__entry_iterator_next",
        "generic_fixture__entry_iterator_remaining",
        "generic_fixture__sum_values",
        "generic_fixture__scale_values",
        "generic_fixture__make_bytes",
        "generic_fixture__consume_bytes",
    ] {
        assert!(
            generated
                .module
                .functions
                .iter()
                .any(|function| function.abi_name == abi_name),
            "missing {abi_name}"
        );
    }
    assert_eq!(generated.module.functions.len(), 7);
    let constructor = generated
        .module
        .functions
        .iter()
        .find(|function| function.abi_name == "generic_fixture__entry_iterator_new")
        .unwrap();
    assert_eq!(
        constructor.params[0].ty,
        AbiType::Slice {
            element: Box::new(AbiType::Primitive(MojoPrimitive::UInt32)),
            ownership: SliceOwnership::Borrowed,
            mutable: false,
        }
    );
    assert_eq!(
        constructor.output,
        AbiType::OpaquePointer {
            name: "EntryIterator".into(),
            mutable: true,
            owned: true,
            optional: false,
        }
    );
    let next = generated
        .module
        .functions
        .iter()
        .find(|function| function.abi_name == "generic_fixture__entry_iterator_next")
        .unwrap();
    assert_eq!(
        next.params,
        [
            diplomat_gen_mojo::AbiParam {
                name: "self".into(),
                ty: AbiType::OpaquePointer {
                    name: "EntryIterator".into(),
                    mutable: true,
                    owned: false,
                    optional: false,
                },
            },
            diplomat_gen_mojo::AbiParam {
                name: "out".into(),
                ty: AbiType::StructPointer {
                    name: "Entry".into(),
                    mutable: true,
                },
            },
        ]
    );
    assert_eq!(next.output, AbiType::Enum("ReadStatus".into()));
    let scale = generated
        .module
        .functions
        .iter()
        .find(|function| function.abi_name == "generic_fixture__scale_values")
        .unwrap();
    assert!(matches!(
        scale.params[0].ty,
        AbiType::Slice {
            ownership: SliceOwnership::Borrowed,
            mutable: true,
            ..
        }
    ));
    assert!(generated.mojo.contains(
        "struct DiplomatSliceMut_UInt32(TrivialRegisterPassable):\n    var data: Pointer[UInt32, MutUntrackedOrigin]"
    ));
    let make_bytes = generated
        .module
        .functions
        .iter()
        .find(|function| function.abi_name == "generic_fixture__make_bytes")
        .unwrap();
    assert!(matches!(
        make_bytes.output,
        AbiType::Slice {
            ownership: SliceOwnership::Owned,
            mutable: true,
            ..
        }
    ));
    assert!(generated.mojo.contains("comptime diplomat_alloc_abi"));
    assert!(generated
        .mojo
        .contains("# symbol diplomat_free_abi = \"diplomat_free\""));
    assert!(
        generated.unsupported.is_empty(),
        "{:#?}",
        generated.unsupported
    );
    assert!(!generated.unsupported.iter().any(|item| {
        item.reason.contains("struct_refs") || item.reason.contains("mut_struct_refs")
    }));
}

#[test]
fn json_report_is_versioned_complete_and_deterministic() {
    let generated = generate_from_source(include_str!("fixtures/supported_surface.rs")).unwrap();
    let first = abi_report_json(&generated).unwrap();
    let second = abi_report_json(&generated).unwrap();
    assert_eq!(first, second);

    let report: serde_json::Value = serde_json::from_str(&first).unwrap();
    assert_eq!(report["schema_version"], ABI_REPORT_SCHEMA_VERSION);
    assert_eq!(report["backend"]["name"], "diplomat-gen-mojo");
    assert_eq!(report["backend"]["version"], env!("CARGO_PKG_VERSION"));
    assert_eq!(report["backend"]["diplomat_core_version"], "0.16.1");
    let (_, mojo_body) = generated.mojo.split_once("\n\n").unwrap();
    assert_eq!(
        report["mojo_body_sha256"],
        format!("{:x}", Sha256::digest(mojo_body.as_bytes()))
    );
    let model_payload = serde_json::json!({
        "structs": report["structs"],
        "opaques": report["opaques"],
        "enums": report["enums"],
        "functions": report["functions"],
        "types": report["types"],
        "unsupported": report["unsupported"],
    });
    assert_eq!(
        report["abi_model_sha256"],
        format!(
            "{:x}",
            Sha256::digest(serde_json::to_vec(&model_payload).unwrap())
        )
    );
    assert!(generated.mojo.starts_with(&format!(
        "# GENERATED FILE — DO NOT EDIT DIRECTLY\n\
         # Generator: diplomat-gen-mojo {}\n\
         # Diplomat core: 0.16.1\n\
         # ABI model SHA-256: {}\n\n",
        env!("CARGO_PKG_VERSION"),
        report["abi_model_sha256"].as_str().unwrap()
    )));
    assert_eq!(report["structs"].as_array().unwrap().len(), 1);
    assert_eq!(report["opaques"].as_array().unwrap().len(), 1);
    assert_eq!(report["enums"].as_array().unwrap().len(), 1);
    assert_eq!(report["functions"].as_array().unwrap().len(), 7);
    assert!(!report["types"].as_array().unwrap().is_empty());
    assert_eq!(report["unsupported"].as_array().unwrap().len(), 0);
    assert!(first.contains("generic_fixture__entry_iterator_next"));
    assert!(first.contains("\"mojo_abi_alias\": \"EntryIterator_next_abi\""));
    assert!(first.contains("\"destructor_mojo_abi_alias\": \"EntryIterator_destroy_abi\""));
    assert!(first.contains("\"kind\": \"slice\""));
    assert!(first.ends_with('\n'));
}

#[test]
fn lowers_fieldless_enums_and_nullable_opaque_pointers() {
    let source = include_str!("fixtures/enum_nullable.rs");
    let generated = generate_from_source(source).unwrap();
    assert_eq!(generated, generate_from_source(source).unwrap());
    let status = generated
        .module
        .enums
        .iter()
        .find(|enumeration| enumeration.name == "Status")
        .unwrap();
    assert_eq!(
        status
            .variants
            .iter()
            .map(|variant| (variant.name.as_str(), variant.discriminant))
            .collect::<Vec<_>>(),
        [("Ok", 0), ("Missing", 7), ("Failed", -3)]
    );

    let constructor = generated
        .module
        .functions
        .iter()
        .find(|function| function.rust_name == "new")
        .unwrap();
    assert_eq!(
        constructor.output,
        AbiType::OpaquePointer {
            name: "Widget".into(),
            mutable: true,
            owned: true,
            optional: true,
        }
    );
    let inspect = generated
        .module
        .functions
        .iter()
        .find(|function| function.rust_name == "inspect")
        .unwrap();
    assert_eq!(
        inspect.params[0].ty,
        AbiType::OpaquePointer {
            name: "Widget".into(),
            mutable: false,
            owned: false,
            optional: true,
        }
    );
    assert_eq!(inspect.output, AbiType::Enum("Status".into()));
    assert!(generated.mojo.contains("comptime Status = Int32"));
    assert!(generated
        .mojo
        .contains("comptime Status_Failed: Status = -3"));
    assert!(generated.mojo.contains("WidgetOptionalHandle"));
    assert!(generated.mojo.contains("WidgetOptionalRef"));
    assert_eq!(generated.unsupported.len(), 1);
    assert!(generated.unsupported[0]
        .reason
        .contains("only nullable opaque pointers"));
}

#[test]
fn payload_enums_remain_explicitly_rejected() {
    let source = r#"
        #[diplomat::bridge]
        mod ffi {
            pub enum Payload {
                Value(u8),
            }
        }
    "#;
    let error = generate_from_source(source).unwrap_err().to_string();
    assert!(error.contains("payload enums are not supported"), "{error}");
}

#[test]
fn standard_library_imports_fail_cleanly_before_hir_lowering() {
    let source = r#"
        #[diplomat::bridge]
        mod ffi {
            use std::cmp::Ordering;

            #[diplomat::opaque]
            pub struct Widget;

            impl Widget {
                pub fn compare(&self) -> Ordering { Ordering::Equal }
            }
        }
    "#;
    let error = generate_from_source(source).unwrap_err().to_string();
    assert!(error.contains("imports from `std`"), "{error}");
    assert!(error.contains("bridge-defined FFI-safe"), "{error}");

    let grouped = source.replace(
        "use std::cmp::Ordering;",
        "use {super::Nothing, core::cmp::Ordering};",
    );
    let error = generate_from_source(&grouped).unwrap_err().to_string();
    assert!(error.contains("imports from `core`"), "{error}");
}

#[test]
fn requires_one_self_contained_inline_bridge() {
    for (source, expected) in [
        ("pub fn ordinary_rust() {}", "no inline #[diplomat::bridge]"),
        (
            "#[diplomat::bridge] mod ffi;",
            "external Diplomat bridge modules are not supported",
        ),
    ] {
        let error = generate_from_source(source).unwrap_err().to_string();
        assert!(error.contains(expected), "{error}");
    }
}

#[test]
fn rejects_function_level_abi_rename_on_free_functions() {
    let source = r#"
        #[diplomat::bridge]
        mod ffi {
            #[diplomat::abi_rename = "rust_mojo__example__value"]
            pub fn value() -> u32 { 42 }
        }
    "#;
    let error = generate_from_source(source).unwrap_err().to_string();
    assert!(
        error.contains("function-level #[diplomat::abi_rename]"),
        "{error}"
    );
    assert!(error.contains("#[diplomat::bridge] module"), "{error}");
    assert!(error.contains("namespaced `{0}` rule"), "{error}");
}

#[test]
fn rejects_output_handles_that_borrow_from_inputs() {
    let source = r#"
        #[diplomat::bridge]
        mod ffi {
            #[diplomat::opaque]
            pub struct Owner(u8);

            #[diplomat::opaque]
            pub struct Dependent<'a>(&'a Owner);

            impl Owner {
                pub fn dependent<'a>(&'a self) -> Box<Dependent<'a>> {
                    unimplemented!()
                }
            }
        }
    "#;
    let generated = generate_from_source(source).unwrap();
    assert!(generated.module.functions.is_empty());
    assert_eq!(generated.unsupported.len(), 1);
    let reason = &generated.unsupported[0].reason;
    assert!(reason.contains("borrows from a method input"), "{reason}");
    assert!(
        reason.contains("genuinely self-owned opaque handle"),
        "{reason}"
    );
}

#[test]
fn rejects_enum_receiver_methods_instead_of_claiming_a_value_abi() {
    let source = r#"
        #[diplomat::bridge]
        mod ffi {
            pub enum Mode {
                A = 0,
                B = 1,
            }

            impl Mode {
                pub fn value(&self) -> u8 { 1 }
            }
        }
    "#;
    let generated = generate_from_source(source).unwrap();
    assert!(generated.module.functions.is_empty());
    assert_eq!(generated.unsupported.len(), 1);
    let reason = &generated.unsupported[0].reason;
    assert!(
        reason.contains("enum receiver methods use a pointer"),
        "{reason}"
    );
    assert!(reason.contains("enum-pointer type"), "{reason}");
}

#[test]
fn rejects_identifiers_that_collide_after_mojo_sanitization() {
    let source = r#"
        #[diplomat::bridge]
        mod ffi {
            pub struct Pointer { pub value: u8 }
            pub struct Pointer_ { pub value: u8 }
        }
    "#;
    let error = generate_from_source(source).unwrap_err().to_string();
    assert!(
        error.contains("generated Mojo identifier `Pointer_` collides"),
        "{error}"
    );
}

#[test]
fn escapes_reserved_mojo_identifiers_deterministically() {
    let source = r#"
        #[diplomat::bridge]
        mod ffi {
            pub struct Pointer { pub value: u8 }
            pub struct TrivialRegisterPassable {
                pub read: u8,
                pub out: u8,
            }
        }
    "#;
    let generated = generate_from_source(source).unwrap();
    assert!(generated
        .mojo
        .contains("struct Pointer_(TrivialRegisterPassable)"));
    assert!(generated
        .mojo
        .contains("struct TrivialRegisterPassable_(TrivialRegisterPassable)"));
    assert!(generated.mojo.contains("    var read_: UInt8"));
    assert!(generated.mojo.contains("    var out_: UInt8"));
}

#[test]
fn rejects_owned_non_byte_slices_without_a_destroy_contract() {
    let source = r#"
        #[diplomat::bridge]
        mod ffi {
            pub fn consume_values(values: Box<[u32]>) { unimplemented!() }
        }
    "#;
    let generated = generate_from_source(source).unwrap();
    assert!(generated.module.functions.is_empty());
    assert_eq!(generated.unsupported.len(), 1);
    assert!(generated
        .unsupported
        .iter()
        .all(|item| item.reason.contains("element-specific destroy function")));
}

#[test]
fn rejects_zero_sized_value_structs_and_their_methods() {
    let source = r#"
        #[diplomat::bridge]
        mod ffi {
            pub struct Marker;
        }
    "#;
    let generated = generate_from_source(source).unwrap();
    assert!(generated.module.structs.is_empty());
    assert!(generated.module.functions.is_empty());
    assert!(generated
        .unsupported
        .iter()
        .any(|item| item.reason.contains("zero-sized structs")));
}
