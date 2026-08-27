#!/usr/bin/env python3
"""
command_parser.py - Deterministic Bash & Shell Command Parsing Engine.
Leverages GNU Bash AST via bashlex with automatic shlex lexical fallback
and graceful non-bash (PowerShell/CMD) pass-through.
"""

import dataclasses
import os
import pathlib
import re
import shlex
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    import bashlex
    import bashlex.errors

    BASHLEX_AVAILABLE = True
except ImportError:
    BASHLEX_AVAILABLE = False


# Common CLI tools with prominent subcommands (multi-verb interfaces)
KNOWN_SUBCOMMAND_BINARIES: Set[str] = {
    "git",
    "meson",
    "ninja",
    "cargo",
    "docker",
    "docker-compose",
    "podman",
    "kubectl",
    "npm",
    "npx",
    "yarn",
    "pnpm",
    "pip",
    "pip3",
    "poetry",
    "uv",
    "pytest",
    "apt",
    "apt-get",
    "systemctl",
    "journalctl",
    "ip",
    "aws",
    "gcloud",
    "az",
    "agy",
    "claude",
    "pi",
    "openclaw",
    "python",
    "python3",
    "go",
    "rustup",
    "composer",
    "bundle",
    "mvn",
    "gradle",
}

# Command wrappers that precede the real target executable
COMMAND_WRAPPERS: Set[str] = {
    "sudo",
    "time",
    "nohup",
    "env",
    "xargs",
    "builtin",
    "command",
    "exec",
    "nice",
    "ionice",
    "strace",
    "valgrind",
    "gdb",
}

# Common flags known to take a parameter (flag value) across standard CLI tools
PARAMETERIZED_FLAGS: Set[str] = {
    "-C",
    "-m",
    "-f",
    "-o",
    "-c",
    "-j",
    "-p",
    "-t",
    "-i",
    "-e",
    "-u",
    "-n",
    "--config",
    "--file",
    "--target",
    "--directory",
    "--output",
    "--message",
    "--model",
    "--filter",
    "--jobs",
    "--user",
    "--env",
    "--workdir",
}


@dataclasses.dataclass
class ParsedCommand:
    """Represents a single atomic command within a shell pipeline."""

    raw: str
    binary: str
    subcommand: Optional[str] = None
    signature: str = ""
    flags: List[str] = dataclasses.field(default_factory=list)
    args: List[str] = dataclasses.field(default_factory=list)
    env_vars: Dict[str, str] = dataclasses.field(default_factory=dict)
    wrappers: List[str] = dataclasses.field(default_factory=list)
    redirections: List[str] = dataclasses.field(default_factory=list)
    operator_after: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "raw": self.raw,
            "binary": self.binary,
            "subcommand": self.subcommand,
            "signature": self.signature,
            "flags": self.flags,
            "args": self.args,
            "env_vars": self.env_vars,
            "wrappers": self.wrappers,
            "redirections": self.redirections,
            "operator_after": self.operator_after,
        }


@dataclasses.dataclass
class ParsedPipeline:
    """Represents a complete pipeline of one or more shell commands."""

    raw_command: str
    is_bash: bool
    parser_engine: str = "bashlex"  # "bashlex" | "shlex" | "passthrough"
    commands: List[ParsedCommand] = dataclasses.field(default_factory=list)
    binaries: List[str] = dataclasses.field(default_factory=list)
    subcommands: List[str] = dataclasses.field(default_factory=list)
    signatures: List[str] = dataclasses.field(default_factory=list)
    env_vars: Dict[str, str] = dataclasses.field(default_factory=dict)
    has_binary: Dict[str, bool] = dataclasses.field(default_factory=dict)
    has_subcommand: Dict[str, bool] = dataclasses.field(default_factory=dict)
    has_signature: Dict[str, bool] = dataclasses.field(default_factory=dict)
    has_flag: Dict[str, bool] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_bash": self.is_bash,
            "parser_engine": self.parser_engine,
            "raw_command": self.raw_command,
            "binaries": self.binaries,
            "subcommands": self.subcommands,
            "signatures": self.signatures,
            "env_vars": self.env_vars,
            "has_binary": self.has_binary,
            "has_subcommand": self.has_subcommand,
            "has_signature": self.has_signature,
            "has_flag": self.has_flag,
            "commands": [c.to_dict() for c in self.commands],
        }

    def has_command(
        self,
        binary: Optional[str] = None,
        subcommand: Optional[str] = None,
        signature: Optional[str] = None,
        arg_contains: Optional[str] = None,
    ) -> bool:
        """Predicate helper to verify if the pipeline contains matching commands."""
        for c in self.commands:
            if binary and c.binary != binary:
                continue
            if subcommand and c.subcommand != subcommand:
                continue
            if signature and c.signature != signature:
                continue
            if arg_contains and not any(arg_contains in a for a in (c.args + c.flags)):
                continue
            return True
        return False


