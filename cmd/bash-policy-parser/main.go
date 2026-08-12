package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"

	"mvdan.cc/sh/v3/syntax"
)

func main() {
	analysis, err := analyzeShell(os.Stdin)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	if err := json.NewEncoder(os.Stdout).Encode(analysis); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

type shellAnalysis struct {
	Commands []shellCommand `json:"commands"`
}

type shellCommand struct {
	Words     []string        `json:"words"`
	Redirects []shellRedirect `json:"redirects"`
}

type shellRedirect struct {
	Operator   string  `json:"operator"`
	Target     *string `json:"target"`
	WritesFile bool    `json:"writes_file"`
}

func analyzeShell(reader io.Reader) (shellAnalysis, error) {
	source, err := io.ReadAll(reader)
	if err != nil {
		return shellAnalysis{}, fmt.Errorf("read command: %w", err)
	}

	file, err := syntax.NewParser(syntax.Variant(syntax.LangBash)).Parse(
		strings.NewReader(string(source)),
		"",
	)
	if err != nil {
		return shellAnalysis{}, fmt.Errorf("parse command: %w", err)
	}

	analysis := shellAnalysis{Commands: make([]shellCommand, 0)}
	redirectsByCall := make(map[*syntax.CallExpr][]*syntax.Redirect)
	syntax.Walk(file, func(node syntax.Node) bool {
		statement, ok := node.(*syntax.Stmt)
		if !ok || len(statement.Redirs) == 0 {
			return true
		}
		if call, ok := statement.Cmd.(*syntax.CallExpr); ok {
			redirectsByCall[call] = append(redirectsByCall[call], statement.Redirs...)
			return true
		}
		syntax.Walk(statement.Cmd, func(descendant syntax.Node) bool {
			nested, ok := descendant.(*syntax.Stmt)
			if !ok {
				return true
			}
			if call, ok := nested.Cmd.(*syntax.CallExpr); ok {
				redirectsByCall[call] = append(redirectsByCall[call], statement.Redirs...)
			}
			return true
		})
		return true
	})

	syntax.Walk(file, func(node syntax.Node) bool {
		statement, ok := node.(*syntax.Stmt)
		if !ok {
			return true
		}
		call, ok := statement.Cmd.(*syntax.CallExpr)
		if !ok || len(call.Args) == 0 {
			return true
		}

		words := make([]string, 0, len(call.Args))
		for _, word := range call.Args {
			words = append(words, staticWord(source, word))
		}
		callRedirects := redirectsByCall[call]
		redirects := make([]shellRedirect, 0, len(callRedirects))
		for _, redirect := range callRedirects {
			var target *string
			if redirect.Word != nil {
				value := staticWord(source, redirect.Word)
				target = &value
			}
			redirects = append(redirects, shellRedirect{
				Operator:   redirect.Op.String(),
				Target:     target,
				WritesFile: redirectWritesFile(source, redirect),
			})
		}
		analysis.Commands = append(analysis.Commands, shellCommand{
			Words:     words,
			Redirects: redirects,
		})
		return true
	})
	return analysis, nil
}

func redirectWritesFile(source []byte, redirect *syntax.Redirect) bool {
	switch redirect.Op {
	case syntax.RdrOut,
		syntax.AppOut,
		syntax.RdrInOut,
		syntax.RdrClob,
		syntax.RdrAll,
		syntax.RdrAllClob,
		syntax.AppAll,
		syntax.AppAllClob:
		return redirect.Word == nil || staticWord(source, redirect.Word) != "/dev/null"
	case syntax.DplOut:
		if redirect.Word == nil {
			return true
		}
		target := staticWord(source, redirect.Word)
		if target == "-" {
			return false
		}
		_, err := strconv.ParseUint(target, 10, 64)
		return err != nil
	default:
		return false
	}
}

func staticWord(source []byte, word *syntax.Word) string {
	var text strings.Builder
	for _, part := range word.Parts {
		writeWordPart(&text, source, part)
	}
	return text.String()
}

func writeWordPart(text *strings.Builder, source []byte, part syntax.WordPart) {
	switch part := part.(type) {
	case *syntax.Lit:
		text.WriteString(part.Value)
	case *syntax.SglQuoted:
		text.WriteString(part.Value)
	case *syntax.DblQuoted:
		for _, nested := range part.Parts {
			writeWordPart(text, source, nested)
		}
	default:
		start, end := part.Pos().Offset(), part.End().Offset()
		if start <= end && end <= uint(len(source)) {
			text.Write(source[start:end])
		}
	}
}
