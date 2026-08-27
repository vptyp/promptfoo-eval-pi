#!/usr/bin/env python3
"""
Universal Agent-as-a-Judge Provider for Promptfoo.
Executes any pluggable agent CLI or harness (agy, claude, codex, openclaw, hermes, pi)
in non-interactive mode and passes the response back to Promptfoo.
"""

import os
import shlex
import subprocess
import sys
from typing import Any, Dict

DEFAULT_COMMAND = "agy -p"


def call_api(prompt: str, options: Dict[str, Any] = None, context: Dict[str, Any] = None) -> Dict[str, Any]:
    """Promptfoo custom provider entrypoint."""
    config = options.get("config", {}) if isinstance(options, dict) else {}
    command_str = config.get("command") or os.getenv("EVAL_JUDGE_COMMAND", DEFAULT_COMMAND)
    timeout = int(config.get("timeout_seconds", 180))

    if "{prompt}" in command_str:
        cmd = shlex.split(command_str.replace("{prompt}", prompt))
    else:
        cmd = [*shlex.split(command_str), prompt]

    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=config.get("cwd") or os.getcwd(),
        )
    except subprocess.TimeoutExpired:
        return {"error": f"Evaluator command '{command_str}' timed out after {timeout}s"}
    except Exception as e:
        return {"error": f"Evaluator command '{command_str}' failed to execute: {e}"}

    if res.returncode != 0:
        err_msg = res.stderr.strip() or res.stdout.strip() or f"exited with code {res.returncode}"
        return {"error": f"Evaluator command '{command_str}' failed ({err_msg})"}

    return {"output": res.stdout.strip()}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <prompt> [command]", file=sys.stderr)
        sys.exit(1)

    prompt_arg = sys.argv[1]
    cmd_arg = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_COMMAND
    result = call_api(prompt_arg, options={"config": {"command": cmd_arg}})
    if "error" in result:
        print(result["error"], file=sys.stderr)
        sys.exit(1)
    print(result.get("output", ""))
