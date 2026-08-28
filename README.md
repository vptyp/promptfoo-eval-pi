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
- **Node.js** (v22.19+; required by current `pi` releases)
- **`uv` package manager** (required for on-demand OpenTelemetry dependencies):
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
- **`pi` CLI** installed and authenticated (e.g. `pi --version`)

### 2. Run Evaluations
The checked-in configuration selects `uv_python.sh` as Promptfoo's Python
executable:

```yaml
providers:
  - id: "file://pi_provider.py"
    config:
      pythonExecutable: "./uv_python.sh"
```

Promptfoo passes its Python worker arguments to this executable. The wrapper
resolves the evaluation harness directory, then runs:

```bash
uv run --all-extras --project <evaluation-harness> python <promptfoo arguments>
```

This creates or updates the harness's `.venv` from `pyproject.toml` and
`uv.lock`, including the development extras, without requiring the caller to
activate a virtual environment. Because `--project` is anchored to the wrapper's
directory, the same wrapper also works when Promptfoo is launched from a target
repository. For a configuration stored elsewhere, use the wrapper's absolute
path for `pythonExecutable`.

The first run may take longer while `uv` prepares the environment. The wrapper
must remain executable. It preserves the caller's environment, and
`pi_provider.py` passes that environment to `pi` unchanged; this is important
for selecting the intended `node` from Linuxbrew, `nvm`, `mise`, or `asdf`.

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
├── command_parser.py      # Deterministic GNU Bash (bashlex) & POSIX (shlex) parser
├── eval_judge.py          # Universal Agent-as-a-Judge provider for semantic rubrics
├── isolation.py           # Test-to-test workspace isolation (worktree, copy, in-place, btrfs)
├── tests/                 # Unit & lifecycle tests (isolation, command parser, diff capture)
│   ├── command_parser_overview.py  # Print parser JSON for a quoted shell command
│   ├── fixtures/
│   │   └── cli.py         # Schema-aware CLI used by parser comparison tests
│   ├── test_command_parser.py
│   └── test_isolation.py
├── uv_python.sh           # Portable uv execution wrapper for zero-config environments
├── pyproject.toml         # Python dependencies and pytest development extra
├── uv.lock                # Reproducible Python dependency lockfile
└── package.json           # Node project scripts & dependencies
```

---

## Test Configuration (`promptfooconfig.yaml`)

```yaml
description: "Pi Coding Agent Evaluation Suite"

providers:
  - id: "file://pi_provider.py"
    label: "pi-agent"
    config:
      timeout_seconds: 120

defaultTest:
  assert:
    - type: javascript
      value: "context.metadata.exitCode === 0"

tests:
  # --------------------------------------------------------------------------
  # 1. Verify Skill Activation
  # --------------------------------------------------------------------------
  - description: "Verify 'uv' skill is read when managing python packages"
    vars:
      prompt: "Use the uv skill to inspect available dependencies"
    assert:
      - type: skill-used
        value: "uv"

  # --------------------------------------------------------------------------
  # 2. Verify Tool Usage and Specific CLI Command Execution
  # --------------------------------------------------------------------------
  - description: "Verify pytest is invoked through bash tool"
    vars:
      prompt: "Run the test suite using pytest"
    assert:
      - type: trajectory:tool-used
        value: "bash"
      - type: trajectory:tool-args-match
        value:
          name: "bash"
          args:
            has_binary:
              pytest: true

  # --------------------------------------------------------------------------
  # 3. Verify Chronological Tool Sequence
  # --------------------------------------------------------------------------
  - description: "Verify file is read before edit is performed"
    vars:
      prompt: "Find and fix the typo in config.py"
    assert:
      - type: trajectory:tool-sequence
        value:
          steps:
            - read
            - edit

  # --------------------------------------------------------------------------
  # 4. Stop on Tool Failure Guard Condition
  # --------------------------------------------------------------------------
  - description: "Verify run executes with zero tool failures"
    vars:
      prompt: "List the files in the directory"
      stop_on_tool_failure: true
    assert:
      - type: trace-error-spans
        value: 0
      - type: javascript
        value: "context.metadata.stoppedEarly === false"
```

---

## Inspecting Parsed Commands

Use the overview script to see the exact JSON that backs command assertions. Pass the complete shell command as one quoted argument:

```bash
tests/command_parser_overview.py './cli.py analyze --timeout=10 "Something" && echo done'
```

Use `--engine bashlex` or `--engine shlex` to force an engine while comparing parser behavior.

The parser does not infer option schemas or short/long aliases. Both `--timeout=10` and `--timeout 10` set `has_flag["--timeout"]`; only the separated form also records `10` in `has_word`. A short form such as `-t` is recorded exactly as `has_flag["-t"]` and must be allowed separately by assertions.

Multiple `has_flag` keys use AND semantics. To accept any flag from a list, use the JavaScript assertion described under [Flag alternatives (OR)](ARCHITECTURE.md#flag-alternatives-or).

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

For semantic evaluations (`llm-rubric`), you can route evaluation prompts to an autonomous agent CLI (`agy -p`, `claude -p`, `codex`, etc.) in non-interactive mode. The judge evaluates the agent's textual response alongside the **structured `git diff`** captured before ephemeral worktree teardown:

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

---

## Test-to-Test Workspace Isolation (`isolation.py`)

Inspired by Chromium's [`agents/testing/workers.py`](https://chromium.googlesource.com/chromium/src/+/refs/heads/main/agents/testing/workers.py), you can run destructive tests in ephemeral, isolated workspaces that automatically clean up when done:

```yaml
providers:
  - id: "file://pi_provider.py"
    config:
      isolation:
        enabled: true
        strategy: "git-worktree" # git-worktree (default) | copy | in-place | btrfs
        clean: true              # auto-destroy workdir on completion
```

Or configure per-test:
```yaml
tests:
  - description: "Destructive refactoring test"
    vars:
      prompt: "Refactor PointCloudBuilder and delete legacy headers"
      isolation:
        enabled: true
        strategy: "git-worktree"
```

> [!TIP]
> **Automatic Git Diff for Judges:** Before the isolated worktree is destroyed, [`pi_provider.py`](pi_provider.py) automatically captures `git diff HEAD` and untracked file additions, attaching the structured diff to `metadata.gitDiff` and appending it to the evaluation response. This allows [`eval_judge.py`](eval_judge.py) and `llm-rubric` to evaluate the exact line-by-line code changes made by the agent.
