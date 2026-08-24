//! A small Diplomat HIR to Mojo raw-ABI backend.
//!
//! This crate intentionally models the ABI before it attempts ergonomic Mojo
//! wrappers. Unsupported HIR nodes are reported, never silently discarded.

use diplomat_core::ast::SpanLocation;
use diplomat_core::hir::{
    self, FloatType, Int128Type, IntSizeType, IntType, Mutability, OpaqueOwner, PrimitiveType,
    ReturnType, SelfType, Slice, StructPathLike, SuccessType, Type, TypeContext, TypeDef,
};
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fmt::{self, Write as _};
use std::path::Path;

/// Fully deterministic output of one generation run.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Generation {
    pub module: AbiModule,
    pub mojo: String,
    pub unsupported: Vec<UnsupportedItem>,
}

/// Version of the machine-readable ABI report emitted by this crate.
pub const ABI_REPORT_SCHEMA_VERSION: u32 = 3;
pub const ABI_BACKEND_NAME: &str = "diplomat-gen-mojo";
pub const ABI_BACKEND_VERSION: &str = env!("CARGO_PKG_VERSION");
pub const DIPLOMAT_CORE_VERSION: &str = "0.16.1";

/// Stable, machine-readable view of a lowering run.
///
/// `types` is the sorted, de-duplicated set of ABI types referenced by fields,
/// parameters, and returns. Type definitions remain in their corresponding
/// `structs`, `opaques`, and `enums` collections.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct AbiReport<'a> {
    pub schema_version: u32,
    pub backend: AbiBackend,
    pub abi_model_sha256: String,
    pub mojo_body_sha256: String,
    pub structs: &'a [AbiStruct],
    pub opaques: &'a [AbiOpaque],
    pub enums: &'a [AbiEnum],
    pub functions: &'a [AbiFunction],
    pub types: Vec<AbiType>,
    pub unsupported: &'a [UnsupportedItem],
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
pub struct AbiBackend {
    pub name: &'static str,
    pub version: &'static str,
    pub diplomat_core_version: &'static str,
}

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize)]
pub struct AbiModule {
    pub structs: Vec<AbiStruct>,
    pub opaques: Vec<AbiOpaque>,
    pub enums: Vec<AbiEnum>,
    pub functions: Vec<AbiFunction>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct AbiStruct {
    pub name: String,
    pub fields: Vec<AbiParam>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct AbiOpaque {
    pub name: String,
    pub destructor_abi_name: String,
    /// Exact Mojo function-type alias emitted for the destructor.
    pub destructor_mojo_abi_alias: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct AbiEnum {
    pub name: String,
    pub variants: Vec<AbiEnumVariant>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct AbiEnumVariant {
    pub name: String,
    pub discriminant: i32,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct AbiFunction {
    pub owner: Option<String>,
    pub rust_name: String,
    pub abi_name: String,
    /// Exact Mojo function-type alias emitted for this symbol.
    pub mojo_abi_alias: String,
    pub params: Vec<AbiParam>,
    pub output: AbiType,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct AbiParam {
    pub name: String,
    pub ty: AbiType,
}

#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize)]
#[serde(tag = "kind", content = "details", rename_all = "snake_case")]
pub enum AbiType {
    Unit,
    Primitive(MojoPrimitive),
    Struct(String),
    StructPointer {
        name: String,
        mutable: bool,
    },
    Enum(String),
    OpaquePointer {
        name: String,
        mutable: bool,
        owned: bool,
        optional: bool,
    },
    Slice {
        element: Box<AbiType>,
        ownership: SliceOwnership,
        mutable: bool,
    },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SliceOwnership {
    Borrowed,
    Owned,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum MojoPrimitive {
    Bool,
    Int8,
    UInt8,
    Int16,
    UInt16,
    Int32,
    UInt32,
    Int64,
    UInt64,
    Int,
    UInt,
    Float32,
    Float64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct UnsupportedItem {
    pub item: String,
    pub reason: String,
}

#[derive(Debug)]
pub enum GenerateError {
    Parse(syn::Error),
    Lowering(Vec<String>),
    Io(std::io::Error),
}

impl fmt::Display for GenerateError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Parse(error) => write!(f, "could not parse Rust source: {error}"),
            Self::Lowering(errors) => {
                writeln!(f, "Diplomat HIR lowering failed:")?;
                for error in errors {
                    writeln!(f, "  {error}")?;
                }
                Ok(())
            }
            Self::Io(error) => error.fmt(f),
        }
    }
}

impl std::error::Error for GenerateError {}

impl From<std::io::Error> for GenerateError {
    fn from(value: std::io::Error) -> Self {
        Self::Io(value)
    }
}

/// Generate from an in-memory Rust source file.
pub fn generate_from_source(source: &str) -> Result<Generation, GenerateError> {
    generate(source, &SpanLocation::None)
}

/// Generate from a Rust source file, preserving its path in HIR diagnostics.
pub fn generate_from_file(path: impl AsRef<Path>) -> Result<Generation, GenerateError> {
    let path = path.as_ref();
    let source = std::fs::read_to_string(path)?;
    let location = SpanLocation::FilePath(path.to_string_lossy().into_owned());
    generate(&source, &location)
}

/// Build the versioned machine-readable ABI report for a generation run.
pub fn abi_report(generation: &Generation) -> AbiReport<'_> {
    let types = collect_abi_types(&generation.module);
    let abi_model_sha256 = abi_model_sha256(&generation.module, &generation.unsupported, &types);
    let body = raw_mojo_body(&generation.mojo, &abi_model_sha256);
    AbiReport {
        schema_version: ABI_REPORT_SCHEMA_VERSION,
        backend: AbiBackend {
            name: ABI_BACKEND_NAME,
            version: ABI_BACKEND_VERSION,
            diplomat_core_version: DIPLOMAT_CORE_VERSION,
        },
        abi_model_sha256,
        mojo_body_sha256: format!("{:x}", Sha256::digest(body.as_bytes())),
        structs: &generation.module.structs,
        opaques: &generation.module.opaques,
        enums: &generation.module.enums,
        functions: &generation.module.functions,
        types,
        unsupported: &generation.unsupported,
    }
}

fn abi_model_sha256(
    module: &AbiModule,
    unsupported: &[UnsupportedItem],
    types: &[AbiType],
) -> String {
    let payload = serde_json::json!({
        "structs": &module.structs,
        "opaques": &module.opaques,
        "enums": &module.enums,
        "functions": &module.functions,
        "types": types,
        "unsupported": unsupported,
    });
    let canonical = serde_json::to_vec(&payload)
        .expect("the ABI model contains only infallibly serializable values");
    format!("{:x}", Sha256::digest(canonical))
}

/// Serialize a generation run as deterministic, pretty-printed JSON.
pub fn abi_report_json(generation: &Generation) -> Result<String, serde_json::Error> {
    let mut json = serde_json::to_string_pretty(&abi_report(generation))?;
    json.push('\n');
    Ok(json)
}

fn generate(source: &str, location: &SpanLocation) -> Result<Generation, GenerateError> {
    let parsed = syn::parse_file(source).map_err(GenerateError::Parse)?;
    let source_errors = preflight_unsupported_bridge_shapes(&parsed);
    if !source_errors.is_empty() {
        return Err(GenerateError::Lowering(source_errors));
    }
    let mut validator = hir::BasicAttributeValidator::new("mojo");
    validator.support.constructors = true;
    validator.support.named_constructors = true;
    validator.support.abi_compatibles = true;
    validator.support.struct_refs = true;
    validator.support.mut_struct_refs = true;
    validator.support.free_functions = true;
    validator.support.owned_slices = true;
    validator.support.mutable_slices = true;
    validator.support.opaque_slices = false;
    validator.support.owned_byte_slice_returns = true;

    let tcx = TypeContext::from_syn(&parsed, Default::default(), validator, None, location)
        .map_err(|errors| {
            GenerateError::Lowering(
                errors
                    .into_iter()
                    .map(|(context, error)| format!("{context}: {error}"))
                    .collect(),
            )
        })?;

    let mut builder = Builder {
        tcx: &tcx,
        module: AbiModule::default(),
        unsupported: Vec::new(),
    };
    builder.lower();
    let naming_errors = validate_rendered_identifiers(&builder.module);
    if !naming_errors.is_empty() {
        return Err(GenerateError::Lowering(naming_errors));
    }
    let types = collect_abi_types(&builder.module);
    let abi_model_sha256 = abi_model_sha256(&builder.module, &builder.unsupported, &types);
    let mojo = render(&builder.module, &builder.unsupported, &abi_model_sha256);
    Ok(Generation {
        module: builder.module,
        mojo,
        unsupported: builder.unsupported,
    })
}

/// Diplomat's HIR intentionally cannot represent payload enums. Detect them
/// on the already-parsed `syn` tree so callers receive a stable error instead
/// of depending on the upstream diagnostic renderer's process-global state.
fn preflight_unsupported_bridge_shapes(file: &syn::File) -> Vec<String> {
    let mut errors = Vec::new();
    let mut bridge_count = 0usize;
    for item in &file.items {
        let syn::Item::Mod(module) = item else {
            continue;
        };
        let is_bridge = module
            .attrs
            .iter()
            .any(|attr| is_diplomat_attr(attr, "bridge"));
        if !is_bridge {
            continue;
        }
        bridge_count += 1;
        let Some((_, items)) = &module.content else {
            errors.push(format!(
                "{}: external Diplomat bridge modules are not supported; generate one self-contained inline bridge file",
                module.ident
            ));
            continue;
        };
        for item in items {
            if let syn::Item::Fn(function) = item {
                if function
                    .attrs
                    .iter()
                    .any(|attr| is_diplomat_attr(attr, "abi_rename"))
                {
                    errors.push(format!(
                        "{}::{}: function-level #[diplomat::abi_rename] on a free function is not supported by pinned Diplomat 0.16.1 macros; put the namespaced `{{0}}` rule on the #[diplomat::bridge] module",
                        module.ident, function.sig.ident
                    ));
                }
            }
            if let syn::Item::Use(import) = item {
                if let Some(first_segment) = unsupported_bridge_import(&import.tree) {
                    errors.push(format!(
                        "{}: imports from `{}` are not Diplomat bridge types; project the value into a bridge-defined FFI-safe scalar, struct, or enum",
                        module.ident,
                        first_segment
                    ));
                }
            }
            let syn::Item::Enum(enumeration) = item else {
                continue;
            };
            for variant in &enumeration.variants {
                if !matches!(variant.fields, syn::Fields::Unit) {
                    errors.push(format!(
                        "{}::{}: payload enums are not supported by Diplomat HIR",
                        enumeration.ident, variant.ident
                    ));
                }
            }
        }
    }
    if bridge_count == 0 {
        errors.push("no inline #[diplomat::bridge] module was found in the input file".into());
    }
    errors
}

fn is_diplomat_attr(attr: &syn::Attribute, name: &str) -> bool {
    let mut segments = attr.path().segments.iter();
    matches!(
        (segments.next(), segments.next(), segments.next()),
        (Some(first), Some(second), None)
            if first.ident == "diplomat" && second.ident == name
    )
}

fn unsupported_bridge_import(tree: &syn::UseTree) -> Option<&'static str> {
    match tree {
        syn::UseTree::Path(path) if path.ident == "std" => Some("std"),
        syn::UseTree::Path(path) if path.ident == "core" => Some("core"),
        syn::UseTree::Name(name) if name.ident == "std" => Some("std"),
        syn::UseTree::Name(name) if name.ident == "core" => Some("core"),
        syn::UseTree::Rename(rename) if rename.ident == "std" => Some("std"),
        syn::UseTree::Rename(rename) if rename.ident == "core" => Some("core"),
        syn::UseTree::Group(group) => group.items.iter().find_map(unsupported_bridge_import),
        syn::UseTree::Path(_)
        | syn::UseTree::Name(_)
        | syn::UseTree::Rename(_)
        | syn::UseTree::Glob(_) => None,
    }
}

struct Builder<'a> {
    tcx: &'a TypeContext,
    module: AbiModule,
    unsupported: Vec<UnsupportedItem>,
}

impl Builder<'_> {
    fn lower(&mut self) {
        for (_, def) in self.tcx.all_types() {
            let owner = def.name().to_string();
            match def {
                TypeDef::Struct(def) => self.lower_struct(&owner, &def.fields),
                TypeDef::OutStruct(def) => self.lower_struct(&owner, &def.fields),
                TypeDef::Opaque(def) => self.module.opaques.push(AbiOpaque {
                    name: owner.clone(),
                    destructor_abi_name: def.dtor_abi_name.to_string(),
                    destructor_mojo_abi_alias: format!("{}_destroy_abi", mojo_ident(&owner)),
                }),
                TypeDef::Enum(def) => self.lower_enum(&owner, def),
                _ => self.unsupported.push(UnsupportedItem {
                    item: owner.clone(),
                    reason: "unknown future Diplomat type definition".into(),
                }),
            }
            for method in def.methods() {
                self.lower_method(Some(&owner), method);
            }
        }
        for (_, function) in self.tcx.all_free_functions() {
            self.lower_method(None, function);
        }
    }

    fn lower_enum(&mut self, name: &str, def: &hir::EnumDef) {
        let mut variants = Vec::with_capacity(def.variants.len());
        for variant in &def.variants {
            let Ok(discriminant) = i32::try_from(variant.discriminant) else {
                self.unsupported.push(UnsupportedItem {
                    item: format!("{name}::{}", variant.name),
                    reason: "enum discriminant does not fit Diplomat's 32-bit C enum ABI".into(),
                });
                return;
            };
            variants.push(AbiEnumVariant {
                name: variant.name.to_string(),
                discriminant,
            });
        }
        self.module.enums.push(AbiEnum {
            name: name.into(),
            variants,
        });
    }

    fn lower_struct<P: hir::TyPosition>(&mut self, name: &str, fields: &[hir::StructField<P>]) {
        if fields.is_empty() {
            self.unsupported.push(UnsupportedItem {
                item: name.into(),
                reason: "zero-sized structs do not have a portable C value layout; project this type to unit or an opaque handle"
                    .into(),
            });
            return;
        }
        let mut lowered = Vec::with_capacity(fields.len());
        let mut complete = true;
        for field in fields {
            match self.lower_value_struct_field(&field.ty) {
                Ok(ty) => {
                    lowered.push(AbiParam {
                        name: field.name.to_string(),
                        ty,
                    });
                }
                Err(reason) => {
                    complete = false;
                    self.unsupported.push(UnsupportedItem {
                        item: format!("{name}.{}", field.name),
                        reason,
                    });
                }
            }
        }
        if complete {
            self.module.structs.push(AbiStruct {
                name: name.into(),
                fields: lowered,
            });
        }
    }

    fn lower_method(&mut self, owner: Option<&str>, method: &hir::Method) {
        let display_name = match owner {
            Some(owner) => format!("{owner}::{}", method.name),
            None => method.name.to_string(),
        };
        if self.output_borrows_inputs(method) {
            self.unsupported.push(UnsupportedItem {
                item: display_name,
                reason: "return value borrows from a method input, but ABI report schema 3 cannot preserve Diplomat lifetime edges; project it to a genuinely self-owned opaque handle (for example, retain an iterator owner and guard inside that handle)"
                    .into(),
            });
            return;
        }
        let mut params = Vec::new();
        if let Some(param_self) = &method.param_self {
            match self.lower_self(&param_self.ty) {
                Ok(ty) => params.push(AbiParam {
                    name: "self".into(),
                    ty,
                }),
                Err(reason) => {
                    self.unsupported.push(UnsupportedItem {
                        item: display_name,
                        reason,
                    });
                    return;
                }
            }
        }
        for param in &method.params {
            match self.lower_type(&param.ty) {
                Ok(ty) => params.push(AbiParam {
                    name: param.name.to_string(),
                    ty,
                }),
                Err(reason) => {
                    self.unsupported.push(UnsupportedItem {
                        item: display_name,
                        reason: format!("parameter `{}`: {reason}", param.name),
                    });
                    return;
                }
            }
        }
        let output = match &method.output {
            ReturnType::Infallible(success) => match self.lower_success(success) {
                Ok(ty) => ty,
                Err(reason) => {
                    self.unsupported.push(UnsupportedItem {
                        item: display_name,
                        reason: format!("return type: {reason}"),
                    });
                    return;
                }
            },
            ReturnType::Fallible(_, _) => {
                self.unsupported.push(UnsupportedItem {
                    item: display_name,
                    reason: "Result returns are not implemented by the initial Mojo backend".into(),
                });
                return;
            }
            ReturnType::Nullable(success) => match self.lower_nullable_success(success) {
                Ok(ty) => ty,
                Err(reason) => {
                    self.unsupported.push(UnsupportedItem {
                        item: display_name,
                        reason: format!("nullable return type: {reason}"),
                    });
                    return;
                }
            },
        };
        let owner = owner.map(str::to_owned);
        let rust_name = method.name.to_string();
        self.module.functions.push(AbiFunction {
            mojo_abi_alias: function_alias_from_parts(owner.as_deref(), &rust_name),
            owner,
            rust_name,
            abi_name: method.abi_name.to_string(),
            params,
            output,
        });
    }

    fn output_borrows_inputs(&self, method: &hir::Method) -> bool {
        let mut visitor = method.borrowing_param_visitor(self.tcx, false);
        if let Some(param_self) = &method.param_self {
            visitor.visit_param(&param_self.ty.clone().into(), "self");
        }
        for param in &method.params {
            visitor.visit_param(&param.ty, param.name.as_str());
        }
        visitor
            .borrow_map()
            .values()
            .any(|lifetime| !lifetime.incoming_edges.is_empty())
    }

    fn lower_success(&self, success: &SuccessType) -> Result<AbiType, String> {
        match success {
            SuccessType::Unit => Ok(AbiType::Unit),
            SuccessType::OutType(ty) => self.lower_type(ty),
            SuccessType::Write => Err("DiplomatWrite/string returns are not implemented".into()),
            _ => Err("unknown future Diplomat success type".into()),
        }
    }

    fn lower_nullable_success(&self, success: &SuccessType) -> Result<AbiType, String> {
        match success {
            SuccessType::OutType(Type::Opaque(path)) => Ok(AbiType::OpaquePointer {
                name: self.tcx.resolve_type(path.id()).name().to_string(),
                mutable: path.owner.is_owned()
                    || path.owner.mutability() == Mutability::Mutable,
                owned: path.owner.is_owned(),
                optional: true,
            }),
            SuccessType::OutType(_) => Err(
                "only nullable opaque pointers have a direct pointer ABI; primitive, enum, and struct options require an option carrier"
                    .into(),
            ),
            SuccessType::Unit | SuccessType::Write => {
                Err("unit and writeable values cannot use the nullable pointer ABI".into())
            }
            _ => Err("unknown future nullable Diplomat success type".into()),
        }
    }

    fn lower_self(&self, ty: &SelfType) -> Result<AbiType, String> {
        match ty {
            SelfType::Opaque(path) => Ok(AbiType::OpaquePointer {
                name: self.tcx.resolve_type(path.id()).name().to_string(),
                mutable: path.owner.mutability == Mutability::Mutable,
                owned: false,
                optional: false,
            }),
            SelfType::Struct(path) => {
                let id = path.id();
                if !self.is_primitive_struct(id, &mut BTreeSet::new()) {
                    return Err("self struct is not ABI-compatible".into());
                }
                let name = self.tcx.resolve_type(id).name().to_string();
                if path.owner().is_owned() {
                    Ok(AbiType::Struct(name))
                } else {
                    Ok(AbiType::StructPointer {
                        name,
                        mutable: path.owner().mutability() == Mutability::Mutable,
                    })
                }
            }
            SelfType::Enum(_) => Err(
                "enum receiver methods use a pointer receiver in Diplomat 0.16.1, which ABI report schema 3 does not model; project the operation to a free function or extend the backend with an enum-pointer type"
                    .into(),
            ),
            _ => Err("unknown future Diplomat self type".into()),
        }
    }

    fn lower_type<P: hir::TyPosition>(&self, ty: &Type<P>) -> Result<AbiType, String> {
        match ty {
            Type::Primitive(primitive) => primitive_to_mojo(*primitive).map(AbiType::Primitive),
            Type::Struct(path) => {
                let id = path.id();
                if !self.is_primitive_struct(id, &mut BTreeSet::new()) {
                    return Err(format!(
                        "struct `{}` is not composed solely of supported ABI value fields",
                        self.tcx.resolve_type(id).name()
                    ));
                }
                let name = self.tcx.resolve_type(id).name().to_string();
                if path.owner().is_owned() {
                    Ok(AbiType::Struct(name))
                } else {
                    Ok(AbiType::StructPointer {
                        name,
                        mutable: path.owner().mutability() == Mutability::Mutable,
                    })
                }
            }
            Type::Opaque(path) => Ok(AbiType::OpaquePointer {
                name: self.tcx.resolve_type(path.id()).name().to_string(),
                mutable: path.owner.is_owned()
                    || path.owner.mutability() == Some(Mutability::Mutable),
                owned: path.owner.is_owned(),
                optional: path.is_optional(),
            }),
            Type::Slice(slice) => self.lower_slice(slice),
            Type::DiplomatOption(_) => Err("DiplomatOption is not implemented".into()),
            Type::Enum(path) => self.lower_enum_type(path.tcx_id),
            Type::Callback(_) => Err("callbacks are not implemented".into()),
            Type::ImplTrait(_) => Err("trait/iterator values are not implemented".into()),
            _ => Err("unknown future Diplomat HIR type".into()),
        }
    }

    fn lower_slice<P: hir::TyPosition>(&self, slice: &Slice<P>) -> Result<AbiType, String> {
        let (element, owner) = match slice {
            Slice::Primitive(owner, primitive) => {
                (AbiType::Primitive(primitive_to_mojo(*primitive)?), *owner)
            }
            Slice::Struct(owner, path) => (
                {
                    let id = path.id();
                    if !self.is_primitive_struct(id, &mut BTreeSet::new()) {
                        return Err(format!(
                            "slice element struct `{}` is not ABI-compatible",
                            self.tcx.resolve_type(id).name()
                        ));
                    }
                    AbiType::Struct(self.tcx.resolve_type(id).name().to_string())
                },
                *owner,
            ),
            Slice::Opaque(_, _) => {
                return Err("slices of opaque pointers are not implemented".into())
            }
            Slice::Str(_, _) | Slice::Strs(_) => {
                return Err("string slices are not implemented".into())
            }
            _ => return Err("unknown future Diplomat slice type".into()),
        };
        if owner.is_owned() && element != AbiType::Primitive(MojoPrimitive::UInt8) {
            return Err(
                "owned slices other than u8 require a generated element-specific destroy function; Diplomat 0.16 only exports the portable diplomat_owned_slice_u8_destroy helper"
                    .into(),
            );
        }
        Ok(AbiType::Slice {
            element: Box::new(element),
            ownership: if owner.is_owned() {
                SliceOwnership::Owned
            } else {
                SliceOwnership::Borrowed
            },
            mutable: owner.mutability() == Mutability::Mutable,
        })
    }

    fn lower_value_struct_field<P: hir::TyPosition>(
        &self,
        ty: &Type<P>,
    ) -> Result<AbiType, String> {
        match ty {
            Type::Primitive(primitive) => primitive_to_mojo(*primitive).map(AbiType::Primitive),
            Type::Struct(path) => {
                let id = path.id();
                if path.owner().is_owned() && self.is_primitive_struct(id, &mut BTreeSet::new()) {
                    Ok(AbiType::Struct(
                        self.tcx.resolve_type(id).name().to_string(),
                    ))
                } else {
                    Err("nested struct is not composed solely of supported value fields".into())
                }
            }
            Type::Enum(path) => self.lower_enum_type(path.tcx_id),
            _ => Err(
                "only primitives, fieldless enums, and nested primitive-struct fields are ABI-modeled"
                    .into(),
            ),
        }
    }

    fn is_primitive_struct(&self, id: hir::TypeId, visiting: &mut BTreeSet<hir::TypeId>) -> bool {
        if !visiting.insert(id) {
            return false;
        }
        let result = match self.tcx.resolve_type(id) {
            TypeDef::Struct(def) => {
                !def.fields.is_empty()
                    && def
                        .fields
                        .iter()
                        .all(|field| self.is_value_field_type(&field.ty, visiting))
            }
            TypeDef::OutStruct(def) => {
                !def.fields.is_empty()
                    && def
                        .fields
                        .iter()
                        .all(|field| self.is_value_field_type(&field.ty, visiting))
            }
            _ => false,
        };
        visiting.remove(&id);
        result
    }

    fn is_value_field_type<P: hir::TyPosition>(
        &self,
        ty: &Type<P>,
        visiting: &mut BTreeSet<hir::TypeId>,
    ) -> bool {
        match ty {
            Type::Primitive(primitive) => primitive_to_mojo(*primitive).is_ok(),
            Type::Struct(path) => {
                path.owner().is_owned() && self.is_primitive_struct(path.id(), visiting)
            }
            Type::Enum(path) => self.lower_enum_type(path.tcx_id).is_ok(),
            _ => false,
        }
    }

    fn lower_enum_type(&self, id: hir::EnumId) -> Result<AbiType, String> {
        let def = self.tcx.resolve_enum(id);
        if def
            .variants
            .iter()
            .any(|variant| i32::try_from(variant.discriminant).is_err())
        {
            return Err(format!(
                "enum `{}` has a discriminant outside the supported 32-bit C enum range",
                def.name
            ));
        }
        Ok(AbiType::Enum(def.name.to_string()))
    }
}

fn primitive_to_mojo(primitive: PrimitiveType) -> Result<MojoPrimitive, String> {
    Ok(match primitive {
        PrimitiveType::Bool => MojoPrimitive::Bool,
        PrimitiveType::Char => MojoPrimitive::UInt32,
        PrimitiveType::Byte | PrimitiveType::Int(IntType::U8) => MojoPrimitive::UInt8,
        PrimitiveType::Ordering | PrimitiveType::Int(IntType::I8) => MojoPrimitive::Int8,
        PrimitiveType::Int(IntType::I16) => MojoPrimitive::Int16,
        PrimitiveType::Int(IntType::U16) => MojoPrimitive::UInt16,
        PrimitiveType::Int(IntType::I32) => MojoPrimitive::Int32,
        PrimitiveType::Int(IntType::U32) => MojoPrimitive::UInt32,
        PrimitiveType::Int(IntType::I64) => MojoPrimitive::Int64,
        PrimitiveType::Int(IntType::U64) => MojoPrimitive::UInt64,
        PrimitiveType::IntSize(IntSizeType::Isize) => MojoPrimitive::Int,
        PrimitiveType::IntSize(IntSizeType::Usize) => MojoPrimitive::UInt,
        PrimitiveType::Float(FloatType::F32) => MojoPrimitive::Float32,
        PrimitiveType::Float(FloatType::F64) => MojoPrimitive::Float64,
        PrimitiveType::Int128(Int128Type::I128 | Int128Type::U128) => {
            return Err("128-bit integers are not C-ABI portable in Diplomat".into())
        }
    })
}

fn render(module: &AbiModule, unsupported: &[UnsupportedItem], abi_model_sha256: &str) -> String {
    let mut out = raw_mojo_header(abi_model_sha256);
    let slice_types = collect_slice_types(module);
    for (name, (element, ownership, mutable)) in &slice_types {
        writeln!(out, "@fieldwise_init").unwrap();
        writeln!(out, "struct {name}(TrivialRegisterPassable):").unwrap();
        let origin = if *mutable || *ownership == SliceOwnership::Owned {
            "MutUntrackedOrigin"
        } else {
            "ImmUntrackedOrigin"
        };
        writeln!(
            out,
            "    var data: Pointer[{}, {origin}]",
            render_type(element),
        )
        .unwrap();
        writeln!(out, "    var len: UInt").unwrap();
        if *ownership == SliceOwnership::Owned {
            writeln!(
                out,
                "    # Rust owns this allocation; it must be destroyed exactly once."
            )
            .unwrap();
        }
        writeln!(out).unwrap();
    }

    for enumeration in &module.enums {
        writeln!(out, "comptime {} = Int32", mojo_ident(&enumeration.name)).unwrap();
        for variant in &enumeration.variants {
            writeln!(
                out,
                "comptime {}_{}: {} = {}",
                mojo_ident(&enumeration.name),
                mojo_ident(&variant.name),
                mojo_ident(&enumeration.name),
                variant.discriminant
            )
            .unwrap();
        }
        writeln!(out).unwrap();
    }
    if module.functions.iter().any(|function| {
        matches!(
            &function.output,
            AbiType::Slice {
                element,
                ownership: SliceOwnership::Owned,
                ..
            } if **element == AbiType::Primitive(MojoPrimitive::UInt8)
        )
    }) {
        writeln!(
            out,
            "comptime diplomat_owned_slice_u8_destroy_abi = def(Pointer[UInt8, MutUntrackedOrigin], UInt) thin abi(\"C\") -> None"
        )
        .unwrap();
        writeln!(
            out,
            "# symbol diplomat_owned_slice_u8_destroy_abi = \"diplomat_owned_slice_u8_destroy\""
        )
        .unwrap();
        writeln!(out).unwrap();
    }
    if has_owned_slice_input(module) {
        writeln!(
            out,
            "comptime diplomat_alloc_abi = def(UInt, UInt) thin abi(\"C\") -> Pointer[UInt8, MutUntrackedOrigin]"
        )
        .unwrap();
        writeln!(out, "# symbol diplomat_alloc_abi = \"diplomat_alloc\"").unwrap();
        writeln!(
            out,
            "comptime diplomat_free_abi = def(Pointer[UInt8, MutUntrackedOrigin], UInt, UInt) thin abi(\"C\") -> None"
        )
        .unwrap();
        writeln!(out, "# symbol diplomat_free_abi = \"diplomat_free\"").unwrap();
        writeln!(out).unwrap();
    }

    for structure in &module.structs {
        writeln!(out, "@fieldwise_init").unwrap();
        writeln!(
            out,
            "struct {}(TrivialRegisterPassable):",
            mojo_ident(&structure.name)
        )
        .unwrap();
        if structure.fields.is_empty() {
            writeln!(out, "    pass").unwrap();
        } else {
            for field in &structure.fields {
                writeln!(
                    out,
                    "    var {}: {}",
                    mojo_ident(&field.name),
                    render_type(&field.ty)
                )
                .unwrap();
            }
        }
        writeln!(out).unwrap();
    }

    for opaque in &module.opaques {
        writeln!(
            out,
            "comptime {}Handle = Pointer[UInt8, MutUntrackedOrigin]",
            mojo_ident(&opaque.name)
        )
        .unwrap();
        writeln!(
            out,
            "comptime {}Ref = Pointer[UInt8, ImmUntrackedOrigin]",
            mojo_ident(&opaque.name)
        )
        .unwrap();
        writeln!(
            out,
            "comptime {}MutRef = Pointer[UInt8, MutUntrackedOrigin]",
            mojo_ident(&opaque.name)
        )
        .unwrap();
        writeln!(
            out,
            "comptime {}OptionalHandle = OptionalPointer[UInt8, MutUntrackedOrigin]",
            mojo_ident(&opaque.name)
        )
        .unwrap();
        writeln!(
            out,
            "comptime {}OptionalRef = OptionalPointer[UInt8, ImmUntrackedOrigin]",
            mojo_ident(&opaque.name)
        )
        .unwrap();
        writeln!(
            out,
            "comptime {}OptionalMutRef = OptionalPointer[UInt8, MutUntrackedOrigin]",
            mojo_ident(&opaque.name)
        )
        .unwrap();
        writeln!(
            out,
            "comptime {}_destroy_abi = def({}Handle) thin abi(\"C\") -> None",
            mojo_ident(&opaque.name),
            mojo_ident(&opaque.name)
        )
        .unwrap();
        writeln!(
            out,
            "# symbol {}_destroy_abi = {:?}",
            mojo_ident(&opaque.name),
            opaque.destructor_abi_name
        )
        .unwrap();
        writeln!(out).unwrap();
    }

    for function in &module.functions {
        let alias = function_alias(function);
        let params = function
            .params
            .iter()
            .map(|param| render_type(&param.ty))
            .collect::<Vec<_>>()
            .join(", ");
        writeln!(
            out,
            "comptime {alias} = def({params}) thin abi(\"C\") -> {}",
            render_type(&function.output)
        )
        .unwrap();
        writeln!(out, "# symbol {alias} = {:?}", function.abi_name).unwrap();
    }

    if !unsupported.is_empty() {
        writeln!(out).unwrap();
        writeln!(out, "# Unsupported HIR items (generation was partial):").unwrap();
        for item in unsupported {
            writeln!(out, "# - {}: {}", item.item, item.reason).unwrap();
        }
    }
    out
}

fn raw_mojo_header(abi_model_sha256: &str) -> String {
    format!(
        "# GENERATED FILE — DO NOT EDIT DIRECTLY\n# Generator: {ABI_BACKEND_NAME} {ABI_BACKEND_VERSION}\n# Diplomat core: {DIPLOMAT_CORE_VERSION}\n# ABI model SHA-256: {abi_model_sha256}\n\n"
    )
}

fn raw_mojo_body<'a>(mojo: &'a str, abi_model_sha256: &str) -> &'a str {
    let header = raw_mojo_header(abi_model_sha256);
    mojo.strip_prefix(&header)
        .expect("the renderer always emits its canonical raw Mojo header")
}

