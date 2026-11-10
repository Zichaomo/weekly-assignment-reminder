"""Keep machine-local agent instructions and delegation transcripts private."""
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class RepositoryHygieneTests(unittest.TestCase):
    def test_runtime_credentials_and_diagnostic_files_are_ignored(self):
        paths = ['debug.log', 'data/config.json', 'data/storage_state.json',
                 'data/settings-fixture.tmp', 'data/browser-profile/Local State',
                 '.local-state/runtime-migration/task.xml',
                 'tests/artifacts/management-menu/menu-guide-centered.png']
        result = subprocess.run(
            ['git', 'check-ignore', '--no-index', '-z', '--stdin'],
            input='\0'.join(paths) + '\0', capture_output=True, text=True,
            encoding='utf-8', cwd=ROOT, timeout=10,
        )
        self.assertIn(result.returncode, (0, 1), result.stderr)
        self.assertEqual(set(result.stdout.strip('\0').split('\0')), set(paths))

    def test_local_agent_files_are_ignored_even_when_previously_tracked(self):
        paths = [
            'AGENTS.md',
            'AGENTS--old.md',
            'data/AGENTS.md',
            '.deepastra-test/task.txt',
            '.deepastra-test/logs/session.jsonl',
            '.deepastra-work/session.json',
        ]
        result = subprocess.run(
            ['git', 'check-ignore', '--no-index', '-z', '--stdin'],
            input='\0'.join(paths) + '\0', capture_output=True, text=True, encoding='utf-8',
            cwd=ROOT, timeout=10,
        )
        self.assertIn(result.returncode, (0, 1), result.stderr)
        self.assertEqual(set(result.stdout.strip('\0').split('\0')), set(paths))

    def test_local_agent_files_are_not_tracked(self):
        result = subprocess.run(
            ['git', 'ls-files', '-z'], capture_output=True, text=True, encoding='utf-8',
            cwd=ROOT, timeout=10, check=True,
        )
        private = [
            name for name in result.stdout.split('\0') if name and (
                Path(name).name.startswith('AGENTS') and name.endswith('.md')
                or any(part.startswith('.deepastra') for part in Path(name).parts)
            )
        ]
        self.assertEqual(private, [], 'Local agent files must be removed from the Git index')


if __name__ == '__main__':
    unittest.main()
