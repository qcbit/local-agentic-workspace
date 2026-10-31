# Observability

## Summary

The Local Agentic Workspace uses **Arize Phoenix** (via OpenTelemetry) to capture, visualize, and baseline LLM token usage across the orchestrator. Tracing is **opt-in** and disabled by default to preserve CPU/memory and reduce I/O overhead.

| Component | Stack |
|---|---|
| Trace SDK | OpenTelemetry (`opentelemetry-sdk`, `opentelemetry-exporter-otlp`) |
| LLM Instrumentation | `openinference-instrumentation-openai` (auto-instruments OpenAI Python SDK) |
| Backend / UI | Arize Phoenix (local, `python -m app.phoenix serve` → `http://localhost:6006`) |
| Export Protocol | OTLP over HTTP |
| Configuration Source | `.agentic_config.json` → `profile_settings.observability` |
| UI | VS Code extension → **Settings → Model & Sandbox → Observability & Telemetry** |
| Tracing Entry Point | `services/orchestrator/src/llm/tracing.py` → `setup_phoenix_tracing()` |

When enabled, every LLM call made through the OpenAI SDK is captured as an OTel span with token-count, model, and project metadata, streamed to the Phoenix collector in batches.

---

## How To

### 1. Start the Phoenix Backend (one-time per session)

```bash
# Option A: via the package entry-point
python -m app.phoenix serve

# Option B: via the px CLI
px serve
```

Phoenix listens on **`http://localhost:6006`** by default.

### 2. Enable Tracing in the VS Code Extension

1. Open the **Agentic Workspace** settings panel (gear icon / `Agentic: Open Settings`).
2. Select the desired **Profile** (top-left selector).
3. Under **Observability & Telemetry**:
   - ✅ Check **"Enable OpenTelemetry & Arize Phoenix Tracing."**
   - Confirm the **OTLP Exporter Endpoint** is correct (default `http://127.0.0.1:6006/v1/traces`).
4. Click **"Save & Sync to Orchestrator."**

This writes `observability.enabled: true` and `observability.endpoint` into the active profile in `.agentic_config.json`.

**Note**: You must **restart the backend process**. OpenTelemetry uses an immutable global TracerProvider. Once an OTLPSpanExporter is attached, it spawns a persistent background thread that periodically flushes spans. If tracing was ever enabled during the current session (or forced by the provider), toggling it off in the UI updates the config but does not stop the already-running exporter thread. You must completely kill and restart the Python orchestrator process to clear the singleton and respect the disabled configuration. You can use `Cmd+Shift+P` and choose `Developer: Reload Window` in Visual Studio Code.

### 3. Programmatic Setup (custom scripts / tests)

```python
# MUST be called BEFORE importing OpenAI in other modules
from services.orchestrator.src.llm.tracing import setup_phoenix_tracing

setup_phoenix_tracing(
    endpoint="http://localhost:6006/v1/traces",   # optional, defaults to env / localhost
    project_name="my-baseline-run",               # optional, defaults to env / "orchestrator-token-baseline"
)

# … run your workload …

# On clean exit:
from services.orchestrator.src.llm.tracing import shutdown_tracing
shutdown_tracing()
```

`setup_phoenix_tracing` is **idempotent** — calling it more than once is a safe no-op.

### 4. View Traces in Phoenix

1. Open **http://localhost:6006** in your browser.
2. Select the project (e.g. `orchestrator-token-baseline` or your custom name).
3. Inspect individual spans to see **prompt tokens**, **completion tokens**, **model name**, and **latency**.
4. Use Phoenix's built-in token-cost analysis to compare runs and establish baselines.

### 5. Configure Log Level / Rotation / Retention

In the same Settings panel, under the **Observability & Telemetry** section:

| Setting | Default | Description |
|---|---|---|
| Log Level | `INFO` | Minimum severity to emit (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| Log Rotation | `10 MB` | Size-based rotation threshold |
| Log Retention | `5 days` | How long local log files are kept before pruning |

These are stored under `profile_settings.logging` in `.agentic_config.json`.

---

## Configuration

### `.agentic_config.json` (per-profile)

```jsonc
{
  "active_profile": "Default",
  "profile_settings": {
    "observability": {
      "enabled": true,
      "endpoint": "http://127.0.0.1:6006/v1/traces"
    },
    "logging": {
      "level": "INFO",
      "rotation": "10 MB",
      "retention": "5 days"
    }
  }
}
```

### Environment Variables (override config file)

| Variable | Default | Description |
|---|---|---|
| `PHOENIX_COLLECTOR_ENDPOINT` | `http://localhost:6006/v1/traces` | OTLP HTTP endpoint for the Phoenix collector |
| `PHOENIX_PROJECT_NAME` | `orchestrator-token-baseline` | Project/dataset name shown in Phoenix UI |
| `BUILD_VERSION` | `local` | Value for the `service.version` resource attribute |

### OpenTelemetry Resource Attributes (set in `tracing.py`)

| Attribute | Example Value |
|---|---|
| `service.name` | `orchestrator` |
| `service.version` | `BUILD_VERSION` or `local` |
| `openinference.project.name` | `PHOENIX_PROJECT_NAME` or fallback |

### Required Python Packages

Ensure these are in your `requirements.txt` / `pyproject.toml`:

```
arize-phoenix
arize-phoenix-otel
opentelemetry-sdk
opentelemetry-exporter-otlp
openinference-instrumentation-openai
```

### Span Limits

The tracer provider is configured with `SpanLimits(max_attributes=1024)` to handle large LLM prompts/responses without dropping spans.

---

## Troubleshoot

### "No traces appearing in Phoenix UI"

| # | Check | Command / Fix |
|---|---|---|
| 1 | Is Phoenix running? | `curl -s http://localhost:6006` — should return HTTP 200 |
| 2 | Is `observability.enabled` true in the active profile? | `cat .agentic_config.json \| jq '.profile_settings.observability'` |
| 3 | Endpoint mismatch (port 6006 vs 4317, `/v1/traces` path)? | Ensure `obsEndpoint` matches the running Phoenix instance |
| 4 | `setup_phoenix_tracing()` called **after** OpenAI import? | It **must** run before any `import openai` / `from openai import …` |
| 5 | `openinference-instrumentation-openai` not installed? | `pip install openinference-instrumentation-openai` — check for the WARNING log |
| 6 | Firewall / proxy blocking localhost? | Check `pf` (macOS) or `iptables` rules for port 6006 |

### "Spans are truncated or missing attributes"

- **Cause:** `SpanLimits(max_attributes=1024)` was hit (very long system prompts).
- **Fix:** Increase the limit in `tracing.py` → `SpanLimits(max_attributes=2048)`.

### "Phoenix collector connection refused"

```bash
# Verify Phoenix bind address (should be 0.0.0.0 or 127.0.0.1:6006)
ss -tlnp | grep 6006    # Linux
lsof -iTCP:6006 -sTCP:LISTEN   # macOS
```

If Phoenix is bound to a non-default port, update the endpoint in Settings or set `PHOENIX_COLLECTOR_ENDPOINT`.

### "High memory usage after enabling tracing"

- **Cause:** `BatchSpanProcessor` buffers spans in memory when the collector is slow or down.
- **Mitigation:**
  - Ensure Phoenix is running before starting the orchestrator.
  - Reduce batch timeout (default 5 s) or max queue size in `tracing.py`.
  - Disable tracing for bulk batch jobs where per-call observability is not needed.

### "Stale traces from a previous session still showing"

- Phoenix retains traces per project. To start fresh, either:
  - Use a **new `PHOENIX_PROJECT_NAME`** for each baseline run, or
  - Delete the old project via the Phoenix UI (Projects → kebab-menu → Delete).

### "`shutdown_tracing()` not called → spans lost on crash"

- **Cause:** Process exited abnormally before `BatchSpanProcessor` could flush.
- **Fix:** Register an `atexit` hook or use a signal handler:
  ```python
  import atexit
  atexit.register(shutdown_tracing)
  ```

---

*Keep this file in sync with `tracing.py` and the Settings panel. Review after every major observability-stack upgrade.*