fn function_alias(function: &AbiFunction) -> String {
    function.mojo_abi_alias.clone()
}

fn function_alias_from_parts(owner: Option<&str>, rust_name: &str) -> String {
    match owner {
        Some(owner) => format!("{}_{}_abi", mojo_ident(owner), mojo_ident(rust_name)),
        None => format!("{}_abi", mojo_ident(rust_name)),
    }
}

fn validate_rendered_identifiers(module: &AbiModule) -> Vec<String> {
    let mut errors = Vec::new();
    let mut top_level = BTreeMap::<String, String>::new();

    for name in collect_slice_types(module).into_keys() {
        record_rendered_identifier(
            &mut top_level,
            &mut errors,
            name.clone(),
            format!("slice carrier `{name}`"),
        );
    }
    if module.functions.iter().any(|function| {
        matches!(
            &function.output,
            AbiType::Slice {
                element,
                ownership: SliceOwnership::Owned,
                ..
            } if **element == AbiType::Primitive(MojoPrimitive::UInt8)
        )
    }) {
        record_rendered_identifier(
            &mut top_level,
            &mut errors,
            "diplomat_owned_slice_u8_destroy_abi".into(),
            "Diplomat owned-byte-slice destructor".into(),
        );
    }
    if has_owned_slice_input(module) {
        for (name, origin) in [
            ("diplomat_alloc_abi", "Diplomat Rust allocator"),
            ("diplomat_free_abi", "Diplomat Rust deallocator"),
        ] {
            record_rendered_identifier(&mut top_level, &mut errors, name.into(), origin.into());
        }
    }

    for enumeration in &module.enums {
        let enum_name = mojo_ident(&enumeration.name);
        record_rendered_identifier(
            &mut top_level,
            &mut errors,
            enum_name.clone(),
            format!("enum `{}`", enumeration.name),
        );
        for variant in &enumeration.variants {
            let name = format!("{enum_name}_{}", mojo_ident(&variant.name));
            record_rendered_identifier(
                &mut top_level,
                &mut errors,
                name,
                format!("enum variant `{}::{}`", enumeration.name, variant.name),
            );
        }
    }
    for structure in &module.structs {
        record_rendered_identifier(
            &mut top_level,
            &mut errors,
            mojo_ident(&structure.name),
            format!("struct `{}`", structure.name),
        );
        let mut fields = BTreeMap::<String, String>::new();
        for field in &structure.fields {
            let name = mojo_ident(&field.name);
            if let Some(previous) = fields.insert(name.clone(), field.name.clone()) {
                errors.push(format!(
                    "struct `{}` fields `{previous}` and `{}` both render as `{name}`",
                    structure.name, field.name
                ));
            }
        }
    }
    for opaque in &module.opaques {
        let base = mojo_ident(&opaque.name);
        for suffix in [
            "Handle",
            "Ref",
            "MutRef",
            "OptionalHandle",
            "OptionalRef",
            "OptionalMutRef",
            "_destroy_abi",
        ] {
            record_rendered_identifier(
                &mut top_level,
                &mut errors,
                format!("{base}{suffix}"),
                format!("opaque `{}` generated `{suffix}` name", opaque.name),
            );
        }
    }
    for function in &module.functions {
        record_rendered_identifier(
            &mut top_level,
            &mut errors,
            function_alias(function),
            format!("function ABI alias `{}`", function.abi_name),
        );
    }
    errors
}