class BashCommandParser:
    """Deterministic Bash & POSIX shell command parser with hybrid bashlex/shlex engine."""

    @staticmethod
    def is_bash_environment(cmd_str: str = "") -> bool:
        """
        Detects whether the environment/command is POSIX/Bash vs PowerShell or Windows CMD.
        Returns False for PowerShell / Windows Batch, True for standard Bash.
        """
        s = cmd_str.strip()
        if not s:
            return True

        # Detect PowerShell Cmdlets and Syntax
        ps_cmdlet_pattern = r"\b(Get-|Set-|Start-|Stop-|New-|Remove-|Where-Object|Select-Object|ForEach-Object|Out-File|Invoke-)\b"
        if re.search(ps_cmdlet_pattern, s, re.IGNORECASE):
            return False
        if s.startswith("$") and not s.startswith("$(") and not re.match(r"^\$[A-Za-z_][A-Za-z0-9_]*\b", s):
            return False
        if "pwsh" in s.lower() or "powershell" in s.lower():
            return False

        # Detect Windows CMD specific syntax
        cmd_pattern = r"\b(dir|type|cls|copy|move|del)\s+/[A-Za-z]"
        if re.search(cmd_pattern, s, re.IGNORECASE):
            return False

        return True

    @classmethod
    def _build_command_from_words(
        cls,
        words: List[str],
        env_vars: Dict[str, str],
        wrappers: List[str],
        redirections: List[str],
    ) -> Optional[ParsedCommand]:
        if not words:
            if env_vars or wrappers:
                # E.g. inline export or env assignment only
                return ParsedCommand(
                    raw=" ".join(wrappers),
                    binary="",
                    env_vars=env_vars,
                    wrappers=wrappers,
                    redirections=redirections,
                )
            return None

        # Unwrap wrappers at start of words (e.g. sudo, time, valgrind --flags)
        idx = 0
        while idx < len(words) and words[idx] in COMMAND_WRAPPERS:
            wrappers.append(words[idx])
            idx += 1
            # Skip flags belonging to the wrapper (e.g. valgrind --leak-check=full, sudo -u root)
            while idx < len(words) and words[idx].startswith("-"):
                wrappers.append(words[idx])
                if (
                    idx + 1 < len(words)
                    and not words[idx + 1].startswith("-")
                    and words[idx] in ("-u", "-n", "-g", "--user", "--group")
                ):
                    idx += 1
                    wrappers.append(words[idx])
                idx += 1

        # Check for inline env vars after wrappers (e.g. sudo CC=clang CXX=clang++ ...)
        while idx < len(words) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=.*", words[idx]):
            k, v = words[idx].split("=", 1)
            env_vars[k] = v
            idx += 1

        if idx >= len(words):
            return None

        binary_raw = words[idx]
        idx += 1

        # Normalize binary name (e.g. /usr/bin/git -> git, ./build/tests -> ./build/tests)
        if binary_raw.startswith(("./", "../")):
            binary = binary_raw
        else:
            binary = pathlib.Path(binary_raw).name

        subcommand: Optional[str] = None
        flags: List[str] = []
        args: List[str] = []

        has_subcommand_support = binary in KNOWN_SUBCOMMAND_BINARIES

        while idx < len(words):
            w = words[idx]
            if w.startswith("-"):
                flags.append(w)
                if (
                    idx + 1 < len(words)
                    and not words[idx + 1].startswith("-")
                    and w in PARAMETERIZED_FLAGS
                ):
                    idx += 1
                    flags.append(words[idx])
            elif has_subcommand_support and subcommand is None:
                subcommand = w
            else:
                args.append(w)
            idx += 1

        signature = f"{binary} {subcommand}" if subcommand else binary
        raw_repr = " ".join(words)

        return ParsedCommand(
            raw=raw_repr,
            binary=binary,
            subcommand=subcommand,
            signature=signature,
            flags=flags,
            args=args,
            env_vars=env_vars,
            wrappers=wrappers,
            redirections=redirections,
        )

    # ----------------------------------------------------------------------
    # 1. Primary Engine: bashlex GNU Bash AST Parser
    # ----------------------------------------------------------------------
    @classmethod
    def _parse_with_bashlex(cls, cmd_str: str) -> Optional[List[ParsedCommand]]:
        if not BASHLEX_AVAILABLE:
            return None

        try:
            tree = bashlex.parse(cmd_str)
        except Exception:
            return None

        commands: List[ParsedCommand] = []

        def extract_command_node(node, op_after: Optional[str] = None) -> Optional[ParsedCommand]:
            if node.kind != "command":
                return None

            words: List[str] = []
            env_vars: Dict[str, str] = {}
            wrappers: List[str] = []
            redirections: List[str] = []

            for part in getattr(node, "parts", []):
                if part.kind == "assignment":
                    word = getattr(part, "word", "")
                    if "=" in word:
                        k, v = word.split("=", 1)
                        env_vars[k] = v
                elif part.kind == "word":
                    words.append(getattr(part, "word", ""))
                elif part.kind == "redirect":
                    start, end = getattr(part, "pos", (0, 0))
                    if start < end and end <= len(cmd_str):
                        redirections.append(cmd_str[start:end].strip())

            for red in getattr(node, "redirects", []):
                start, end = getattr(red, "pos", (0, 0))
                if start < end and end <= len(cmd_str):
                    red_str = cmd_str[start:end].strip()
                    if red_str not in redirections:
                        redirections.append(red_str)

            cmd = cls._build_command_from_words(words, env_vars, wrappers, redirections)
            if cmd:
                cmd.operator_after = op_after
            return cmd

        def walk_ast(node):
            if node.kind == "command":
                cmd = extract_command_node(node)
                if cmd:
                    commands.append(cmd)
            elif node.kind == "pipeline":
                parts = getattr(node, "parts", [])
                cmd_parts = [p for p in parts if p.kind == "command"]
                for i, p in enumerate(cmd_parts):
                    op = "|" if i < len(cmd_parts) - 1 else None
                    cmd = extract_command_node(p, op_after=op)
                    if cmd:
                        commands.append(cmd)
            elif node.kind == "list":
                parts = getattr(node, "parts", [])
                i = 0
                while i < len(parts):
                    p = parts[i]
                    op_after = None
                    if i + 1 < len(parts) and parts[i + 1].kind == "operator":
                        op_node = parts[i + 1]
                        op_start, op_end = getattr(op_node, "pos", (0, 0))
                        op_after = cmd_str[op_start:op_end].strip()
                        i += 1  # Skip operator node

                    if p.kind == "command":
                        cmd = extract_command_node(p, op_after=op_after)
                        if cmd:
                            commands.append(cmd)
                    elif p.kind == "pipeline":
                        pipeline_parts = [sub for sub in getattr(p, "parts", []) if sub.kind == "command"]
                        for j, sub in enumerate(pipeline_parts):
                            sub_op = "|" if j < len(pipeline_parts) - 1 else op_after
                            cmd = extract_command_node(sub, op_after=sub_op)
                            if cmd:
                                commands.append(cmd)
                    else:
                        walk_ast(p)
                    i += 1
            elif hasattr(node, "parts"):
                for p in node.parts:
                    walk_ast(p)

        for root in tree:
            walk_ast(root)

        return commands if commands else None

    # ----------------------------------------------------------------------
    # 2. Fallback Engine: shlex Lexical Scanner
    # ----------------------------------------------------------------------
    @classmethod
    def _parse_with_shlex(cls, cmd_str: str) -> List[ParsedCommand]:
        normalized_cmd = cmd_str.replace("\r\n", "\n").replace("\n", " ; ")
        try:
            lexer = shlex.shlex(normalized_cmd, posix=True, punctuation_chars="|&;><")
            lexer.whitespace_split = True
            raw_tokens = list(lexer)
        except Exception:
            raw_tokens = normalized_cmd.split()

        commands: List[ParsedCommand] = []
        current_chunk: List[str] = []

        def flush_chunk(op: Optional[str]):
            if not current_chunk:
                return
            env_vars: Dict[str, str] = {}
            wrappers: List[str] = []
            redirections: List[str] = []
            words: List[str] = []

            i = 0
            while i < len(current_chunk):
                tok = current_chunk[i]
                # Redirection operators
                if tok in (">", ">>", "<", ">&", "<&", "&>", "2>", "2>>", "1>", "1>>"):
                    target = current_chunk[i + 1] if i + 1 < len(current_chunk) else ""
                    redirections.append(f"{tok} {target}".strip())
                    i += 2
                    continue
                elif i + 2 < len(current_chunk) and tok in ("1", "2") and current_chunk[i + 1] in (">", ">&", ">>"):
                    redirections.append(f"{tok}{current_chunk[i+1]}{current_chunk[i+2]}")
                    i += 3
                    continue
                elif tok == "2>&1":
                    redirections.append(tok)
                    i += 1
                    continue
                # Environment variables
                elif not words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=.*", tok):
                    k, v = tok.split("=", 1)
                    env_vars[k] = v
                    i += 1
                    continue
                else:
                    words.append(tok)
                    i += 1

            cmd = cls._build_command_from_words(words, env_vars, wrappers, redirections)
            if cmd:
                cmd.operator_after = op
                commands.append(cmd)

        for tok in raw_tokens:
            if tok in ("&&", "||", ";", "|", "|&"):
                flush_chunk(tok)
                current_chunk = []
            else:
                current_chunk.append(tok)

        if current_chunk:
            flush_chunk(None)

        return commands

    # ----------------------------------------------------------------------
    # 3. Unified Entry Point
    # ----------------------------------------------------------------------
    @classmethod
    def parse(cls, cmd_str: str, force_engine: Optional[str] = None) -> ParsedPipeline:
        """
        Parses a shell command string into a ParsedPipeline.
        Attempts bashlex first; falls back to shlex on syntax errors or missing library.
        """
        if not cls.is_bash_environment(cmd_str):
            return ParsedPipeline(
                raw_command=cmd_str,
                is_bash=False,
                parser_engine="passthrough",
            )

        engine_used = "bashlex"
        commands: Optional[List[ParsedCommand]] = None

        if force_engine != "shlex":
            commands = cls._parse_with_bashlex(cmd_str)

        if not commands or force_engine == "shlex":
            commands = cls._parse_with_shlex(cmd_str)
            engine_used = "shlex"

        binaries = [c.binary for c in commands if c.binary]
        subcommands = [c.subcommand for c in commands if c.subcommand]
        signatures = [c.signature for c in commands if c.signature]
        combined_env: Dict[str, str] = {}
        has_flag: Dict[str, bool] = {}
        for c in commands:
            combined_env.update(c.env_vars)
            for f in c.flags:
                has_flag[f] = True

        has_binary = {b: True for b in binaries}
        has_subcommand = {s: True for s in subcommands}
        has_signature = {sig: True for sig in signatures}

        return ParsedPipeline(
            raw_command=cmd_str,
            is_bash=True,
            parser_engine=engine_used,
            commands=commands,
            binaries=binaries,
            subcommands=subcommands,
            signatures=signatures,
            env_vars=combined_env,
            has_binary=has_binary,
            has_subcommand=has_subcommand,
            has_signature=has_signature,
            has_flag=has_flag,
        )
