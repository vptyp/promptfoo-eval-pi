# Unit tests for isolation.py
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from isolation import WorkspaceIsolation, get_git_root


class TestWorkspaceIsolation(unittest.TestCase):
    def setUp(self):
        # Create a temporary git repo for testing
        self.test_dir = tempfile.TemporaryDirectory()
        self.repo_path = pathlib.Path(self.test_dir.name)
        subprocess.run(["git", "-C", str(self.repo_path), "init"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo_path), "config", "user.name", "Test"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo_path), "config", "user.email", "test@test.com"], check=True, capture_output=True)

        # Create an initial file and commit
        self.readme = self.repo_path / "README.md"
        self.readme.write_text("Initial content\n")
        subprocess.run(["git", "-C", str(self.repo_path), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo_path), "commit", "-m", "Initial commit"], check=True, capture_output=True)

    def tearDown(self):
        self.test_dir.cleanup()

    def test_git_worktree_isolation(self):
        with WorkspaceIsolation(self.repo_path, strategy="git-worktree", clean=True) as isolated_path:
            self.assertTrue(isolated_path.exists())
            self.assertNotEqual(isolated_path, self.repo_path)

            # Modify file in isolated workspace
            (isolated_path / "README.md").write_text("Mutated content\n")
            (isolated_path / "new_file.txt").write_text("Created in isolation\n")

            # Check original repo is untouched
            self.assertEqual(self.readme.read_text(), "Initial content\n")
            self.assertFalse((self.repo_path / "new_file.txt").exists())

        # Check isolated workdir was cleaned up
        self.assertFalse(isolated_path.exists())

    def test_copy_isolation(self):
        with WorkspaceIsolation(self.repo_path, strategy="copy", clean=True) as isolated_path:
            self.assertTrue(isolated_path.exists())
            (isolated_path / "README.md").write_text("Copy mutation\n")
            self.assertEqual(self.readme.read_text(), "Initial content\n")

        self.assertFalse(isolated_path.exists())

    def test_in_place_isolation(self):
        with WorkspaceIsolation(self.repo_path, strategy="in-place", clean=True) as isolated_path:
            self.assertEqual(isolated_path, self.repo_path)
            self.readme.write_text("In-place mutation\n")
            (self.repo_path / "untracked.txt").write_text("Untracked\n")

        # After exit, in-place changes should be reverted and cleaned
        self.assertEqual(self.readme.read_text(), "Initial content\n")
        self.assertFalse((self.repo_path / "untracked.txt").exists())

    def test_capture_workspace_diff(self):
        from pi_provider import capture_workspace_diff

        with WorkspaceIsolation(self.repo_path, strategy="git-worktree", clean=True) as isolated_path:
            # Modify tracked file
            (isolated_path / "README.md").write_text("Modified content\n")
            # Create new untracked file
            (isolated_path / "new_module.py").write_text("def test(): pass\n")

            diff = capture_workspace_diff(isolated_path)
            self.assertIn("Modified content", diff)
            self.assertIn("new_module.py", diff)
            self.assertIn("def test(): pass", diff)


if __name__ == "__main__":
    unittest.main()
