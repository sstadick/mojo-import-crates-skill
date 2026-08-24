use diplomat_gen_mojo::{abi_report_json, generate_from_file};
use std::ffi::OsString;
use std::path::PathBuf;

const USAGE: &str = "Usage: diplomat-gen-mojo <bridge.rs> [OPTIONS]\n\
\n\
Options:\n\
  -o, --output <output.mojo>  Write generated Mojo ABI source to a file\n\
      --report <report.json>  Write the versioned machine-readable ABI report\n\
      --deny-unsupported      Fail after writing outputs if lowering was partial\n\
  -h, --help                  Print help";

#[derive(Debug, PartialEq, Eq)]
struct Options {
    input: PathBuf,
    output: Option<PathBuf>,
    report: Option<PathBuf>,
    deny_unsupported: bool,
}

#[derive(Debug)]
enum Command {
    Generate(Options),
    Help,
}

fn main() {
    if let Err(error) = run() {
        eprintln!("diplomat-gen-mojo: {error}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), String> {
    let command = parse_args(std::env::args_os().skip(1))?;
    let Command::Generate(options) = command else {
        println!("{USAGE}");
        return Ok(());
    };
    validate_distinct_paths(&options)?;

    let generated = generate_from_file(&options.input).map_err(|error| error.to_string())?;
    if let Some(output) = &options.output {
        std::fs::write(output, &generated.mojo)
            .map_err(|error| format!("could not write {}: {error}", output.display()))?;
    } else {
        print!("{}", generated.mojo);
    }
    if let Some(report) = &options.report {
        let json = abi_report_json(&generated)
            .map_err(|error| format!("could not serialize ABI report: {error}"))?;
        std::fs::write(report, json)
            .map_err(|error| format!("could not write {}: {error}", report.display()))?;
    }
    for item in &generated.unsupported {
        eprintln!("unsupported {}: {}", item.item, item.reason);
    }
    if options.deny_unsupported && !generated.unsupported.is_empty() {
        return Err(format!(
            "{} unsupported item(s); output is partial",
            generated.unsupported.len()
        ));
    }
    Ok(())
}

fn validate_distinct_paths(options: &Options) -> Result<(), String> {
    let input = canonical_path_identity(&options.input)?;
    let output = options
        .output
        .as_deref()
        .map(canonical_path_identity)
        .transpose()?;
    let report = options
        .report
        .as_deref()
        .map(canonical_path_identity)
        .transpose()?;
    if output.as_ref() == Some(&input) || report.as_ref() == Some(&input) {
        return Err("output paths must not overwrite the input bridge".into());
    }
    if output.is_some() && output == report {
        return Err("--output and --report must name different files".into());
    }
    Ok(())
}

fn canonical_path_identity(path: &std::path::Path) -> Result<PathBuf, String> {
    if path.exists() {
        return path
            .canonicalize()
            .map_err(|error| format!("could not resolve {}: {error}", path.display()));
    }
    let parent = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or_else(|| std::path::Path::new("."));
    let name = path
        .file_name()
        .ok_or_else(|| format!("path has no file name: {}", path.display()))?;
    let parent = parent
        .canonicalize()
        .map_err(|error| format!("could not resolve {}: {error}", parent.display()))?;
    Ok(parent.join(name))
}

fn parse_args(args: impl IntoIterator<Item = OsString>) -> Result<Command, String> {
    let mut args = args.into_iter();
    let mut input = None;
    let mut output = None;
    let mut report = None;
    let mut deny_unsupported = false;
    let mut positional_only = false;

    while let Some(arg) = args.next() {
        if !positional_only && (arg == "-o" || arg == "--output") {
            set_path_option(&mut output, args.next(), "--output")?;
        } else if !positional_only && arg == "--report" {
            set_path_option(&mut report, args.next(), "--report")?;
        } else if !positional_only && arg == "--deny-unsupported" {
            if deny_unsupported {
                return Err("--deny-unsupported may only be supplied once".into());
            }
            deny_unsupported = true;
        } else if !positional_only && (arg == "-h" || arg == "--help") {
            return Ok(Command::Help);
        } else if !positional_only && arg == "--" {
            positional_only = true;
        } else if !positional_only && arg.to_string_lossy().starts_with('-') {
            return Err(format!(
                "unknown option `{}`\n{USAGE}",
                arg.to_string_lossy()
            ));
        } else if input.replace(PathBuf::from(arg)).is_some() {
            return Err("only one input file may be supplied".into());
        }
    }

    let input = input.ok_or_else(|| format!("missing input\n{USAGE}"))?;
    if output.is_some() && output == report {
        return Err("--output and --report must name different files".into());
    }
    Ok(Command::Generate(Options {
        input,
        output,
        report,
        deny_unsupported,
    }))
}

fn set_path_option(
    slot: &mut Option<PathBuf>,
    value: Option<OsString>,
    option: &str,
) -> Result<(), String> {
    if slot.is_some() {
        return Err(format!("{option} may only be supplied once"));
    }
    *slot = Some(PathBuf::from(
        value.ok_or_else(|| format!("{option} requires a path"))?,
    ));
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(args: &[&str]) -> Result<Command, String> {
        parse_args(args.iter().map(OsString::from))
    }

    #[test]
    fn parses_all_generation_options() {
        let Command::Generate(options) = parse(&[
            "bridge.rs",
            "--output",
            "ffi.mojo",
            "--report",
            "abi.json",
            "--deny-unsupported",
        ])
        .unwrap() else {
            panic!("expected generation options");
        };
        assert_eq!(options.input, PathBuf::from("bridge.rs"));
        assert_eq!(options.output, Some(PathBuf::from("ffi.mojo")));
        assert_eq!(options.report, Some(PathBuf::from("abi.json")));
        assert!(options.deny_unsupported);
    }

    #[test]
    fn help_does_not_require_input() {
        assert!(matches!(parse(&["--help"]).unwrap(), Command::Help));
    }

    #[test]
    fn rejects_missing_report_path() {
        assert_eq!(
            parse(&["bridge.rs", "--report"]).unwrap_err(),
            "--report requires a path"
        );
    }

    #[test]
    fn rejects_unknown_options() {
        let error = parse(&["--wat", "bridge.rs"]).unwrap_err();
        assert!(error.starts_with("unknown option `--wat`"), "{error}");
    }

    #[test]
    fn supports_dash_prefixed_input_after_separator() {
        let Command::Generate(options) = parse(&["--", "-bridge.rs"]).unwrap() else {
            panic!("expected generation options");
        };
        assert_eq!(options.input, PathBuf::from("-bridge.rs"));
    }

    #[test]
    fn output_and_report_must_differ() {
        let error = parse(&[
            "bridge.rs",
            "--output",
            "generated.txt",
            "--report",
            "generated.txt",
        ])
        .unwrap_err();
        assert_eq!(error, "--output and --report must name different files");
    }
}
