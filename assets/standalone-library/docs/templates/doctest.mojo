{{/* Global-only doctests are complete programs; grouped snippets are wrapped as TestSuite tests. */}}
{{if .Code}}
from std.testing import TestSuite

{{range .Global}}{{.}}
{{end}}

def test_{{.Name}}() raises:
{{range .Code}}    {{.}}
{{end}}


def main() raises:
    TestSuite.discover_tests[__functions_in_module()]().run()
{{else}}
{{range .Global}}{{.}}
{{end}}
{{end}}
