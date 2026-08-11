package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"strings"

	"mvdan.cc/sh/v3/syntax"
)

func main() {
	commands, err := parseCommands(os.Stdin)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	if err := json.NewEncoder(os.Stdout).Encode(commands); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func parseCommands(reader io.Reader) ([][]string, error) {
	source, err := io.ReadAll(reader)
	if err != nil {
		return nil, fmt.Errorf("read command: %w", err)
	}

	file, err := syntax.NewParser(syntax.Variant(syntax.LangBash)).Parse(
		strings.NewReader(string(source)),
		"",
	)
	if err != nil {
		return nil, fmt.Errorf("parse command: %w", err)
	}

	commands := make([][]string, 0)
	syntax.Walk(file, func(node syntax.Node) bool {
		call, ok := node.(*syntax.CallExpr)
		if !ok || len(call.Args) == 0 {
			return true
		}

		words := make([]string, 0, len(call.Args))
		for _, word := range call.Args {
			words = append(words, staticWord(source, word))
		}
		commands = append(commands, words)
		return true
	})
	return commands, nil
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
