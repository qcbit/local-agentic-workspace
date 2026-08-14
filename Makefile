.PHONY: setup dev clean clean-full lint test build-backend package

setup:
	@echo "Setting up local development environment..."
	@bash scripts/bootstrap.sh

dev:
	@echo "Starting local orchestrator daemon..."
	@python3 services/orchestrator/src/ipc/uds_server.py

lint:
	@echo "Running linters..."
	@cd apps/vscode-extension && npm run lint || true
	@flake8 services/orchestrator/src/ || true

test:
	@echo "Running tests..."
	@cd services/orchestrator && pytest
	@cd apps/vscode-extension && npm test

clean:
	@echo "Cleaning up standard build files..."
	@rm -rf /tmp/*.sock *.sock
	@rm -rf apps/vscode-extension/out apps/vscode-extension/dist
	@rm -rf apps/warp-cli/target
	@rm -f apps/vscode-extension/*.vsix
	@find . -type d -name "__pycache__" -exec rm -rf {} +
	@find . -type f -name "*.pyc" -delete

clean-full: clean
	@echo "Performing full cleanup (including binaries)..."
	@rm -rf build/ dist/
	@rm -rf apps/vscode-extension/bin

build-backend:
	@echo "Compiling the PyInstaller binary using the spec file..."
	@pyinstaller uds_server-macos-arm64.spec --clean
	@mkdir -p apps/vscode-extension/bin
	@cp dist/uds_server-macos-arm64 apps/vscode-extension/bin/uds_server-macos-arm64

package: build-backend
	@echo "Compiling the React/TS frontend..."
	@cd apps/vscode-extension && npm run compile
	@echo "Packaging the VS Code extension..."
	@cd apps/vscode-extension && npx vsce package
