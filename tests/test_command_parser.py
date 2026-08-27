#!/usr/bin/env python3
"""
Unit tests for command_parser.py.
Covers multi-command parsing, stream redirections, pipes, flags and generic words,
environment variables, wrappers, and non-bash fallback detection.
"""

import json
import pathlib
import subprocess
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from command_parser import BashCommandParser


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
        self.assertTrue(res.has_word.get("setup"))
        self.assertTrue(res.has_word.get("test"))

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
        self.assertTrue(res.has_word.get("test"))
        self.assertTrue(res.has_word.get("status"))

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
        self.assertTrue(res.has_word.get("test"))
        self.assertEqual(res.commands[0].operator_after, "|")
        self.assertEqual(res.commands[1].operator_after, "|")
        self.assertIsNone(res.commands[2].operator_after)

    def test_stderr_pipeline_operator_preserved_by_both_engines(self):
        cmd = "meson test |& grep failure"

        for engine in ("bashlex", "shlex"):
            with self.subTest(engine=engine):
                res = BashCommandParser.parse(cmd, force_engine=engine)
                self.assertEqual(res.commands[0].operator_after, "|&")
                self.assertEqual(res.binaries, ["meson", "grep"])

    # ----------------------------------------------------------------------
    # 4. Command flags + generic words
    # ----------------------------------------------------------------------
    def test_flags_and_words_without_cli_specific_inference(self):
        cmd = 'git -C /path/to/repo commit -m "feat(api): add new endpoints" --no-verify'
        res = BashCommandParser.parse(cmd)

        self.assertTrue(res.is_bash)
        self.assertEqual(len(res.commands), 1)
        c = res.commands[0]
        self.assertEqual(c.binary, "git")
        self.assertIn("-C", c.flags)
        self.assertIn("-m", c.flags)
        self.assertIn("--no-verify", c.flags)
        self.assertEqual(
            c.words,
            ["/path/to/repo", "commit", "feat(api): add new endpoints"],
        )

    def test_complex_meson_test_flags_and_filter_target(self):
        cmd = "meson test -C build -v --benchmark --timeout-multiplier 2 *CApi*"
        res = BashCommandParser.parse(cmd)

        self.assertTrue(res.is_bash)
        c = res.commands[0]
        self.assertEqual(c.binary, "meson")
        self.assertIn("-C", c.flags)
        self.assertIn("-v", c.flags)
        self.assertIn("--benchmark", c.flags)
        self.assertIn("test", c.words)
        self.assertIn("build", c.words)
        self.assertIn("*CApi*", c.words)

    def test_executable_flag_value_and_positional_arguments(self):
        cmd = './cli.py --timeout 10 analyze "Something"'

        for engine in ("bashlex", "shlex"):
            with self.subTest(engine=engine):
                res = BashCommandParser.parse(cmd, force_engine=engine)
                c = res.commands[0]
                self.assertEqual(c.binary, "./cli.py")
                self.assertEqual(c.flags, ["--timeout"])
                self.assertEqual(c.words, ["10", "analyze", "Something"])

    def test_schema_aware_cli_and_generic_parser_views(self):
        cmd = './cli.py analyze --timeout=10 "Something"'
        fixture_dir = pathlib.Path(__file__).resolve().parent / "fixtures"

        completed = subprocess.run(
            ["./cli.py", "analyze", "--timeout=10", "Something"],
            cwd=fixture_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            json.loads(completed.stdout),
            {"command": "analyze", "subject": "Something", "timeout": 10},
        )

        for engine in ("bashlex", "shlex"):
            with self.subTest(engine=engine):
                res = BashCommandParser.parse(cmd, force_engine=engine)
                c = res.commands[0]
                self.assertEqual(c.binary, "./cli.py")
                self.assertEqual(c.flags, ["--timeout=10"])
                self.assertEqual(c.words, ["analyze", "Something"])

    def test_command_parser_overview_cli(self):
        command = './cli.py analyze --timeout=10 "Something" && echo done'
        overview_cli = pathlib.Path(__file__).resolve().parent / "command_parser_overview.py"

        completed = subprocess.run(
            [str(overview_cli), "--engine", "shlex", command],
            check=True,
            capture_output=True,
            text=True,
        )
        overview = json.loads(completed.stdout)

        self.assertEqual(overview["parser_engine"], "shlex")
        self.assertEqual(overview["binaries"], ["./cli.py", "echo"])
        self.assertEqual(overview["words"], ["analyze", "Something", "done"])
        self.assertTrue(overview["has_flag"]["--timeout"])
        self.assertEqual(overview["commands"][0]["operator_after"], "&&")

    def test_words_do_not_depend_on_subcommand_knowledge(self):
        cases = {
            "./cli.py analyze payload": ("./cli.py", ["analyze", "payload"]),
            "meson test project": ("meson", ["test", "project"]),
            "git status": ("git", ["status"]),
        }

        for cmd, (binary, words) in cases.items():
            with self.subTest(cmd=cmd):
                res = BashCommandParser.parse(cmd)
                c = res.commands[0]
                self.assertEqual(c.binary, binary)
                self.assertEqual(c.words, words)
                for word in words:
                    self.assertTrue(res.has_word.get(word))

    def test_long_flag_name_matches_equal_and_separated_forms(self):
        commands = (
            ('./cli.py analyze --timeout=10 "Something"', "--timeout"),
            ('./cli.py analyze --timeout 10 "Something"', "--timeout"),
            ('./cli.py analyze -t 10 "Something"', "-t"),
        )

        for cmd, expected_flag in commands:
            with self.subTest(cmd=cmd):
                res = BashCommandParser.parse(cmd)
                self.assertTrue(res.has_flag.get(expected_flag))

    # ----------------------------------------------------------------------
    # 5. Inline Environment Variables & Command Wrappers
    # ----------------------------------------------------------------------
    def test_inline_env_vars_and_sudo_wrapper(self):
        cmd = "sudo CC=clang CXX=clang++ BUILD_TYPE=debug meson setup build"
        res = BashCommandParser.parse(cmd)

        self.assertTrue(res.is_bash)
        c = res.commands[0]
        self.assertEqual(c.binary, "meson")
        self.assertEqual(c.words, ["setup", "build"])
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

    def test_wrapper_options_with_separate_values(self):
        cases = {
            "strace -o trace.log ./program": ("./program", ["strace", "-o", "trace.log"]),
            "xargs -I '{}' echo '{}'": ("echo", ["xargs", "-I", "{}"]),
            "sudo -n git status": ("git", ["sudo", "-n"]),
        }

        for cmd, (binary, wrappers) in cases.items():
            with self.subTest(cmd=cmd):
                res = BashCommandParser.parse(cmd)
                self.assertEqual(res.commands[0].binary, binary)
                self.assertEqual(res.commands[0].wrappers, wrappers)

    def test_python_and_pytest_positionals_are_arguments(self):
        cases = {
            "pytest tests/test_command_parser.py -q": "tests/test_command_parser.py",
            "python script.py --verbose": "script.py",
            "python3 script.py --verbose": "script.py",
        }

        for cmd, expected_arg in cases.items():
            with self.subTest(cmd=cmd):
                res = BashCommandParser.parse(cmd)
                c = res.commands[0]
                self.assertIn(expected_arg, c.words)

    # ----------------------------------------------------------------------
    # 6. Predicate Helpers (has_command)
    # ----------------------------------------------------------------------
    def test_has_command_predicate_helper(self):
        cmd = "cd build && meson test -C build -v *CApi*"
        res = BashCommandParser.parse(cmd)

        self.assertTrue(res.has_command(binary="meson"))
        self.assertTrue(res.has_command(binary="meson", word="test"))
        self.assertTrue(res.has_command(arg_contains="CApi"))
        self.assertFalse(res.has_command(binary="pytest"))
        self.assertFalse(res.has_command(binary="git", word="commit"))

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

    def test_bash_arguments_that_mention_powershell_are_not_passthrough(self):
        cases = {
            "printf '%s\\n' powershell": "printf",
            "printf 'Get-Process\\n'": "printf",
        }

        for cmd, binary in cases.items():
            with self.subTest(cmd=cmd):
                res = BashCommandParser.parse(cmd)
                self.assertTrue(res.is_bash)
                self.assertEqual(res.binaries, [binary])

    def test_git_dir_option_is_not_misclassified_as_windows_cmd(self):
        cmd = "git --git-dir /tmp/repo/.git status"
        res = BashCommandParser.parse(cmd)

        self.assertTrue(res.is_bash)
        self.assertEqual(res.binaries, ["git"])
        self.assertTrue(res.has_word.get("status"))
        self.assertIn("--git-dir", res.commands[0].flags)
        self.assertIn("/tmp/repo/.git", res.commands[0].words)

    # ----------------------------------------------------------------------
    # 8. Engine Parity & Fallback Verification (shlex vs bashlex)
    # ----------------------------------------------------------------------
    def test_shlex_engine_fallback_parity(self):
        cmd = "CXX=g++ git -C /repo commit -m 'feat: init' > /dev/null 2>&1 && meson test -C build -v"
        
        # Test with primary engine (bashlex)
        res_bashlex = BashCommandParser.parse(cmd, force_engine="bashlex")
        self.assertEqual(res_bashlex.parser_engine, "bashlex")
        self.assertEqual(res_bashlex.binaries, ["git", "meson"])
        self.assertTrue(res_bashlex.has_word.get("commit"))
        self.assertTrue(res_bashlex.has_word.get("test"))

        # Test with fallback engine (shlex)
        res_shlex = BashCommandParser.parse(cmd, force_engine="shlex")
        self.assertEqual(res_shlex.parser_engine, "shlex")
        self.assertEqual(res_shlex.binaries, ["git", "meson"])
        self.assertTrue(res_shlex.has_word.get("commit"))
        self.assertTrue(res_shlex.has_word.get("test"))
        self.assertEqual(res_shlex.env_vars["CXX"], "g++")

    # ----------------------------------------------------------------------
    # 9. Promptfoo-Native Partial Map Assertions (has_binary, has_word)
    # ----------------------------------------------------------------------
    def test_has_maps_for_promptfoo_matching(self):
        cmd = "cd build && meson test -C build -v *CApi* > test.log 2>&1"
        res = BashCommandParser.parse(cmd)

        self.assertTrue(res.has_binary.get("meson"))
        self.assertTrue(res.has_binary.get("cd"))
        self.assertFalse(res.has_binary.get("ninja", False))

        self.assertTrue(res.has_word.get("test"))
        self.assertTrue(res.has_word.get("build"))
        self.assertTrue(res.has_word.get("*CApi*"))
        self.assertFalse(res.has_word.get("commit", False))

        self.assertTrue(res.has_flag.get("-C"))
        self.assertTrue(res.has_flag.get("-v"))


if __name__ == "__main__":
    unittest.main()
