.PHONY: setup dev clean lint test audit

setup:
	@echo "Setting up local development environment..."
	@bash scripts/bootstrap.sh

dev:
	@echo "Starting local orchestrator daemon..."
	@python3 services/orchestrator/src/main.py

lint:
	@echo "Running linters..."
	@cd apps/vscode-extension && npm run lint || true
	@cd services/orchestrator && flake8 src/ || true

test:
	@echo "Running tests..."
	@cd services/orchestrator && pytest
	@cd apps/vscode-extension && npm test

clean:
	@echo "Cleaning up..."
	@rm -rf /tmp/*.sock *.sock
	@rm -rf apps/vscode-extension/out apps/vscode-extension/dist
	@rm -rf apps/warp-cli/target
	@find .-type d -name "__pycache__" -exec rm -rf {} +
