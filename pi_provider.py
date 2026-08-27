#!/usr/bin/env python3
"""
pi_provider.py - High-Performance Custom Promptfoo Python Provider for the `pi` coding agent.
Translates `pi --mode json` event streams into Promptfoo trajectory spans,
token metrics, skill metadata, and condition guards.
Optimized with `orjson` / `ujson` and an in-memory streaming delta assembler.
"""

import os
import re
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional

from isolation import WorkspaceIsolation

# ------------------------------------------------------------------------------
# High-Performance JSON Parser with Fallbacks (orjson -> ujson -> stdlib json)
# ------------------------------------------------------------------------------
try:
    import orjson

    def json_loads(data: str | bytes) -> Any:
        return orjson.loads(data)

    def json_dumps(data: Any, indent: Optional[int] = None) -> str:
        option = orjson.OPT_INDENT_2 if indent else 0
        return orjson.dumps(data, option=option).decode("utf-8")

except ImportError:
    try:
        import ujson

        json_loads = ujson.loads
        json_dumps = ujson.dumps
    except ImportError:
        import json

        json_loads = json.loads
        json_dumps = json.dumps

# ------------------------------------------------------------------------------
# OpenTelemetry Tracer Setup (Integrated with Promptfoo OTLP Receiver)
# ------------------------------------------------------------------------------
try:
    from opentelemetry import trace
    from opentelemetry.propagate import extract
    from opentelemetry.trace import SpanKind, Status, StatusCode
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    _endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    _traces_url = f"{_endpoint.rstrip('/')}/v1/traces"
    _tracer_provider = TracerProvider()
    _exporter = OTLPSpanExporter(endpoint=_traces_url)
    _tracer_provider.add_span_processor(SimpleSpanProcessor(_exporter))
    trace.set_tracer_provider(_tracer_provider)
    _global_tracer = trace.get_tracer("promptfoo.pi_provider")
    OTEL_AVAILABLE = True
except Exception:
    OTEL_AVAILABLE = False
    _global_tracer = None
    _tracer_provider = None

MAX_TOOL_RESULT_BYTES = 32 * 1024  # 32 KB result truncation limit


def _extract_skill_name(path_or_cmd: str) -> Optional[str]:
    """
    Extracts skill name from a path referencing SKILL.md or a script in a skill folder.
    E.g. '/path/to/.skills/pointcloud-ops/SKILL.md' -> 'pointcloud-ops'
         '.skills/uv/scripts/sync.sh' -> 'uv'
    """
    if not isinstance(path_or_cmd, str):
        return None
    match = re.search(r"(?:^|[/|\\])(?:\.?skills[/|\\])?([^/|\\]+)[/|\\]SKILL\.md$", path_or_cmd, re.IGNORECASE)
    if match:
        return match.group(1)

    match_script = re.search(r"(?:^|[/|\\])\.?skills[/|\\]([^/|\\]+)[/|\\]", path_or_cmd)
    if match_script:
        return match_script.group(1)

    return None


