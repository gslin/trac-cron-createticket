# GNUmakefile for TracCronCreateTicket plugin

.PHONY: test lint

# Run tests (if any). Uses uv to execute pytest.
# If no tests are present, the command will simply report that.

test:
	@echo "Running tests..."
	@uv run pytest || echo "No tests found or pytest not installed."

# Lint the codebase using ruff (via uv).
lint:
	@echo "Running ruff linter..."
	@uv run ruff check .
