test:
    uv run python -m unittest -v

format:
    uv run ruff format .

lint:
    uv run ruff check .

typecheck:
    uv run ty check

check: lint typecheck test
    uv run ruff format --check .
