#!/usr/bin/env python3
"""Verify source, tag, and built distribution versions agree."""

from __future__ import annotations

import argparse
import email.parser
import re
import runpy
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

__version__ = str(
    runpy.run_path(Path(__file__).resolve().parents[1] / "capslock" / "_version.py")[
        "__version__"
    ]
)


def metadata_version(content: bytes) -> str:
    message = email.parser.BytesParser().parsebytes(content)
    version = message.get("Version")
    if not version:
        raise ValueError("distribution metadata has no Version field")
    return version


def distribution_version(path: Path) -> str:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            metadata = [
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata) != 1:
                raise ValueError(f"expected one wheel METADATA file in {path}")
            return metadata_version(archive.read(metadata[0]))
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            metadata = [
                member
                for member in archive.getmembers()
                if PurePosixPath(member.name).name == "PKG-INFO"
                and len(PurePosixPath(member.name).parts) == 2
            ]
            if len(metadata) != 1:
                raise ValueError(f"expected one sdist PKG-INFO file in {path}")
            extracted = archive.extractfile(metadata[0])
            if extracted is None:
                raise ValueError(f"could not read sdist metadata from {path}")
            return metadata_version(extracted.read())
    raise ValueError(f"unsupported distribution: {path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", help="Git tag, for example v1.3.1")
    parser.add_argument("--dist", type=Path, action="append", default=[])
    args = parser.parse_args()

    if not re.fullmatch(r"\d+\.\d+\.\d+", __version__):
        raise SystemExit(f"invalid source version: {__version__}")
    if args.tag and args.tag != f"v{__version__}":
        raise SystemExit(f"tag {args.tag} does not match source version v{__version__}")
    for path in args.dist:
        packaged = distribution_version(path)
        if packaged != __version__:
            raise SystemExit(f"{path} has version {packaged}, expected {__version__}")
    print(f"Release version verified: {__version__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
