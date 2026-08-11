package main

import (
	"reflect"
	"strings"
	"testing"
)

func TestParseCommands(t *testing.T) {
	tests := []struct {
		name    string
		command string
		want    [][]string
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
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			got, err := parseCommands(strings.NewReader(test.command))
			if err != nil {
				t.Fatalf("parseCommands() error = %v", err)
			}
			if !reflect.DeepEqual(got, test.want) {
				t.Fatalf("parseCommands() = %#v, want %#v", got, test.want)
			}
		})
	}
}

func TestParseCommandsRejectsInvalidSyntax(t *testing.T) {
	if _, err := parseCommands(strings.NewReader("echo 'unterminated")); err == nil {
		t.Fatal("parseCommands() unexpectedly accepted invalid syntax")
	}
}
