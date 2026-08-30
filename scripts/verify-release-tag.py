"""Verify that a release tag matches the project version."""
from __future__ import annotations

import argparse
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 release runners install tomli.
    import tomli as tomllib


def project_version(path: Path) -> str:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    args = parser.parse_args()

    version = project_version(args.pyproject)
    expected = f"v{version}"
    if args.tag != expected:
        parser.error(f"tag {args.tag!r} does not match package version {version!r}")
    print(f"verified {args.tag} for package version {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
