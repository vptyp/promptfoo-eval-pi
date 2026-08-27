# Copyright 2026 prompt-eval authors.
# Inspired by Chromium's agents/testing/checkout_helpers.py and workers.py.
"""
Workspace Isolation Helpers for Test-to-Test Isolation.

Provides ephemeral working directories for destructive agent tasks (file edits,
code refactoring, build mutations) ensuring the source checkout remains untainted.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
from typing import Optional

logger = logging.getLogger("promptfoo.isolation")


@functools.cache
def get_git_root(path: pathlib.Path | str) -> Optional[pathlib.Path]:
    """Finds the root directory of the git repository containing path."""
    try:
        res = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
        return pathlib.Path(res.stdout.strip()).resolve()
    except Exception:
        return None


@functools.cache
def check_btrfs(path: pathlib.Path | str) -> bool:
    """Checks if the given path is on a Btrfs filesystem partition."""
    try:
        res = subprocess.run(
            ["stat", "-f", "-c", "%T", str(path)],
            capture_output=True,
            text=True,
            check=True,
        )
        return "btrfs" in res.stdout.strip().lower()
    except Exception:
        return False


class WorkspaceIsolation(contextlib.AbstractContextManager):
    """
    Context manager for running destructive evaluation tasks in an isolated checkout.

    Strategies:
      - 'git-worktree' (Default): Creates a detached git worktree sharing git object storage. Instant (<50ms).
      - 'copy' / 'reflink': Copies source directory using copy-on-write reflinks when supported.
      - 'in-place': Tests in the current directory; stashes uncommitted changes on entry and resets on exit.
      - 'btrfs': Creates a copy-on-write Btrfs subvolume snapshot.
    """

    def __init__(
        self,
        src_dir: pathlib.Path | str,
        strategy: str = "git-worktree",
        clean: bool = True,
        workdir_parent: Optional[pathlib.Path | str] = None,
        name_prefix: str = "workdir",
    ):
        self.src_dir = pathlib.Path(src_dir).resolve()
        self.strategy = strategy.lower().strip()
        self.clean = clean
        self.workdir_parent = (
            pathlib.Path(workdir_parent).resolve()
            if workdir_parent
            else pathlib.Path(tempfile.gettempdir())
        )
        self.name_prefix = name_prefix
        self.isolated_path: Optional[pathlib.Path] = None
        self._stashed: bool = False
        self._git_root: Optional[pathlib.Path] = None

    def __enter__(self) -> pathlib.Path:
        start_time = time.perf_counter()

        if self.strategy == "in-place":
            self.isolated_path = self.src_dir
            self._git_root = get_git_root(self.src_dir)
            if self._git_root:
                # Stash any pre-existing uncommitted changes
                stash_msg = f"promptfoo-isolation-{int(time.time() * 1000)}"
                res = subprocess.run(
                    ["git", "-C", str(self._git_root), "stash", "push", "-u", "-m", stash_msg],
                    capture_output=True,
                    text=True,
                )
                if "No local changes to save" not in res.stdout and "Saved working directory" in res.stdout:
                    self._stashed = True
            logger.debug(f"[Isolation] In-place setup took {time.perf_counter() - start_time:.3f}s")
            return self.isolated_path

        # Generate unique workdir path
        unique_name = f"{self.name_prefix}-{os.getpid()}-{int(time.time() * 1000)}"
        self.isolated_path = self.workdir_parent / unique_name

        if self.strategy == "git-worktree":
            self._git_root = get_git_root(self.src_dir)
            if not self._git_root:
                logger.warning(
                    f"[Isolation] '{self.src_dir}' is not a git repository. Falling back to 'copy' strategy."
                )
                self.strategy = "copy"
            else:
                rel_path = self.src_dir.relative_to(self._git_root)
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(self._git_root),
                        "worktree",
                        "add",
                        "--detach",
                        str(self.isolated_path),
                        "HEAD",
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                logger.debug(
                    f"[Isolation] Created git worktree at {self.isolated_path} in {time.perf_counter() - start_time:.3f}s"
                )
                return self.isolated_path / rel_path

        if self.strategy == "btrfs":
            if check_btrfs(self.src_dir):
                subprocess.run(
                    ["btrfs", "subvolume", "snapshot", str(self.src_dir), str(self.isolated_path)],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                logger.debug(
                    f"[Isolation] Created Btrfs snapshot at {self.isolated_path} in {time.perf_counter() - start_time:.3f}s"
                )
                return self.isolated_path
            logger.warning("[Isolation] Filesystem is not Btrfs. Falling back to 'copy' strategy.")
            self.strategy = "copy"

        # Fallback / 'copy' strategy with reflink (copy-on-write) support
        self.isolated_path.mkdir(parents=True, exist_ok=True)
        # Try cp -a --reflink=auto first for fast copy-on-write on supported filesystems
        cp_res = subprocess.run(
            ["cp", "-a", "--reflink=auto", f"{self.src_dir}/.", str(self.isolated_path)],
            capture_output=True,
            text=True,
        )
        if cp_res.returncode != 0:
            # Fallback to standard shutil copy
            shutil.copytree(self.src_dir, self.isolated_path, dirs_exist_ok=True)

        logger.debug(
            f"[Isolation] Created directory copy at {self.isolated_path} in {time.perf_counter() - start_time:.3f}s"
        )
        return self.isolated_path

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if not self.clean:
            return

        start_time = time.perf_counter()

        if self.strategy == "in-place":
            if self._git_root:
                # Reset hard and clean untracked files
                subprocess.run(
                    ["git", "-C", str(self._git_root), "reset", "--hard", "HEAD"],
                    capture_output=True,
                    text=True,
                )
                subprocess.run(
                    ["git", "-C", str(self._git_root), "clean", "-fd"],
                    capture_output=True,
                    text=True,
                )
                if self._stashed:
                    subprocess.run(
                        ["git", "-C", str(self._git_root), "stash", "pop"],
                        capture_output=True,
                        text=True,
                    )
            logger.debug(f"[Isolation] In-place cleanup took {time.perf_counter() - start_time:.3f}s")
            return

        if not self.isolated_path or not self.isolated_path.exists():
            return

        if self.strategy == "git-worktree" and self._git_root:
            try:
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(self._git_root),
                        "worktree",
                        "remove",
                        "--force",
                        str(self.isolated_path),
                    ],
                    capture_output=True,
                    text=True,
                )
                subprocess.run(
                    ["git", "-C", str(self._git_root), "worktree", "prune"],
                    capture_output=True,
                    text=True,
                )
            except Exception as e:
                logger.warning(f"[Isolation] Failed to remove git worktree: {e}")
                shutil.rmtree(self.isolated_path, ignore_errors=True)
        elif self.strategy == "btrfs":
            try:
                subprocess.run(
                    ["btrfs", "subvolume", "delete", str(self.isolated_path)],
                    capture_output=True,
                    text=True,
                )
            except Exception:
                shutil.rmtree(self.isolated_path, ignore_errors=True)
        else:
            shutil.rmtree(self.isolated_path, ignore_errors=True)

        logger.debug(f"[Isolation] Teardown took {time.perf_counter() - start_time:.3f}s")
