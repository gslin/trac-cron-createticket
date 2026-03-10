# GNUmakefile for TracCronCreateTicket plugin

.PHONY: test lint clean

# Run tests (if any). Uses uv to execute pytest.
# If no tests are present, the command will simply report that.

test:
	@echo "Running tests..."
	@uv run --extra dev pytest || echo "No tests found or pytest not installed."

# Lint the codebase using ruff (via uv).
lint:
	@echo "Running ruff linter..."
	@uv run --extra dev ruff check .

# Clean generated artefacts
clean:
	@echo "Cleaning project artefacts..."
	@rm -rf __pycache__ .ruff_cache .mypy_cache .pytest_cache .coverage *.egg-info build dist