fn record_rendered_identifier(
    seen: &mut BTreeMap<String, String>,
    errors: &mut Vec<String>,
    name: String,
    origin: String,
) {
    if let Some(previous) = seen.insert(name.clone(), origin.clone()) {
        errors.push(format!(
            "generated Mojo identifier `{name}` collides between {previous} and {origin}"
        ));
    }
}

fn collect_slice_types(module: &AbiModule) -> BTreeMap<String, (AbiType, SliceOwnership, bool)> {
    let mut found = BTreeMap::new();
    for function in &module.functions {
        for param in &function.params {
            collect_slice_type(&param.ty, &mut found);
        }
        collect_slice_type(&function.output, &mut found);
    }
    found
}

fn has_owned_slice_input(module: &AbiModule) -> bool {
    module.functions.iter().any(|function| {
        function.params.iter().any(|param| {
            matches!(
                param.ty,
                AbiType::Slice {
                    ownership: SliceOwnership::Owned,
                    ..
                }
            )
        })
    })
}

fn collect_abi_types(module: &AbiModule) -> Vec<AbiType> {
    let mut found = BTreeSet::new();
    for structure in &module.structs {
        for field in &structure.fields {
            collect_abi_type(&field.ty, &mut found);
        }
    }
    for function in &module.functions {
        for param in &function.params {
            collect_abi_type(&param.ty, &mut found);
        }
        collect_abi_type(&function.output, &mut found);
    }
    found.into_iter().collect()
}

