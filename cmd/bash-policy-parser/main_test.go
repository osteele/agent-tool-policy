package main

import (
	"reflect"
	"strings"
	"testing"
)

func TestParseCommands(t *testing.T) {
	tests := []struct {
		name       string
		command    string
		want       [][]string
		writesFile bool
	}{
		{
			name:    "compound",
			command: "echo one && ruff check .",
			want:    [][]string{{"echo", "one"}, {"ruff", "check", "."}},
		},
		{
			name:    "quotes removed",
			command: `weft run -C '~/code/project' beta 'python experiment.py'`,
			want:    [][]string{{"weft", "run", "-C", "~/code/project", "beta", "python experiment.py"}},
		},
		{
			name:    "arithmetic expansion",
			command: "echo $((1 + 2))",
			want:    [][]string{{"echo", "$((1 + 2))"}},
		},
		{
			name:    "case pattern",
			command: `case ":$PATH:" in *":$HOME/.local/bin:"*) echo present;; esac`,
			want:    [][]string{{"echo", "present"}},
		},
		{
			name:    "time clause",
			command: "time sleep 0.01",
			want:    [][]string{{"sleep", "0.01"}},
		},
		{
			name:    "nested substitution",
			command: "echo $(git status)",
			want:    [][]string{{"echo", "$(git status)"}, {"git", "status"}},
		},
		{
			name: "quoted heredoc is data",
			command: `python3 - <<'PY'
print("$(git commit)")
PY`,
			want: [][]string{{"python3", "-"}},
		},
		{
			name: "unquoted heredoc expansion executes",
			command: `python3 - <<PY
print("$(git commit)")
PY`,
			want: [][]string{{"python3", "-"}, {"git", "commit"}},
		},
		{
			name: "sqlite heredoc is data",
			command: `sqlite3 database.sqlite <<'SQL'
select 'git commit';
SQL`,
			want: [][]string{{"sqlite3", "database.sqlite"}},
		},
		{
			name:       "output redirect writes file",
			command:    "echo changed > file",
			want:       [][]string{{"echo", "changed"}},
			writesFile: true,
		},
		{
			name:       "append redirect writes file",
			command:    "cat source >> destination",
			want:       [][]string{{"cat", "source"}},
			writesFile: true,
		},
		{
			name:       "read write redirect writes file",
			command:    "cat <> state",
			want:       [][]string{{"cat"}},
			writesFile: true,
		},
		{
			name:    "input redirect does not write",
			command: "cat < source",
			want:    [][]string{{"cat"}},
		},
		{
			name:    "file descriptor redirect does not write",
			command: "echo warning >&2",
			want:    [][]string{{"echo", "warning"}},
		},
		{
			name:       "legacy combined redirect writes file",
			command:    "echo warning >& output.log",
			want:       [][]string{{"echo", "warning"}},
			writesFile: true,
		},
		{
			name:    "dev null output does not write file",
			command: "echo ignored >/dev/null",
			want:    [][]string{{"echo", "ignored"}},
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			got, err := analyzeShell(strings.NewReader(test.command))
			if err != nil {
				t.Fatalf("analyzeShell() error = %v", err)
			}
			if !reflect.DeepEqual(got.Commands, test.want) {
				t.Fatalf("analyzeShell().Commands = %#v, want %#v", got.Commands, test.want)
			}
			if got.WritesFiles != test.writesFile {
				t.Fatalf("analyzeShell().WritesFiles = %v, want %v", got.WritesFiles, test.writesFile)
			}
		})
	}
}

func TestParseCommandsRejectsInvalidSyntax(t *testing.T) {
	if _, err := analyzeShell(strings.NewReader("echo 'unterminated")); err == nil {
		t.Fatal("analyzeShell() unexpectedly accepted invalid syntax")
	}
}
