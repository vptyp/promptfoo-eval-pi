# Pi Agent Evaluation Harness with Promptfoo

A test evaluation suite for the [`pi`](https://pi.dev) coding agent running in `--mode json`, powered by [Promptfoo](https://promptfoo.dev).

---

## Documentation Links

- [**Architecture & Design (`ARCHITECTURE.md`)**](ARCHITECTURE.md): Detailed component design, event streaming pipeline, `orjson` integration, and delta assembler.

---

## Features

- **Zero-Code Trajectory Assertions:** Uses native Promptfoo trajectory assertions (`skill-used`, `trajectory:tool-used`, `trajectory:tool-args-match`, `trajectory:tool-sequence`, `trace-error-spans`).
- **High-Performance Streaming (`orjson`):** Fast stream processing with `orjson` / `ujson` and an in-memory **Streaming Delta Assembler** that reduces trace payload memory by 90–95%.
- **Automatic Trace Normalization:** Translates `pi`'s line-delimited JSON (`ndjson`) event stream into OpenTelemetry-compliant trace spans, token counts, and cost metrics.
- **Skill Usage Tracking:** Automatically detects when `pi` discovers, views, or executes instructions from a `SKILL.md` file.
- **Live Stream Interception & Guardrails:** Supports early termination conditions such as `stop_on_tool_failure`, step caps (`max_steps`), and configurable timeouts.
- **Memory & Storage Safeguards:** Automatic 32 KB tool output truncation to keep Promptfoo SQLite storage compact on 300+ turn evaluations.
- **Interactive Web Dashboard:** Rich report viewer with diffs, latency, cost analysis, and full trace inspection.

---

## Quickstart

### 1. Prerequisites
- **Node.js** (v18+)
- **`uv` package manager** (required for on-demand OpenTelemetry dependencies):
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
- **`pi` CLI** installed and authenticated (e.g. `pi --version`)

### 2. Run Evaluations
The runner automatically uses `uv_python.sh` to manage OpenTelemetry dependencies on-demand without manual virtualenv management.

```bash
# Run evaluations in any project
npx promptfoo eval -j 1

# Open the interactive web dashboard to review traces
npx promptfoo view
```

---

## Project Structure

```
.
├── ARCHITECTURE.md        # High-level architecture, streaming pipeline & optimizations
├── README.md              # Project documentation and quickstart
├── promptfooconfig.yaml   # Promptfoo evaluation test suite and assertions
├── pi_provider.py         # High-performance Python provider with orjson & delta assembler
├── eval_judge.py          # Universal Agent-as-a-Judge provider for semantic rubrics
├── uv_python.sh           # Portable uv execution wrapper for zero-config environments
├── pyproject.toml         # Dependencies (opentelemetry-sdk, otlp exporter, orjson)
└── package.json           # Node project scripts & dependencies
```

---

## Test Configuration (`promptfooconfig.yaml`)

```yaml
description: "Pi Agent Evaluation Suite"

providers:
  - id: "file://pi_provider.py"
    label: "pi-agent"
    config:
      # Optional default provider options
      timeout_seconds: 120

tests:
  # --------------------------------------------------------------------------
  # 1. Verify Skill Activation
  # --------------------------------------------------------------------------
  - description: "Verify 'uv' skill is read and used"
    vars:
      prompt: "Use the uv skill to sync project dependencies and run tests"
    assert:
      - type: skill-used
        value: "uv"

  # --------------------------------------------------------------------------
  # 2. Verify Tool Usage and Specific CLI Command Execution
  # --------------------------------------------------------------------------
  - description: "Verify pytest CLI is executed via bash tool"
    vars:
      prompt: "Run tests in tests/test_core.py"
    assert:
      - type: trajectory:tool-used
        value: "bash"
      - type: trajectory:tool-args-match
        value:
          name: "bash"
          args:
            command: "*pytest tests/test_core.py*"

  # --------------------------------------------------------------------------
  # 3. Verify Chronological Tool Sequence
  # --------------------------------------------------------------------------
  - description: "Verify sequence: read file -> edit file -> run bash"
    vars:
      prompt: "Fix typo in config.py and verify with pytest"
    assert:
      - type: trajectory:tool-sequence
        value:
          steps:
            - read
            - edit
            - bash

  - description: "Verify ordered tool sequence: clean -> compile"
    vars:
      prompt: "Execute in two steps: meson compile --clean then meson compile"
    assert:
      - type: trajectory:tool-sequence
        value:
          steps:
            - bash
            - bash
      - type: icontains
        value: "clean"

  # --------------------------------------------------------------------------
  # 4. Stop on Tool Failure Guard Condition
  # --------------------------------------------------------------------------
  - description: "Strict mode: test fails if any tool error occurs"
    vars:
      prompt: "Format all python files with ruff format"
      stop_on_tool_failure: true
    assert:
      - type: trace-error-spans
        value: 0
```

---

## How It Works

1. **Promptfoo** invokes `call_api(prompt, options, context)` inside `pi_provider.py`.
2. `pi_provider.py` spawns `pi --mode json -p "<prompt>"` in a subprocess.
3. The live JSONL output is parsed in real time via `orjson`/`ujson`:
   - Streaming deltas (`text_delta`, `thinking_delta`) are assembled into clean turns.
   - Tool calls (`bash`, `read`, `edit`, `write`) are extracted.
   - Skill activations are detected via `SKILL.md` access.
   - OpenTelemetry-compatible spans are generated.
   - Early stopping triggers (like `stop_on_tool_failure`) terminate the subprocess if activated.
4. `pi_provider.py` returns standard Promptfoo `ProviderResponse` containing `output`, `tokenUsage`, `cost`, and `metadata.trace`.
5. Promptfoo's assertion engine evaluates all trajectory and text checks and renders the report in the web UI.

---

## Agent-as-a-Judge (`eval_judge.py`)

For semantic evaluations (`llm-rubric`), you can use an autonomous agent CLI with workspace/environment access as the judge instead of a passive LLM API:

```yaml
defaultTest:
  options:
    provider:
      id: "file:///path/to/prompt-eval/eval_judge.py"
      config:
        command: "agy -p" # Default
```

### Non-Interactive Command Examples:
- **Google Antigravity**: `command: "agy -p"`
- **Anthropic Claude Code**: `command: "claude -p"`
- **OpenAI Codex**: `command: "codex exec {prompt}"`
- **Pi Coding Agent**: `command: "pi -p"`
- **OpenClaw**: `command: "openclaw run {prompt}"`
- **Nous Hermes**: `command: "hermes run {prompt}"`
- **Aider**: `command: "aider --message {prompt} --yes"`
- **Custom Script**: `command: "./scripts/judge.sh {prompt}"`

Override globally via environment variable:
```bash
export EVAL_JUDGE_COMMAND="claude -p"
npx promptfoo eval
```
