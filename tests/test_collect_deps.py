"""D36: dependency specs from the workspace are attacker-controlled input.
This is the trust boundary that let a planted setup.py run as root pre-seal
twice, so it gets a check that runs without docker."""
import importlib.util
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "docker" / "collect_deps.py"
_spec = importlib.util.spec_from_file_location("collect_deps", _SRC)
collect_deps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(collect_deps)


@pytest.mark.parametrize("spec", [
    "/workspace/evilpkg",          # D36: the exact vector, absolute path
    "./evilpkg",
    "../evilpkg",
    "evilpkg @ file:///workspace/evilpkg",
    "https://example.invalid/x.tar.gz",
    "-e /workspace",
    "--index-url http://evil.invalid/simple",
    "-r /workspace/more.txt",
    ".",
    "babel; os.system('x')",
    "babel --global-option=x",
    "evil pkg",
    123,
    None,
])
def test_rejects_anything_that_is_not_a_plain_requirement(spec):
    assert collect_deps.normalize(spec, "pyproject.toml") is None


@pytest.mark.parametrize("spec,expected", [
    ("MarkupSafe>=3", "MarkupSafe>=3"),
    ("python-dateutil>=2.8.1", "python-dateutil>=2.8.1"),
    ("pytz", "pytz"),
    ("babel >= 2 , < 3", "babel>=2,<3"),
    ("pkg[extra1,extra2]==1.0", "pkg[extra1,extra2]==1.0"),
])
def test_accepts_plain_pep508(spec, expected):
    assert collect_deps.normalize(spec, "pyproject.toml") == expected


def test_pyproject_dependencies_are_validated_not_passed_through(tmp_path, capsys):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ["/workspace/evilpkg", "babel>=2"]\n'
    )
    assert collect_deps.main(tmp_path) == 0
    out = capsys.readouterr().out.split()
    assert out == ["babel>=2"]
