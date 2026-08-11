parser := ".bin/bash-policy-parser"

build-parser:
    mkdir -p .bin
    go build -buildvcs=false -o {{parser}} ./cmd/bash-policy-parser

test: build-parser
    ${GO_TEST:-go test} ./...
    uv run python -m unittest -v

format:
    gofmt -w cmd/bash-policy-parser
    uv run ruff format .

lint:
    go vet ./...
    uv run ruff check .

typecheck:
    uv run ty check

check: lint typecheck test
    test -z "$(gofmt -l cmd/bash-policy-parser)"
    uv run ruff format --check .
