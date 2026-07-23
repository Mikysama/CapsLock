import re
import subprocess
import sys
from importlib.metadata import version

import pytest

from capslock import __version__
from capslock.cli.app import build_parser


def test_package_version_matches_distribution_metadata() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)
    assert version("capslock") == __version__


def test_cli_reports_package_version(capsys) -> None:
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["--version"])

    assert error.value.code == 0
    assert capsys.readouterr().out.strip() == f"capslock {__version__}"


def test_module_entrypoint_reports_package_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "capslock", "--version"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == f"capslock {__version__}"