fn collect_abi_type(ty: &AbiType, found: &mut BTreeSet<AbiType>) {
    found.insert(ty.clone());
    if let AbiType::Slice { element, .. } = ty {
        collect_abi_type(element, found);
    }
}

fn collect_slice_type(ty: &AbiType, found: &mut BTreeMap<String, (AbiType, SliceOwnership, bool)>) {
    if let AbiType::Slice {
        element,
        ownership,
        mutable,
    } = ty
    {
        found.insert(render_type(ty), ((**element).clone(), *ownership, *mutable));
    }
}

fn render_type(ty: &AbiType) -> String {
    match ty {
        AbiType::Unit => "None".into(),
        AbiType::Primitive(primitive) => match primitive {
            MojoPrimitive::Bool => "Bool",
            MojoPrimitive::Int8 => "Int8",
            MojoPrimitive::UInt8 => "UInt8",
            MojoPrimitive::Int16 => "Int16",
            MojoPrimitive::UInt16 => "UInt16",
            MojoPrimitive::Int32 => "Int32",
            MojoPrimitive::UInt32 => "UInt32",
            MojoPrimitive::Int64 => "Int64",
            MojoPrimitive::UInt64 => "UInt64",
            MojoPrimitive::Int => "Int",
            MojoPrimitive::UInt => "UInt",
            MojoPrimitive::Float32 => "Float32",
            MojoPrimitive::Float64 => "Float64",
        }
        .into(),
        AbiType::Struct(name) => mojo_ident(name),
        AbiType::StructPointer { name, mutable } => {
            let origin = if *mutable {
                "MutUntrackedOrigin"
            } else {
                "ImmUntrackedOrigin"
            };
            format!("Pointer[{}, {origin}]", mojo_ident(name))
        }
        AbiType::Enum(name) => mojo_ident(name),
        AbiType::OpaquePointer {
            name,
            mutable,
            owned,
            optional,
        } => {
            let suffix = match (*optional, *owned, *mutable) {
                (false, true, _) => "Handle",
                (false, false, false) => "Ref",
                (false, false, true) => "MutRef",
                (true, true, _) => "OptionalHandle",
                (true, false, false) => "OptionalRef",
                (true, false, true) => "OptionalMutRef",
            };
            format!("{}{suffix}", mojo_ident(name))
        }
        AbiType::Slice {
            element,
            ownership,
            mutable,
        } => {
            let prefix = match ownership {
                SliceOwnership::Borrowed if *mutable => "DiplomatSliceMut",
                SliceOwnership::Borrowed => "DiplomatSlice",
                SliceOwnership::Owned => "DiplomatOwnedSlice",
            };
            format!("{prefix}_{}", type_suffix(element))
        }
    }
}

