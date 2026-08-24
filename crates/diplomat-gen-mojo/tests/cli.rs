use diplomat_gen_mojo::ABI_REPORT_SCHEMA_VERSION;
use serde_json::Value;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicUsize, Ordering};

static NEXT_TEMP_DIR: AtomicUsize = AtomicUsize::new(0);

struct TempDir(PathBuf);

impl TempDir {
    fn new() -> Self {
        let sequence = NEXT_TEMP_DIR.fetch_add(1, Ordering::Relaxed);
        let path = std::env::temp_dir().join(format!(
            "diplomat-gen-mojo-cli-{}-{sequence}",
            std::process::id()
        ));
        std::fs::create_dir(&path).unwrap();
        Self(path)
    }

    fn join(&self, path: impl AsRef<Path>) -> PathBuf {
        self.0.join(path)
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        std::fs::remove_dir_all(&self.0).unwrap();
    }
}

fn fixture(name: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures")
        .join(name)
}

#[test]
fn cli_writes_mojo_and_json_report() {
    let temp = TempDir::new();
    let mojo = temp.join("ffi.mojo");
    let report = temp.join("abi.json");
    let output = Command::new(env!("CARGO_BIN_EXE_diplomat-gen-mojo"))
        .arg(fixture("supported_surface.rs"))
        .arg("--output")
        .arg(&mojo)
        .arg("--report")
        .arg(&report)
        .arg("--deny-unsupported")
        .output()
        .unwrap();

    assert!(output.status.success(), "{output:#?}");
    assert!(output.stdout.is_empty());
    assert!(String::from_utf8(std::fs::read(mojo).unwrap())
        .unwrap()
        .contains("comptime EntryIterator_next_abi"));
    let report: Value = serde_json::from_slice(&std::fs::read(report).unwrap()).unwrap();
    assert_eq!(report["schema_version"], ABI_REPORT_SCHEMA_VERSION);
    assert!(report["functions"]
        .as_array()
        .unwrap()
        .iter()
        .any(|function| function["mojo_abi_alias"] == "EntryIterator_next_abi"));
    assert_eq!(report["functions"].as_array().unwrap().len(), 7);
    assert!(report["unsupported"].as_array().unwrap().is_empty());
}

#[test]
fn deny_unsupported_still_writes_complete_reports() {
    let temp = TempDir::new();
    let mojo = temp.join("partial.mojo");
    let report = temp.join("partial.json");
    let output = Command::new(env!("CARGO_BIN_EXE_diplomat-gen-mojo"))
        .arg(fixture("enum_nullable.rs"))
        .arg("-o")
        .arg(&mojo)
        .arg("--report")
        .arg(&report)
        .arg("--deny-unsupported")
        .output()
        .unwrap();

    assert!(!output.status.success());
    assert!(mojo.exists());
    let report: Value = serde_json::from_slice(&std::fs::read(report).unwrap()).unwrap();
    let unsupported = report["unsupported"].as_array().unwrap();
    assert_eq!(unsupported.len(), 1);
    assert_eq!(
        unsupported[0]["item"],
        "Widget::unsupported_optional_scalar"
    );
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(stderr.contains("1 unsupported item(s); output is partial"));
}

#[test]
fn cli_refuses_to_overwrite_the_bridge_or_alias_outputs() {
    let bridge = fixture("supported_surface.rs");
    let overwrite = Command::new(env!("CARGO_BIN_EXE_diplomat-gen-mojo"))
        .arg(&bridge)
        .arg("--output")
        .arg(&bridge)
        .output()
        .unwrap();
    assert!(!overwrite.status.success());
    assert!(String::from_utf8(overwrite.stderr)
        .unwrap()
        .contains("must not overwrite the input bridge"));

    let temp = TempDir::new();
    let output_path = temp.join("same-output");
    let alias = temp.join(".").join("same-output");
    let collision = Command::new(env!("CARGO_BIN_EXE_diplomat-gen-mojo"))
        .arg(bridge)
        .arg("--output")
        .arg(output_path)
        .arg("--report")
        .arg(alias)
        .output()
        .unwrap();
    assert!(!collision.status.success());
    assert!(String::from_utf8(collision.stderr)
        .unwrap()
        .contains("must name different files"));
}
