# Local Agentic Workspace

A privacy-first, fully local AI coding assistant integrated directly into VS Code. 

Unlike cloud-based tools that send your code over the internet, Local Agentic Workspace runs a standalone, air-gapped intelligence engine on your local machine. It combines a React-based chat interface with a powerful Python orchestrator to understand your codebase and automate workflows.

## Features
* **Privacy First:** All reasoning, embedding, and code analysis happens entirely on your local hardware.
* **Agentic Workflows:** Spawn background AI agents to research, write, and propose code changes.
* **Context-Aware Chat:** Communicate with an LLM that inherently understands your current workspace through local vector search (LanceDB).
* **Zero-Dependency Engine:** The backend runs via a standalone, pre-compiled binary. No complex Python environments required.

## Hardware Requirements
Because this extension runs AI models entirely on your local hardware, your machine needs enough resources to load the models into memory alongside VS Code.

* **OS:** macOS ARM64 (Apple Silicon: M1, M2, M3, or M4 series).
* **Memory (RAM):** 16GB Unified Memory recommended (required for smoothly running 7B-8B parameter LLMs + VS Code). 8GB is the bare minimum but may result in heavy swap usage.
* **Storage:** At least 5GB to 10GB of free disk space for downloaded LLM weights, FastEmbed ONNX models, and LanceDB vector indices.

## Configuring Your Local Models
To maintain complete privacy, this extension relies on a local LLM runner (such as Ollama, LM Studio, or llama.cpp) to generate responses.

**1. Start your local LLM provider:**
Ensure your local inference server is running in the background and exposing a local API endpoint (e.g., `http://127.0.0.1:11434` for Ollama or `http://127.0.0.1:1234` for LM Studio).

**2. Configure the extension:**
* Open the **Local Agentic Workspace** panel in your VS Code sidebar.
* Click the **Settings** icon (or run the `Local Agentic: Show Settings` command).
* In the configuration menu, input your local inference endpoint URL.
* Select your desired model from the drop-down menu. The extension automatically detects and populates this list with the models currently downloaded to your machine!

*Note: The embedding model used for vectorizing your codebase (FastEmbed) is bundled directly into the backend orchestrator and requires no manual configuration.*

## Getting Started
1. Install the extension.
2. Verify your local LLM runner is active.
3. Open a project and allow the background orchestrator to safely index your workspace into the local `.lancedb`.
4. Start chatting or use the Command Palette (`Cmd + Shift + P`) to trigger agentic code searches!
