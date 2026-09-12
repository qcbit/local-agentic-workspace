# Agentic Workflows & Context Engineering

The `warp/workflows/` directory houses programmatic, context-engineered templates for the Local Agentic Workspace. While generic desktop clients rely on appended chat text, this directory enforces behavior directly through the Python orchestrator's state machine.

Context engineering is the practice of designing goals, instructions, constraints, and decision logic that guide agents through multi-step workflows. By pre-defining the operational context, you prevent prompt fatigue and guarantee predictable output formatting.

## Workflow Schema

Each workflow is a JSON object containing the operating manual for the LLM.

*   **`orchestrator_config`**: Dynamically overrides `.agentic_config.json` for the duration of the task. Useful for enforcing strict sandboxes or disabling web search for sensitive audits.
*   **`context_engineering`**: Injects an operating manual into the agent's `system_prompt`.
*   **`pre_flight_actions`**: An array of tool calls the orchestrator executes *before* the first LLM generation. This seeds the context window with immediate data, reducing token costs and iteration cycles.

## Best Practices: Preventing Hallucinations in Local Models

When managing local open-source language models (like Qwen or Llama variants) through Ollama, models often experience "instruction fade" or plan-to-execution drift. They may understand a constraint conceptually but fail to execute it mechanically. Apply these strategies to keep them on track:

### 1. Invert the Prompt Structure (Recency Bias)
Models prioritize the instructions they read last. Always place your `output_contract` at the absolute bottom of the `context_engineering` block. If the desired formatting is the final thing the model reads before generating text, compliance rates increase dramatically.

### 2. Forced Serialization (Phase-Based Constraints)
Local models struggle with implicit priorities and will often rush to complete a task. Break complex requirements into explicitly named and numbered phases:
*   *Bad:* "Check your syntax before writing to the file."
*   *Good:* "PHASE 1 (READ): Read the file. PHASE 2 (VALIDATE): Use python_repl to run ast.parse(). PHASE 3 (WRITE): Only write the file if Phase 2 succeeds."

### 3. Explicit Anti-Hallucination Directives
Models naturally generate generalized boilerplate when summarizing tasks (e.g., claiming they refactored an entire file when they only changed one line). Use explicit negative constraints to anchor their summaries to reality:
*   *Example:* "ANTI-HALLUCINATION RULE: Do not claim credit for code or type hints that were already present in the original file. Your changelog must only include the exact lines you physically altered."

### 4. Inject Few-Shot Examples
Instead of abstractly describing how a final report should look, provide a dummy example of the expected structure within the `output_contract`. Models emulate structural patterns much more reliably than they interpret formatting instructions.

### 5. Programmatic Circuit Breakers
Do not rely entirely on the LLM to follow rules. Use `orchestrator_config` to physically disable tools (like setting `sandbox.strict_mode = true`) so the agent mathematically cannot violate boundary conditions, regardless of its generated reasoning.

## Execution

To invoke a workflow, use the `@workflow` syntax in your VS Code Chat panel:

`@workflow:python_refactor Add type hints to services/orchestrator/src/memory/context_manager.py`

The extension host parses the tag, loads the corresponding JSON from `warp/workflows/`, and passes the merged context payload via UDS to the Python state machine.
