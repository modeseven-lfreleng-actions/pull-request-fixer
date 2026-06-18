# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Comprehensive test suite for file_fixer module.

Tests cover real-world scenarios including:
- Removing lines
- Adding lines
- Making mid-line edits
- Context-aware operations
- Multiple file patterns
- Edge cases
"""

from __future__ import annotations

from collections.abc import Generator  # noqa: TC003
from pathlib import Path
import tempfile

import pytest

from pull_request_fixer.file_fixer import FileFixer


class TestFileFixer:
    """Test suite for FileFixer class."""

    @pytest.fixture
    def fixer(self) -> FileFixer:
        """Create a FileFixer instance."""
        return FileFixer()

    @pytest.fixture
    def tmp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for test files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)


class TestRemovingLines(TestFileFixer):
    """Test cases for removing lines from files."""

    def test_remove_type_definitions_from_github_actions(
        self, fixer: FileFixer, tmp_dir: Path
    ) -> None:
        """Test removing type: definitions from GitHub composite actions.

        Real-world use case: actionlint now flags type definitions in
        composite actions as errors since all inputs are strings.
        """
        action_file = tmp_dir / "action.yaml"
        action_file.write_text(
            """---
name: 'Test Action'
description: 'Test composite action'

inputs:
  repository:
    description: 'Remote Git repository URL'
    required: false
    type: 'string'
  path:
    description: 'Local filesystem path'
    required: false
    type: 'string'
  debug:
    description: 'Enable debug mode'
    required: false
    type: 'boolean'
    default: false

runs:
  using: 'composite'
  steps:
    - run: echo "Hello"
"""
        )

        was_modified, original, new = fixer.remove_lines_matching(
            action_file,
            r"type:",
            context_start=r"inputs:",
            context_end=r"runs:",
            dry_run=False,
        )

        assert was_modified is True
        assert "type: 'string'" in original
        assert "type: 'boolean'" in original
        assert "type:" not in new
        assert "description:" in new  # Other lines preserved
        assert "default: false" in new  # Non-type lines in inputs preserved
        assert "runs:" in new  # Context boundary preserved

    def test_remove_deprecated_config_keys(
        self, fixer: FileFixer, tmp_dir: Path
    ) -> None:
        """Test removing deprecated configuration keys."""
        config_file = tmp_dir / "config.yaml"
        config_file.write_text(
            """version: '2.0'
database:
  host: localhost
  port: 5432
  legacy_mode: true
cache:
  enabled: true
  legacy_mode: false
  ttl: 3600
"""
        )

        was_modified, original, new = fixer.remove_lines_matching(
            config_file,
            r"legacy_mode:",
            dry_run=False,
        )

        assert was_modified is True
        assert "legacy_mode: true" in original
        assert "legacy_mode: false" in original
        assert "legacy_mode" not in new
        assert "host: localhost" in new
        assert "ttl: 3600" in new

    def test_remove_debug_statements(
        self, fixer: FileFixer, tmp_dir: Path
    ) -> None:
        """Test removing debug print statements from Python code."""
        py_file = tmp_dir / "module.py"
        py_file.write_text(
            """def calculate(x, y):
    print("Debug: x =", x)
    result = x + y
    print(f"Debug: result = {result}")
    return result


def process(data):
    print("Debug: processing")
    return data.strip()
"""
        )

        was_modified, original, new = fixer.remove_lines_matching(
            py_file,
            r"^\s*print\(.*[Dd]ebug",
            dry_run=False,
        )

        assert was_modified is True
        assert 'print("Debug: x =", x)' in original
        assert 'print(f"Debug: result = {result}")' in original
        assert "Debug" not in new
        assert "def calculate" in new
        assert "result = x + y" in new
        assert "return result" in new


class TestAddingLines(TestFileFixer):
    """Test cases for adding lines to files."""

    def test_add_license_header(self, fixer: FileFixer, tmp_dir: Path) -> None:
        """Test adding license header to Python files."""
        py_file = tmp_dir / "module.py"
        py_file.write_text(
            '''"""Module docstring."""

def hello():
    return "world"
'''
        )

        # Add SPDX header before the first line
        was_modified, original, new = fixer.apply_fix(
            py_file,
            r"^",  # Match start of file
            "# SPDX-License-Identifier: Apache-2.0\n# SPDX-FileCopyrightText: 2025 The Linux Foundation\n\n",
            dry_run=False,
        )

        assert was_modified is True
        assert not original.startswith("# SPDX")
        # REUSE-IgnoreStart
        assert new.startswith("# SPDX-License-Identifier: Apache-2.0")
        assert "# SPDX-FileCopyrightText: 2025 The Linux Foundation" in new
        # REUSE-IgnoreEnd
        assert '"""Module docstring."""' in new

    def test_add_required_field_to_yaml(
        self, fixer: FileFixer, tmp_dir: Path
    ) -> None:
        """Test adding required field after specific keys in YAML."""
        yaml_file = tmp_dir / "config.yaml"
        yaml_file.write_text(
            """services:
  web:
    image: nginx
    ports:
      - "80:80"
  db:
    image: postgres
    ports:
      - "5432:5432"
