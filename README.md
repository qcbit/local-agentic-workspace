# Local Agentic Workspace

## Description

A powerful, privacy-first VS Code extension that integrates an autonomous, self-correcting AI agent directly into your local development environment. Built for speed and security, this extension relies entirely on local compute or cloud APIs, utilizing a Python-based orchestrator, TCP sockets for low-latency IPC, and flexible LLM providers.

## 🚀 Features

* **Autonomous Agent Loop**: A fault-tolerant, self-correcting AI agent capable of reasoning through codebase issues, patching code, and handling its own formatting or tool-call errors on the fly.
* **Proactive Error Interception**: Automatically monitors the VS Code integrated terminal. If a command fails (e.g., non-zero exit code), the agent intercepts the error output and proposes a fix.
* **Lightning-Fast IPC**: Utilizes JSON-RPC 2.0 over a local TCP socket (`127.0.0.1:7777`) for seamless communication between the VS Code extension host and the Python orchestrator.
* **Semantic Search & RAG**: Real-time file syncing and codebase indexing powered by a local LanceDB vector store for sub-100ms semantic search.
* **Interactive CodeLens Approvals**: AI-proposed file modifications are routed to a native VS Code Diff View, allowing the developer to review, approve, or reject changes via CodeLens before anything is written to disk.
* **Dynamic Workspace Profiles**: A React-based webview for real-time configuration syncing, allowing seamless swapping between different environment configurations, context windows, and model backends (such as Ollama or Google Gemini).

## 🏗️ Architecture

This project utilizes a dual-stack architecture to separate the editor UI from the heavy AI orchestration:

* **Frontend (VS Code Extension)**: Written in TypeScript and Node.js. Manages the UI, webviews (React), text document syncing, and the Warp Proxy Server.
* **Backend (Orchestrator)**: Written in Python. Hosts the Agent Loop, LanceDB vector store, and the Tool Registry.
* **Inference Engine**: Supports local execution via Ollama or high-capacity cloud endpoints like Google Gemini.

## 📋 Prerequisites

Before running the extension in development mode, ensure you have the following installed:

* VS Code (latest version)
* Node.js (v18+)
* Python (3.10+)
* Make (for build scripts)
* Ollama running locally (if using local models).

## 🛠️ Development Setup

### 1. Prepare the Local LLM (Optional)

If using local execution, ensure Ollama is running and pull your preferred models. A quantized 14B model is recommended for the best balance of reasoning and speed.

```bash
ollama run qwen2.5-coder:14b-instruct-q4_K_M


```

### 2. Start Development Watchers

Open the repository root in your terminal and run the unified make command to install dependencies and start the TypeScript/React background watchers.

```bash
make dev


```

### 3. Launch the Extension

With `make dev` running in the background, open the repository in VS Code and press **F5**. The extension will automatically detect Development Mode and launch the Python orchestrator daemon on `127.0.0.1:7777`.

## ⚙️ Configuration & Multiple Profiles

The extension maintains its state in a `.agentic_config.json` file, which can exist at the workspace root or globally at `~/.agentic_config.json`. You can create, manage, and toggle between multiple named profiles to separate your local Ollama setup from enterprise cloud endpoints or Google Gemini.

### Managing Profiles via the UI

1. Click the Agentic Workspace item in the VS Code Status Bar (bottom right) to open the React Settings Panel.
2. Use the **Active Profile** dropdown to switch between existing configurations.
3. Type a name into the **New Profile name...** field and click **Add Profile** to create a fresh workspace profile.
4. Modify the profile parameters and click **Save & Sync to Orchestrator**.

---

### Setting Up a Google Gemini Profile

To configure a profile that uses Google's high-capacity Gemini models via OpenAI-compatible endpoints:

#### Step 1: Obtain a Google Gemini API Key

1. Navigate to **[Google AI Studio](https://aistudio.google.com/)** and sign in with your Google account.
2. Click on **Get API Key** in the left-hand sidebar.
3. Click **Create API Key** (you can create it for a new or existing Google Cloud project).
4. Copy the generated API key securely.

#### Step 2: Configure the Profile in VS Code

1. Open the Agentic Workspace Settings panel via the Status Bar.
2. Create a new profile named `Gemini` (or select an existing one) and configure the fields as follows:
* **Endpoint URL**: `[https://generativelanguage.googleapis.com/v1beta/openai/](https://generativelanguage.googleapis.com/v1beta/openai/)`
* **API Key / Auth Token**: Paste your Google AI Studio API key.
* **Model Deployment Name**: Enter a supported model identifier, such as `gemini-2.5-pro` or `gemini-3.7-flash`.
* **Max Context Tokens**: Set up to `1048576` to leverage Gemini's 1-million-token input capacity.


3. Click **Save & Sync to Orchestrator**.

---

**Endpoint URL Tips:**

* **Ollama Native:** If using Ollama, ensure your endpoint points to the chat completions path: `[http://127.0.0.1:11434/v1/chat/completions](http://127.0.0.1:11434/v1/chat/completions)`. The orchestrator will automatically attempt to append this if you only provide the base port.
* **Google Gemini:** Use the OpenAI compatibility base URL: `[https://generativelanguage.googleapis.com/v1beta/openai/](https://generativelanguage.googleapis.com/v1beta/openai/)`.
* **Azure/Enterprise Proxy:** If routing through an enterprise proxy, use the full deployment URL and ensure the API key is configured correctly in your profile settings.

## 🧠 Recommended Local Models

When configuring your memory or token limits for local models, refer to this table to ensure you give the agent enough room to read files without triggering context amnesia.

| Model (Ollama Tag) | Parameters | Native Context Window | Recommended `maxTokens` Setting |
| --- | --- | --- | --- |
| `qwen2.5-coder:14b` | 14B | 32,768 | 24000 |
| `qwen2.5-coder:7b` | 7B | 32,768 | 24000 |
| `llama3:8b` | 8B | 8,192 | 6000 |
| `mistral:7b` | 7B | 8,192 | 6000 |
| `deepseek-coder-v2` | 16B | 32,768 | 24000 |

*(Note: Cloud models like Google Gemini support context windows up to 1,048,576 tokens).*

## 🚑 Troubleshooting

* **Context Limit Reached (Amnesia Loop):** If the agent repeatedly loops on a task and the logs show `Summarizing and dropping old messages...`, the agent's memory is full. Increase your `maxTokens` limit in the settings to accommodate larger files.
* **Blank Chat Webview:** If the React chat panel loads entirely white, the VS Code state cache may be poisoned. Run `make dev` to ensure the webview bundle is compiled, then run the `Developer: Reload Window` command in VS Code.
* **Port 7777 in Use:** If the backend fails to spawn, a previous orphaned Python process may be holding the TCP socket open. Terminate the process manually using `lsof -i :7777` and `kill -9 <PID>`.

## 🛡️ Privacy & Security

This extension is designed to be customizable and secure.

* **No Telemetry**: No data is sent to external servers when using local models.
* **Local Vector DB**: LanceDB runs strictly on the host machine.
* **Explicit Approvals**: The agent cannot write to the file system without explicit user approval via the CodeLens diff UI.