def _truncate_result(result: Any) -> Any:
    """Truncates oversized tool result outputs to prevent memory/storage bloat."""
    if isinstance(result, str) and len(result.encode("utf-8")) > MAX_TOOL_RESULT_BYTES:
        return result[: MAX_TOOL_RESULT_BYTES // 2] + "\n\n[... truncated by pi_provider ...]\n"
    if isinstance(result, dict) and "content" in result and isinstance(result["content"], list):
        truncated_content = []
        for item in result["content"]:
            if isinstance(item, dict) and "text" in item:
                text = item["text"]
                if isinstance(text, str) and len(text.encode("utf-8")) > MAX_TOOL_RESULT_BYTES:
                    text = text[: MAX_TOOL_RESULT_BYTES // 2] + "\n\n[... truncated by pi_provider ...]\n"
                truncated_content.append({**item, "text": text})
            else:
                truncated_content.append(item)
        return {**result, "content": truncated_content}
    return result


def run_pi_session(
    prompt: str,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
    stop_on_tool_failure: bool = False,
    max_steps: Optional[int] = None,
    timeout_seconds: int = 120,
    extra_flags: Optional[List[str]] = None,
    traceparent: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Executes `pi --mode json -p "<prompt>"` as a live subprocess,
    assembles streaming deltas in real-time, enforces guards, emits OTel spans, and normalizes the trajectory.
    """
    # Prioritize npm-packages pi agent binary
    pi_candidates = [
        os.path.expanduser("~/.npm-packages/bin/pi"),
        shutil.which("pi"),
        os.path.expanduser("~/.local/bin/pi"),
        "/usr/local/bin/pi",
        "/usr/bin/pi",
    ]
    pi_bin = "pi"
    for candidate in pi_candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            pi_bin = candidate
            break

    cmd = [pi_bin, "--mode", "json", "-p"]

    if model:
        cmd.extend(["--model", str(model)])
    if extra_flags:
        cmd.extend(extra_flags)

    cmd.append(str(prompt))

    env = os.environ.copy()
    extra_paths = [
        os.path.expanduser("~/.npm-packages/bin"),
        os.path.expanduser("~/.local/bin"),
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
    env["PATH"] = ":".join(extra_paths) + ":" + env.get("PATH", "")

    start_time = time.time()
    proc = subprocess.Popen(
        cmd,
        cwd=cwd or os.getcwd(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )

    parent_ctx = None
    if OTEL_AVAILABLE and traceparent:
        try:
            parent_ctx = extract({"traceparent": traceparent})
        except Exception:
            parent_ctx = None

    active_otel_spans: Dict[str, Any] = {}
    turns: List[Dict[str, Any]] = []
    current_turn: Dict[str, Any] = {"thinking": "", "content": "", "tool_calls": []}
    tool_executions: List[Dict[str, Any]] = []
    skill_calls: List[str] = []
    final_output = ""
    stopped_early = False
    stop_reason = None
    step_count = 0
    token_usage = {"total": 0, "prompt": 0, "completion": 0}
    total_cost = 0.0

    try:
        assert proc.stdout is not None
        for line in iter(proc.stdout.readline, ""):
            line_str = line.strip()
            if not line_str:
                continue
            try:
                event = json_loads(line_str)
            except Exception:
                continue

            if not isinstance(event, dict):
                continue

            event_type = event.get("type")

            # ------------------------------------------------------------------
            # 1. Delta Assembler (Collapses streaming chunks into unified turns)
            # ------------------------------------------------------------------
            if event_type == "message_update":
                assistant_evt = event.get("assistantMessageEvent")
                if isinstance(assistant_evt, dict):
                    evt_sub_type = assistant_evt.get("type")
                    delta = str(assistant_evt.get("delta", ""))

                    if evt_sub_type == "thinking_delta":
                        current_turn["thinking"] += delta
                    elif evt_sub_type == "text_delta":
                        current_turn["content"] += delta

                # Keep token usage up-to-date
                u = event.get("usage")
                if isinstance(u, dict):
                    token_usage["total"] = u.get("totalTokens", token_usage["total"])
                    token_usage["prompt"] = u.get("input", token_usage["prompt"])
                    token_usage["completion"] = u.get("output", token_usage["completion"])
                    cost_val = u.get("cost")
                    if isinstance(cost_val, dict):
                        total_cost = cost_val.get("total", total_cost)

            # ------------------------------------------------------------------
            # 2. Tool Executions & Skill Tracking
            # ------------------------------------------------------------------
            elif event_type == "tool_execution_start":
                step_count += 1
                tool_name = str(event.get("toolName", ""))
                args = event.get("args", {})
                if not isinstance(args, dict):
                    args = {"raw": args}

                skill_name: Optional[str] = None
                # Check for skill reads
                if tool_name == "read":
                    path = str(args.get("path", ""))
                    skill_name = _extract_skill_name(path)
                    if skill_name and not any(s.get("name") == skill_name for s in skill_calls):
                        skill_calls.append({"name": skill_name, "path": path, "source": "read"})

                # Check for skill script executions
                elif tool_name == "bash":
                    cmd_str = str(args.get("command", ""))
                    skill_name = _extract_skill_name(cmd_str)
                    if skill_name and not any(s.get("name") == skill_name for s in skill_calls):
                        skill_calls.append({"name": skill_name, "path": cmd_str, "source": "bash"})

                tool_call_id = str(event.get("toolCallId", f"call_{step_count}"))
                tool_info = {
                    "toolCallId": tool_call_id,
                    "tool": tool_name,
                    "args": args,
                    "isError": False,
                    "timestamp": time.time(),
                }
                tool_executions.append(tool_info)
                current_turn["tool_calls"].append(tool_info)

                # Start OpenTelemetry child span
                if OTEL_AVAILABLE and _global_tracer and parent_ctx:
                    try:
                        span = _global_tracer.start_span(
                            name=tool_name,
                            context=parent_ctx,
                            kind=SpanKind.INTERNAL,
                        )
                        span.set_attribute("tool.name", tool_name)
                        span.set_attribute("gen_ai.tool.name", tool_name)
                        if skill_name:
                            span.set_attribute("skill.name", skill_name)
                        if isinstance(args, dict):
                            span.set_attribute("tool.args", json_dumps(args))
                            if "command" in args:
                                span.set_attribute("command", str(args["command"]))
                            if "path" in args:
                                span.set_attribute("path", str(args["path"]))
                            if "query" in args:
                                span.set_attribute("query", str(args["query"]))
                        active_otel_spans[tool_call_id] = span
                    except Exception:
                        pass

            elif event_type == "tool_execution_end":
                call_id = str(event.get("toolCallId", ""))
                is_error = bool(event.get("isError", False))
                result = _truncate_result(event.get("result"))

                for t in reversed(tool_executions):
                    if t.get("toolCallId") == call_id:
                        t["isError"] = is_error
                        t["result"] = result
                        break

                # End OpenTelemetry child span
                if OTEL_AVAILABLE and call_id in active_otel_spans:
                    span = active_otel_spans.pop(call_id, None)
                    if span:
                        try:
                            if is_error:
                                span.set_status(Status(StatusCode.ERROR, description="Tool execution failed"))
                                span.set_attribute("is_error", True)
                            else:
                                span.set_status(Status(StatusCode.OK))
                                span.set_attribute("is_error", False)
                            span.end()
                        except Exception:
                            pass

                # Condition check: stop on tool failure
                if is_error and stop_on_tool_failure:
                    stopped_early = True
                    stop_reason = f"tool_failure: {event.get('toolName')}"
                    proc.terminate()
                    break

            # ------------------------------------------------------------------
            # 3. Turn Milestones & Final Message Capture
            # ------------------------------------------------------------------
            elif event_type in ("turn_end", "agent_end"):
                if current_turn["thinking"] or current_turn["content"] or current_turn["tool_calls"]:
                    turns.append(current_turn)
                current_turn = {"thinking": "", "content": "", "tool_calls": []}

                msg = event.get("message")
                if isinstance(msg, dict):
                    content = msg.get("content", [])
                    if isinstance(content, list):
                        texts = [
                            c.get("text", "")
                            for c in content
                            if isinstance(c, dict) and c.get("type") == "text"
                        ]
                        if texts:
                            final_output = "\n".join(texts)

                if event_type == "agent_end":
                    messages = event.get("messages", [])
                    if isinstance(messages, list):
                        for m in reversed(messages):
                            if isinstance(m, dict) and m.get("role") == "assistant":
                                content = m.get("content", [])
                                if isinstance(content, list):
                                    texts = [
                                        c.get("text", "")
                                        for c in content
                                        if isinstance(c, dict) and c.get("type") == "text"
                                    ]
                                    if texts:
                                        final_output = "\n".join(texts)
                                        break
                    if not event.get("willRetry", False):
                        break

            elif event_type == "agent_settled":
                break

            # Cap max steps guard
            if max_steps and step_count >= max_steps:
                stopped_early = True
                stop_reason = f"max_steps_exceeded: {step_count}"
                proc.terminate()
                break

        # Fallback for final_output if still empty
        if not final_output:
            for t in reversed(turns):
                if t.get("content"):
                    final_output = t["content"]
                    break

        # Wait with remaining timeout
        elapsed = time.time() - start_time
        remaining_timeout = max(1, timeout_seconds - int(elapsed))
        proc.wait(timeout=remaining_timeout)

    except subprocess.TimeoutExpired:
        proc.kill()
        stopped_early = True
        stop_reason = "timeout_exceeded"
    finally:
        stderr_output = ""
        if proc.stderr:
            try:
                stderr_output = proc.stderr.read()
            except Exception:
                pass
            proc.stderr.close()
        if proc.stdout:
            proc.stdout.close()

        for span in active_otel_spans.values():
            try:
                span.end()
            except Exception:
                pass
        active_otel_spans.clear()

        if _tracer_provider:
            try:
                _tracer_provider.force_flush()
            except Exception:
                pass

    if not final_output:
        for t in reversed(turns):
            if t.get("content"):
                final_output = t["content"]
                break

    if not final_output and proc.returncode != 0:
        final_output = f"[pi process failed with exit code {proc.returncode}]: {stderr_output.strip()}"

    duration_ms = int((time.time() - start_time) * 1000)

    # --------------------------------------------------------------------------
    # 4. Generate OpenTelemetry-compatible Spans for Promptfoo Assertions
    # --------------------------------------------------------------------------
    spans = []
    for idx, t in enumerate(tool_executions):
        span_status = {"code": "ERROR"} if t["isError"] else {"code": "OK"}
        spans.append({
            "name": t["tool"],
            "attributes": {
                "tool.name": t["tool"],
                "tool.args": t["args"],
                "tool.arguments": t["args"],
                "tool.input": t["args"],
                "tool.is_error": t["isError"],
                "gen_ai.turn.index": idx,
            },
            "status": span_status,
        })

    return {
        "output": final_output,
        "stoppedEarly": stopped_early,
        "stopReason": stop_reason,
        "tokenUsage": token_usage,
        "cost": total_cost,
        "durationMs": duration_ms,
        "skillCalls": skill_calls,
        "toolCalls": tool_executions,
        "turns": turns,
        "trace": {
            "spans": spans
        },
        "exitCode": proc.returncode,
    }


def call_api(prompt: Any, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """
    Standard Promptfoo Python Provider Entrypoint.
    Accepts (prompt, options, context) or (prompt, context) flexibly.
    """
    options: Dict[str, Any] = {}
    context: Dict[str, Any] = {}

    for arg in args:
        if isinstance(arg, dict):
            if "config" in arg or "id" in arg:
                options = arg
            elif "vars" in arg or "prompt" in arg or "test" in arg:
                context = arg
            elif not options:
                options = arg
            elif not context:
                context = arg

    if "options" in kwargs and isinstance(kwargs["options"], dict):
        options = kwargs["options"]
    if "context" in kwargs and isinstance(kwargs["context"], dict):
        context = kwargs["context"]

    config = options.get("config", {}) if isinstance(options, dict) else {}
    if not isinstance(config, dict):
        config = {}

    vars_dict = context.get("vars", {}) if isinstance(context, dict) else {}
    if not isinstance(vars_dict, dict):
        vars_dict = {}

    cwd = vars_dict.get("cwd") or config.get("cwd")
    model = vars_dict.get("model") or config.get("model")
    stop_on_tool_failure = vars_dict.get(
        "stop_on_tool_failure", config.get("stop_on_tool_failure", False)
    )
    max_steps = vars_dict.get("max_steps", config.get("max_steps"))
    timeout_seconds = vars_dict.get(
        "timeout_seconds", config.get("timeout_seconds", 120)
    )
    traceparent = context.get("traceparent")
    if not traceparent and "vars" in context and isinstance(context["vars"], dict):
        traceparent = context["vars"].get("traceparent")

    actual_prompt = str(prompt)
    if (actual_prompt == "{{prompt}}" or not actual_prompt.strip()) and vars_dict.get("prompt"):
        actual_prompt = str(vars_dict["prompt"])

    # Workspace Isolation Options (inspired by Chromium agents/testing/workers.py)
    isolation_cfg = vars_dict.get("isolation")
    if isolation_cfg is None:
        isolation_cfg = config.get("isolation", False)

    isolation_enabled = False
    isolation_strategy = "git-worktree"
    isolation_clean = True
    workdir_parent = None

    if isinstance(isolation_cfg, bool):
        isolation_enabled = isolation_cfg
    elif isinstance(isolation_cfg, str):
        isolation_enabled = True
        isolation_strategy = isolation_cfg
    elif isinstance(isolation_cfg, dict):
        isolation_enabled = bool(isolation_cfg.get("enabled", True))
        isolation_strategy = str(isolation_cfg.get("strategy", "git-worktree"))
        isolation_clean = bool(isolation_cfg.get("clean", True))
        workdir_parent = isolation_cfg.get("workdir_parent")

    try:
        if isolation_enabled:
            target_cwd = cwd or os.getcwd()
            with WorkspaceIsolation(
                src_dir=target_cwd,
                strategy=isolation_strategy,
                clean=isolation_clean,
                workdir_parent=workdir_parent,
            ) as isolated_cwd:
                session = run_pi_session(
                    prompt=actual_prompt,
                    cwd=str(isolated_cwd),
                    model=model,
                    stop_on_tool_failure=bool(stop_on_tool_failure),
                    max_steps=max_steps,
                    timeout_seconds=int(timeout_seconds) if timeout_seconds else 120,
                    traceparent=traceparent,
                )
        else:
            session = run_pi_session(
                prompt=actual_prompt,
                cwd=cwd,
                model=model,
                stop_on_tool_failure=bool(stop_on_tool_failure),
                max_steps=max_steps,
                timeout_seconds=int(timeout_seconds) if timeout_seconds else 120,
                traceparent=traceparent,
            )

        return {
            "output": session["output"],
            "tokenUsage": session["tokenUsage"],
            "cost": session["cost"],
            "latencyMs": session["durationMs"],
            "metadata": {
                "skillCalls": session["skillCalls"],
                "toolCalls": session["toolCalls"],
                "turns": session["turns"],
                "trace": session["trace"],
                "stoppedEarly": session["stoppedEarly"],
                "stopReason": session["stopReason"],
                "stepCount": len(session["toolCalls"]),
                "exitCode": session["exitCode"],
                "isolated": isolation_enabled,
                "isolationStrategy": isolation_strategy if isolation_enabled else None,
            },
        }
    except Exception as e:
        import traceback
        return {
            "output": "",
            "error": f"{e}\n{traceback.format_exc()}",
        }


if __name__ == "__main__":
    import sys

    test_prompt = sys.argv[1] if len(sys.argv) > 1 else "what is 2+2?"
    res = call_api(test_prompt)
    print(json_dumps(res, indent=2))