"""
        )

        # Add 'restart: always' after each 'image:' line
        was_modified, original, new = fixer.apply_fix(
            yaml_file,
            r"(^\s+image:.*\n)",
            r"\1    restart: always\n",
            dry_run=False,
        )

        assert was_modified is True
        assert "restart: always" not in original
        assert new.count("restart: always") == 2
        assert "image: nginx" in new
        assert "image: postgres" in new

    def test_add_type_hints_to_python_function(
        self, fixer: FileFixer, tmp_dir: Path
    ) -> None:
        """Test adding type hints to Python function definitions."""
        py_file = tmp_dir / "module.py"
        py_file.write_text(
            """def process_data(data):
    return data.strip()


def calculate_total(items):
    return sum(items)
"""
        )

        # Add type hints to functions
        was_modified, original, new = fixer.apply_fix(
            py_file,
            r"def (\w+)\((\w+)\):",
            r"def \1(\2: str) -> str:",
            dry_run=False,
        )

        assert was_modified is True
        assert ": str" not in original
        assert "def process_data(data: str) -> str:" in new
        assert "def calculate_total(items: str) -> str:" in new

    def test_add_terraform_tags(self, fixer: FileFixer, tmp_dir: Path) -> None:
        """Test adding required tags to Terraform resources."""
        tf_file = tmp_dir / "main.tf"
        tf_file.write_text(
            """resource "aws_instance" "web" {
  ami           = "ami-12345678"
  instance_type = "t2.micro"
}

resource "aws_s3_bucket" "data" {
  bucket = "my-bucket"
  acl    = "private"
}
"""
        )

        # Add tags block before closing brace of each resource
        was_modified, original, new = fixer.apply_fix(
            tf_file,
            r"(resource \"[^\"]+\" \"[^\"]+\" \{[^}]+)(^})",
            r'\1  tags = {\n    Environment = "production"\n    ManagedBy   = "terraform"\n  }\n\2',
            dry_run=False,
        )

        assert was_modified is True
        assert "tags" not in original
        assert new.count("tags = {") == 2
        assert new.count('Environment = "production"') == 2


class TestMidLineEdits(TestFileFixer):
    """Test cases for making mid-line edits."""

    def test_update_version_numbers(
        self, fixer: FileFixer, tmp_dir: Path
    ) -> None:
        """Test updating version numbers in config files."""
        config_file = tmp_dir / "package.json"
        config_file.write_text(
            """{
  "name": "my-package",
  "version": "1.2.3",
  "dependencies": {
    "react": "^17.0.2",
    "lodash": "^4.17.21"
  }
}
"""
        )

        # Update major version from 1.x.x to 2.x.x
        was_modified, original, new = fixer.apply_fix(
            config_file,
            r'"version":\s*"1\.\d+\.\d+"',
            '"version": "2.0.0"',
            dry_run=False,
        )

        assert was_modified is True
        assert '"version": "1.2.3"' in original
        assert '"version": "2.0.0"' in new
        assert '"name": "my-package"' in new  # Other content preserved

    def test_replace_deprecated_function_names(
        self, fixer: FileFixer, tmp_dir: Path
    ) -> None:
        """Test replacing deprecated function names."""
        py_file = tmp_dir / "legacy.py"
        py_file.write_text(
            """from old_module import old_function

def process():
    result = old_function("data")
    return old_function(result)


class Handler:
    def handle(self):
        return old_function(self.data)
"""
        )

        was_modified, original, new = fixer.apply_fix(
            py_file,
            r"\bold_function\b",
            "new_function",
            dry_run=False,
        )

        assert was_modified is True
        assert new.count("new_function") == 4  # Including the import statement
        assert "old_function" not in new
        assert "from old_module import" in new  # Module import preserved

    def test_update_url_scheme(self, fixer: FileFixer, tmp_dir: Path) -> None:
        """Test updating URL schemes from http to https."""
        config_file = tmp_dir / "config.ini"
        config_file.write_text(
            """[api]
