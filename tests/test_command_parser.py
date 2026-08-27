#!/usr/bin/env python3
"""
Unit tests for command_parser.py.
Covers multi-command parsing, stream redirections, pipes, flags + subcommand + args,
environment variables, wrappers, and non-bash fallback detection.
"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from command_parser import BashCommandParser, ParsedPipeline


class TestBashCommandParser(unittest.TestCase):

    # ----------------------------------------------------------------------
    # 1. Multi-Command Chains (&&, ||, ;, \n)
    # ----------------------------------------------------------------------
    def test_multi_command_chain_and_or_semicolon(self):
        cmd = "meson setup build && ninja -C build && meson test -C build -v || echo 'Test failed'; ./cleanup.sh"
        res = BashCommandParser.parse(cmd)

        self.assertTrue(res.is_bash)
        self.assertEqual(len(res.commands), 5)
        self.assertEqual(res.binaries, ["meson", "ninja", "meson", "echo", "./cleanup.sh"])
        self.assertEqual(res.subcommands, ["setup", "test"])
        self.assertEqual(res.signatures, ["meson setup", "ninja", "meson test", "echo", "./cleanup.sh"])

        # Check operators after each command
        self.assertEqual(res.commands[0].operator_after, "&&")
        self.assertEqual(res.commands[1].operator_after, "&&")
        self.assertEqual(res.commands[2].operator_after, "||")
        self.assertEqual(res.commands[3].operator_after, ";")
        self.assertIsNone(res.commands[4].operator_after)

    def test_newline_separated_commands(self):
        cmd = "cd /home/user/project\nmeson test -C build\ngit status"
        res = BashCommandParser.parse(cmd)

        self.assertTrue(res.is_bash)
        self.assertEqual(len(res.commands), 3)
        self.assertEqual(res.binaries, ["cd", "meson", "git"])
        self.assertEqual(res.subcommands, ["test", "status"])

    # ----------------------------------------------------------------------
    # 2. Stream Redirections (>, >>, <, 2>&1, &>, 2>)
    # ----------------------------------------------------------------------
    def test_stream_redirections_stdout_stderr(self):
        cmd = "g++ -std=c++23 src/main.cpp -o build/main > /dev/null 2>&1"
        res = BashCommandParser.parse(cmd)

        self.assertTrue(res.is_bash)
        self.assertEqual(len(res.commands), 1)
        c = res.commands[0]
        self.assertEqual(c.binary, "g++")
        self.assertIn("> /dev/null", c.redirections)
        self.assertIn("2>&1", c.redirections)

    def test_stream_redirections_append_and_input(self):
        cmd = "python3 process_data.py < input.csv >> output.log"
        res = BashCommandParser.parse(cmd)

        self.assertTrue(res.is_bash)
        c = res.commands[0]
        self.assertEqual(c.binary, "python3")
        self.assertIn("< input.csv", c.redirections)
        self.assertIn(">> output.log", c.redirections)

    # ----------------------------------------------------------------------
    # 3. Pipes (| and |&)
    # ----------------------------------------------------------------------
    def test_pipeline_streaming(self):
        cmd = "meson test -C build -v | grep 'OK' | wc -l"
        res = BashCommandParser.parse(cmd)

        self.assertTrue(res.is_bash)
        self.assertEqual(len(res.commands), 3)
        self.assertEqual(res.binaries, ["meson", "grep", "wc"])
        self.assertEqual(res.subcommands, ["test"])
        self.assertEqual(res.commands[0].operator_after, "|")
        self.assertEqual(res.commands[1].operator_after, "|")
        self.assertIsNone(res.commands[2].operator_after)

    # ----------------------------------------------------------------------
    # 4. Command flags + Subcommand + flags + args
    # ----------------------------------------------------------------------
    def test_flags_subcommand_flags_args(self):
        # Flag before subcommand (-C /path/to/repo) and flags after subcommand (-m "...")
        cmd = 'git -C /path/to/repo commit -m "feat(api): add new endpoints" --no-verify'
        res = BashCommandParser.parse(cmd)

        self.assertTrue(res.is_bash)
        self.assertEqual(len(res.commands), 1)
        c = res.commands[0]
        self.assertEqual(c.binary, "git")
        self.assertEqual(c.subcommand, "commit")
        self.assertEqual(c.signature, "git commit")
        self.assertIn("-C", c.flags)
        self.assertIn("/path/to/repo", c.flags)
        self.assertIn("-m", c.flags)
        self.assertIn("feat(api): add new endpoints", c.flags)
        self.assertIn("--no-verify", c.flags)

    def test_complex_meson_test_flags_and_filter_target(self):
        cmd = "meson test -C build -v --benchmark --timeout-multiplier 2 *CApi*"
        res = BashCommandParser.parse(cmd)

        self.assertTrue(res.is_bash)
        c = res.commands[0]
        self.assertEqual(c.binary, "meson")
        self.assertEqual(c.subcommand, "test")
        self.assertEqual(c.signature, "meson test")
        self.assertIn("-C", c.flags)
        self.assertIn("build", c.flags)
        self.assertIn("-v", c.flags)
        self.assertIn("--benchmark", c.flags)
        self.assertIn("*CApi*", c.args)

    # ----------------------------------------------------------------------
    # 5. Inline Environment Variables & Command Wrappers
    # ----------------------------------------------------------------------
    def test_inline_env_vars_and_sudo_wrapper(self):
        cmd = "sudo CC=clang CXX=clang++ BUILD_TYPE=debug meson setup build"
        res = BashCommandParser.parse(cmd)

        self.assertTrue(res.is_bash)
        c = res.commands[0]
        self.assertEqual(c.binary, "meson")
        self.assertEqual(c.subcommand, "setup")
        self.assertIn("sudo", c.wrappers)
        self.assertEqual(c.env_vars, {
            "CC": "clang",
            "CXX": "clang++",
            "BUILD_TYPE": "debug"
        })
        self.assertEqual(res.env_vars["CXX"], "clang++")

    def test_time_and_valgrind_wrappers(self):
        cmd = "time valgrind --leak-check=full ./build/unit_tests"
        res = BashCommandParser.parse(cmd)

        self.assertTrue(res.is_bash)
        c = res.commands[0]
        self.assertEqual(c.binary, "./build/unit_tests")
        self.assertIn("time", c.wrappers)
        self.assertIn("valgrind", c.wrappers)

    # ----------------------------------------------------------------------
    # 6. Predicate Helpers (has_command)
    # ----------------------------------------------------------------------
    def test_has_command_predicate_helper(self):
        cmd = "cd build && meson test -C build -v *CApi*"
        res = BashCommandParser.parse(cmd)

        self.assertTrue(res.has_command(binary="meson"))
        self.assertTrue(res.has_command(binary="meson", subcommand="test"))
        self.assertTrue(res.has_command(signature="meson test"))
        self.assertTrue(res.has_command(arg_contains="CApi"))
        self.assertFalse(res.has_command(binary="pytest"))
        self.assertFalse(res.has_command(signature="git commit"))

    # ----------------------------------------------------------------------
    # 7. Non-Bash Environment Fallback (PowerShell & Windows CMD)
    # ----------------------------------------------------------------------
    def test_powershell_cmdlets_fallback(self):
        cmd = "Get-Process | Where-Object { $_.CPU -gt 10 } | Select-Object -First 5"
        res = BashCommandParser.parse(cmd)

        self.assertFalse(res.is_bash)
        self.assertEqual(res.raw_command, cmd)
        self.assertEqual(res.commands, [])
        self.assertEqual(res.binaries, [])

    def test_powershell_service_start_fallback(self):
        cmd = 'Start-Service -Name "MyService"'
        res = BashCommandParser.parse(cmd)
        self.assertFalse(res.is_bash)

    def test_windows_cmd_fallback(self):
        cmd = "dir /s /b C:\\Users\\Project"
        res = BashCommandParser.parse(cmd)
        self.assertFalse(res.is_bash)

    # ----------------------------------------------------------------------
    # 8. Engine Parity & Fallback Verification (shlex vs bashlex)
    # ----------------------------------------------------------------------
    def test_shlex_engine_fallback_parity(self):
        cmd = "CXX=g++ git -C /repo commit -m 'feat: init' > /dev/null 2>&1 && meson test -C build -v"
        
        # Test with primary engine (bashlex)
        res_bashlex = BashCommandParser.parse(cmd, force_engine="bashlex")
        self.assertEqual(res_bashlex.parser_engine, "bashlex")
        self.assertEqual(res_bashlex.binaries, ["git", "meson"])
        self.assertEqual(res_bashlex.subcommands, ["commit", "test"])
        self.assertEqual(res_bashlex.signatures, ["git commit", "meson test"])

        # Test with fallback engine (shlex)
        res_shlex = BashCommandParser.parse(cmd, force_engine="shlex")
        self.assertEqual(res_shlex.parser_engine, "shlex")
        self.assertEqual(res_shlex.binaries, ["git", "meson"])
        self.assertEqual(res_shlex.subcommands, ["commit", "test"])
        self.assertEqual(res_shlex.signatures, ["git commit", "meson test"])
        self.assertEqual(res_shlex.env_vars["CXX"], "g++")

    # ----------------------------------------------------------------------
    # 9. Promptfoo-Native Partial Map Assertions (has_binary, has_signature)
    # ----------------------------------------------------------------------
    def test_has_maps_for_promptfoo_matching(self):
        cmd = "cd build && meson test -C build -v *CApi* > test.log 2>&1"
        res = BashCommandParser.parse(cmd)

        self.assertTrue(res.has_binary.get("meson"))
        self.assertTrue(res.has_binary.get("cd"))
        self.assertFalse(res.has_binary.get("ninja", False))

        self.assertTrue(res.has_subcommand.get("test"))
        self.assertFalse(res.has_subcommand.get("commit", False))

        self.assertTrue(res.has_signature.get("meson test"))
        self.assertTrue(res.has_signature.get("cd"))
        self.assertFalse(res.has_signature.get("git commit", False))

        self.assertTrue(res.has_flag.get("-C"))
        self.assertTrue(res.has_flag.get("-v"))


if __name__ == "__main__":
    unittest.main()