fn type_suffix(ty: &AbiType) -> String {
    match ty {
        AbiType::Primitive(p) => format!("{p:?}"),
        AbiType::Struct(name) => mojo_ident(name),
        AbiType::StructPointer { name, .. } => mojo_ident(name),
        AbiType::Enum(name) => mojo_ident(name),
        _ => "Unsupported".into(),
    }
}

fn mojo_ident(input: &str) -> String {
    let mut output = String::with_capacity(input.len());
    for (index, ch) in input.chars().enumerate() {
        if ch.is_ascii_alphanumeric() || (ch == '_' && index > 0) {
            output.push(ch);
        } else {
            output.push('_');
        }
    }
    if output.is_empty() || output.as_bytes()[0].is_ascii_digit() {
        output.insert(0, '_');
    }
    if is_reserved_mojo_identifier(&output) {
        output.push('_');
    }
    output
}

fn is_reserved_mojo_identifier(identifier: &str) -> bool {
    matches!(
        identifier,
        "alias"
            | "as"
            | "assert"
            | "async"
            | "await"
            | "break"
            | "comptime"
            | "continue"
            | "def"
            | "elif"
            | "else"
            | "fn"
            | "for"
            | "from"
            | "if"
            | "import"
            | "in"
            | "let"
            | "mut"
            | "out"
            | "owned"
            | "raise"
            | "raises"
            | "read"
            | "ref"
            | "return"
            | "self"
            | "Self"
            | "std"
            | "struct"
            | "trait"
            | "try"
            | "type"
            | "var"
            | "while"
            | "with"
            | "yield"
            | "True"
            | "False"
            | "None"
            | "Bool"
            | "Int8"
            | "UInt8"
            | "Int16"
            | "UInt16"
            | "Int32"
            | "UInt32"
            | "Int64"
            | "UInt64"
            | "Int"
            | "UInt"
            | "Float32"
            | "Float64"
            | "Pointer"
            | "OptionalPointer"
            | "MutUntrackedOrigin"
            | "ImmUntrackedOrigin"
            | "RegisterPassable"
            | "TrivialRegisterPassable"
    )
}