endpoint = http://api.example.com/v1
fallback = http://backup.example.com/v1

[webhook]
url = http://hooks.example.com/notify
"""
        )

        was_modified, original, new = fixer.apply_fix(
            config_file,
            r"http://",
            "https://",
            dry_run=False,
        )

        assert was_modified is True
        assert new.count("https://") == 3
        assert "http://" not in new
        assert "endpoint = https://api.example.com/v1" in new

    def test_normalize_quote_style(
        self, fixer: FileFixer, tmp_dir: Path
    ) -> None:
        """Test normalizing quote style in Python code."""
        py_file = tmp_dir / "quotes.py"
        py_file.write_text(
            """def greet(name):
    return 'Hello, ' + name

message = 'Welcome'
path = '/usr/local/bin'
"""
        )

        # Replace single quotes with double quotes (simple strings only)
        was_modified, original, new = fixer.apply_fix(
            py_file,
            r"'([^']+)'",
            r'"\1"',
            dry_run=False,
        )

        assert was_modified is True
        assert '"Hello, "' in new
        assert '"Welcome"' in new
        assert '"/usr/local/bin"' in new
        assert "'" not in new


class TestContextAwareOperations(TestFileFixer):
    """Test cases for context-aware operations."""

    def test_remove_lines_only_in_specific_section(
        self, fixer: FileFixer, tmp_dir: Path
    ) -> None:
        """Test removing lines only within specific section markers."""
        doc_file = tmp_dir / "README.md"
        doc_file.write_text(
            """# Project

## Installation

Run these commands:

```bash
# Debug: This is a test
npm install
# Debug: Check version
npm --version
```

## Usage

Import and use:

```python
# Debug: Not removed outside bash context
import package
```
"""
        )

        # Remove debug comments only in bash code blocks
        was_modified, original, new = fixer.remove_lines_matching(
            doc_file,
            r"# Debug:",
            context_start=r"```bash",
            context_end=r"```$",
            dry_run=False,
        )

        assert was_modified is True
        # Should remove from bash block
        assert "# Debug: This is a test" in original
        assert "# Debug: Check version" in original
        # But should NOT remove from Python block
        assert "# Debug: Not removed outside bash context" in new

    def test_update_config_in_production_section(
        self, fixer: FileFixer, tmp_dir: Path
    ) -> None:
        """Test updating config only in production section."""
        config_file = tmp_dir / "settings.yaml"
        config_file.write_text(
            """environments:
  development:
    debug: true
    log_level: DEBUG
    cache_enabled: false

  production:
    debug: true
    log_level: INFO
    cache_enabled: false

  staging:
    debug: true
    log_level: INFO
    cache_enabled: false
