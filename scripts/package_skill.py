#!/usr/bin/env python3
"""Package this skill's source files, excluding media, OCR caches and bytecode."""

import argparse
from pathlib import Path
import zipfile


def package(root, output):
    root = Path(root).resolve()
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("Output exists; choose a new package filename")
    allowed = {".md", ".py", ".swift", ".sh", ".yaml", ".yml"}
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if path.is_symlink() or not path.is_file():
                continue
            if any(
                part.startswith(".") or part == "__pycache__" for part in relative.parts
            ):
                continue
            if relative.parts[0] not in {
                "agents",
                "references",
                "scripts",
                "SKILL.md",
                "README.md",
            }:
                continue
            if path.suffix not in allowed:
                continue
            archive.write(path, str(Path(root.name) / relative))
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    print(package(Path(__file__).resolve().parents[1], args.output))
