# Local Agentic Workspace

## Description

A powerful, privacy-first VS Code extension that integrates an autonomous, self-correcting AI agent directly into your local development environment. Built for speed and security, this extension relies entirely on local compute, utilizing a Python-based orchestrator, Unix Domain Sockets (UDS) for low-latency IPC, and local LLMs.

## 🚀 Features

**Autonomous Agent Loop**: A fault-tolerant, self-correcting AI agent capable of reasoning through codebase issues, patching code, and handling its own formatting or tool-call errors on the fly.

**Proactive Error Interception**: Automatically monitors the VS Code integrated terminal. If a command fails (e.g., non-zero exit code), the agent intercepts the error output and proposes a fix.

**Lightning-Fast IPC**: Utilizes JSON-RPC 2.0 over Unix Domain Sockets (/tmp/agent.sock) for seamless, zero-network-overhead communication between the VS Code extension host and the Python orchestrator.

**Semantic Search & RAG**: Real-time file syncing and codebase indexing powered by a local LanceDB vector store for sub-100ms semantic search.

**Interactive CodeLens Approvals**: AI-proposed file modifications are routed to a native VS Code Diff View, allowing the developer to review, approve, or reject changes via CodeLens before anything is written to disk.

**Dynamic Workspace Profiles**: A React-based webview for real-time configuration syncing, allowing seamless swapping between different local models (e.g., qwen2.5-coder, llama3) and context windows.

## 🏗️ Architecture

This project utilizes a dual-stack architecture to separate the editor UI from the heavy AI orchestration:

**Frontend (VS Code Extension)**: Written in TypeScript and Node.js. Manages the UI, webviews (React), text document syncing, and the Warp Proxy Server.

**Backend (Orchestrator)**: Written in Python. Hosts the Agent Loop, LanceDB vector store, and the Tool Registry.

**Inference Engine**: Powered by Ollama, optimized for local execution on Apple Silicon (M-series) or standard GPU hardware.

## 📋 Prerequisites

Before running the extension in development mode, ensure you have the following installed:

- VS Code (latest version)
- Node.js (v18+)
- Python (3.10+)
- Ollama running locally.

## 🛠️ Development Setup

Currently, the extension runs in a split development environment.

### 1. Prepare the Local LLM

Ensure Ollama is running and pull your preferred models. A quantized 14B model is recommended for the best balance of reasoning and speed.

```Bash
ollama run qwen2.5-coder:14b-instruct-q4_K_M
```

### 2. Start the Python Orchestrator

Navigate to the Python backend directory, install dependencies, and boot the UDS server.

```Bash
pip install -r requirements.txt
cd services/orchestrator
python src/uds_server.py
```

Note: The server will bind to /tmp/agent.sock. Ensure you have the necessary write permissions.

### 3. Launch the VS Code Extension

Open the repository root in a new VS Code window.

```Bash
npm install
```

Press F5 to compile the TypeScript extension and launch the Extension Development Host.

## ⚙️ Configuration

The extension maintains its state in services/orchestrator/config.json.

To update settings via the UI:

1. Click the Agentic Workspace item in the VS Code Status Bar (bottom right). 
2. The React Settings Panel will fetch the current configuration from the backend.
3. Select your active profile, target model, and context window.
4. Click Sync to Orchestrator.

## 🛡️ Privacy & Security

This extension is designed to be 100% air-gapped.

**No Telemetry**: No data is sent to external servers when using a local model.

**Local Inference**: All LLM prompts and codebase context are processed locally via Ollama.

**Local Vector DB**: LanceDB runs strictly on the host machine.

**Explicit Approvals**: The agent cannot write to the file system without explicit user approval via the CodeLens diff UI.

## Status

Under development