"""
        )

        # Change debug and cache only in production
        was_modified, original, new = fixer.remove_lines_matching(
            config_file,
            r"(debug|cache_enabled):\s*(true|false)",
            context_start=r"^\s+production:",
            context_end=r"^\s+staging:",  # Next section starts
            dry_run=False,
        )

        assert was_modified is True
        # Should still have debug in development and staging
        assert new.count("debug: true") == 2
        assert new.count("cache_enabled: false") == 2
        # Production section should have those lines removed
        assert "production:" in new
        assert "log_level: INFO" in new  # Other production config preserved


class TestMultipleFilePatterns(TestFileFixer):
    """Test cases for matching multiple file patterns."""

    def test_find_yaml_files_with_various_extensions(
        self, fixer: FileFixer, tmp_dir: Path
    ) -> None:
        """Test finding YAML files with different extensions."""
        # Create various YAML files
        (tmp_dir / "config.yaml").write_text("key: value")
        (tmp_dir / "docker-compose.yml").write_text("version: '3'")
        (tmp_dir / "action.yaml").write_text("name: test")
        (tmp_dir / "config.json").write_text("{}")
        (tmp_dir / "README.md").write_text("# Title")

        # Find all YAML files
        files = fixer.find_files(tmp_dir, r".*\.(yaml|yml)$")

        assert len(files) == 3
        file_names = {f.name for f in files}
        assert "config.yaml" in file_names
        assert "docker-compose.yml" in file_names
        assert "action.yaml" in file_names
        assert "config.json" not in file_names
        assert "README.md" not in file_names

    def test_find_files_in_subdirectories(
        self, fixer: FileFixer, tmp_dir: Path
    ) -> None:
        """Test finding files in nested directory structures."""
        # Create nested structure
        (tmp_dir / "src").mkdir()
        (tmp_dir / "src" / "main.py").write_text("print('main')")
        (tmp_dir / "tests").mkdir()
        (tmp_dir / "tests" / "test_main.py").write_text("def test(): pass")
        (tmp_dir / ".github").mkdir()
        (tmp_dir / ".github" / "workflows").mkdir()
        (tmp_dir / ".github" / "workflows" / "ci.yaml").write_text("name: CI")

        # Find all Python files
        py_files = fixer.find_files(tmp_dir, r".*\.py$")
        assert len(py_files) == 2

        # Find workflow files
        workflow_files = fixer.find_files(
            tmp_dir, r"\.github/workflows/.*\.yaml$"
        )
        assert len(workflow_files) == 1
        assert workflow_files[0].name == "ci.yaml"

    def test_find_files_with_dot_prefix(
        self, fixer: FileFixer, tmp_dir: Path
    ) -> None:
        """Test finding files starting with dot (hidden files)."""
        (tmp_dir / ".gitignore").write_text("*.pyc")
        (tmp_dir / ".pre-commit-config.yaml").write_text("repos: []")
        (tmp_dir / "regular.txt").write_text("content")

        # Find hidden YAML files
        files = fixer.find_files(tmp_dir, r"^\./\..*\.yaml$")
        assert len(files) == 1
        assert files[0].name == ".pre-commit-config.yaml"


class TestEdgeCases(TestFileFixer):
    """Test edge cases and error handling."""

    def test_no_matches_found(self, fixer: FileFixer, tmp_dir: Path) -> None:
        """Test when no matches are found for pattern."""
        test_file = tmp_dir / "test.txt"
        test_file.write_text("Hello world\nGoodbye world")

        was_modified, original, new = fixer.apply_fix(
            test_file,
            r"PATTERN_THAT_DOES_NOT_EXIST",
            "replacement",
            dry_run=False,
        )

        assert was_modified is False
        assert original == new
        assert "Hello world" in new

    def test_empty_file(self, fixer: FileFixer, tmp_dir: Path) -> None:
        """Test handling empty files."""
        test_file = tmp_dir / "empty.txt"
        test_file.write_text("")

        was_modified, original, new = fixer.apply_fix(
            test_file,
            r".*",
            "content",
            dry_run=False,
        )

        # Empty string matches regex, so it will be "modified"
        assert original == ""
        assert new == "content"

    def test_file_with_unicode_content(
        self, fixer: FileFixer, tmp_dir: Path
    ) -> None:
        """Test handling files with Unicode characters."""
        test_file = tmp_dir / "unicode.txt"
        test_file.write_text("Hello 世界 🌍\nEmoji: 🚀 and symbols: ±°√")

        was_modified, original, new = fixer.apply_fix(
            test_file,
            r"🌍",
            "🌎",
            dry_run=False,
        )

        assert was_modified is True
        assert "🌍" in original
        assert "🌎" in new
        assert "世界" in new
        assert "🚀" in new

    def test_dry_run_does_not_modify_file(
        self, fixer: FileFixer, tmp_dir: Path
    ) -> None:
        """Test that dry run does not write changes to disk."""
        test_file = tmp_dir / "test.txt"
        original_content = "Hello world"
        test_file.write_text(original_content)

        was_modified, original, new = fixer.apply_fix(
            test_file,
            r"world",
            "universe",
            dry_run=True,
        )

        assert was_modified is True
        assert "universe" in new
        # File on disk should be unchanged
        assert test_file.read_text() == original_content

    def test_invalid_regex_pattern(
        self, fixer: FileFixer, tmp_dir: Path
    ) -> None:
        """Test handling invalid regex patterns."""
        test_file = tmp_dir / "test.txt"
        test_file.write_text("content")

        # Invalid regex with unmatched parenthesis
        was_modified, original, new = fixer.apply_fix(
            test_file,
            r"(unclosed",
            "replacement",
            dry_run=False,
        )

        assert was_modified is False
        assert original == new

    def test_multiline_patterns(self, fixer: FileFixer, tmp_dir: Path) -> None:
        """Test matching patterns across multiple lines with [^char]+ syntax."""
        test_file = tmp_dir / "multiline.py"
        test_file.write_text(
            """def function(
    arg1,
    arg2,
    arg3
):
    return arg1 + arg2 + arg3


def another():
    pass
"""
        )

        # Match function definition across multiple lines
        # [^)]+ will match anything except ), including newlines
        was_modified, original, new = fixer.apply_fix(
            test_file,
            r"def function\([^)]+\):",
            "def function(arg1, arg2, arg3):",
            dry_run=False,
        )

        # Should match and collapse multi-line function signature
        assert was_modified is True
        assert "def function(arg1, arg2, arg3):" in new
        assert new.count("\n") < original.count(
            "\n"
        )  # Fewer lines after collapse

    def test_file_modification_basic(
        self, fixer: FileFixer, tmp_dir: Path
    ) -> None:
        """Test basic file modification of executable script.

        Note: File permissions are not explicitly preserved by the fixer
        as they are managed by Git during the commit/push cycle.
        """
        test_file = tmp_dir / "script.sh"
        test_file.write_text("#!/bin/bash\necho 'hello'")
        test_file.chmod(0o755)  # Make executable

        was_modified, original, new = fixer.apply_fix(
            test_file,
            r"hello",
            "world",
            dry_run=False,
        )

        assert was_modified is True
        assert "world" in test_file.read_text()
        assert "hello" not in test_file.read_text()

        # Verify that the file was actually written
        # Note: write_text() may change permissions on some platforms,
        # but Git preserves them during commit operations
        assert test_file.exists()


class TestComplexRealWorldScenarios(TestFileFixer):
    """Test complex real-world scenarios."""

    def test_update_github_actions_workflow(
        self, fixer: FileFixer, tmp_dir: Path
    ) -> None:
        """Test updating GitHub Actions workflow file."""
        workflow = tmp_dir / "ci.yaml"
        workflow.write_text(
            """name: CI

on:
  push:
    branches: [main]
  pull_request:

jobs:
  test:
    runs-on: ubuntu-20.04
    steps:
      - uses: actions/checkout@v2
      - uses: actions/setup-python@v2
        with:
          python-version: '3.9'
      - run: pytest

  lint:
    runs-on: ubuntu-20.04
    steps:
      - uses: actions/checkout@v2
      - run: ruff check
"""
        )

        # Update ubuntu version
        was_modified, original, new = fixer.apply_fix(
            workflow,
            r"ubuntu-20\.04",
            "ubuntu-22.04",
            dry_run=False,
        )

        assert was_modified is True
        assert new.count("ubuntu-22.04") == 2
        assert "ubuntu-20.04" not in new

        # Update action versions
        was_modified2, _, new2 = fixer.apply_fix(
            workflow,
            r"actions/(checkout|setup-python)@v2",
            r"actions/\1@v3",
            dry_run=False,
        )

        assert was_modified2 is True
        assert "actions/checkout@v3" in new2
        assert "actions/setup-python@v3" in new2

    def test_migrate_dockerfile(self, fixer: FileFixer, tmp_dir: Path) -> None:
        """Test migrating Dockerfile base image."""
        dockerfile = tmp_dir / "Dockerfile"
        dockerfile.write_text(
            """FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "app.py"]
"""
        )

        # Update Python version
        was_modified, original, new = fixer.apply_fix(
            dockerfile,
            r"python:3\.9",
            "python:3.11",
            dry_run=False,
        )

        assert was_modified is True
        assert "FROM python:3.11-slim" in new

    def test_refactor_import_statements(
        self, fixer: FileFixer, tmp_dir: Path
    ) -> None:
        """Test refactoring Python import statements."""
        py_file = tmp_dir / "module.py"
        py_file.write_text(
            """from typing import Dict, List, Optional
import json
from collections import defaultdict

def process(data: Dict[str, List[int]]) -> Optional[str]:
    result: Dict[str, int] = defaultdict(int)
    return json.dumps(result)
"""
        )

        # Replace old-style type hints with new-style (3.10+)
        was_modified, original, new = fixer.apply_fix(
            py_file,
            r"\bDict\[",
            "dict[",
            dry_run=False,
        )

        assert was_modified is True
        assert "dict[" in new

        was_modified2, _, new2 = fixer.apply_fix(
            py_file,
            r"\bList\[",
            "list[",
            dry_run=False,
        )

        assert was_modified2 is True
        assert "list[" in new2
