"""Audit Mach-O minimum OS declarations in the actual Apple Silicon app payload."""

import argparse
import json
import struct
from pathlib import Path


def minimum_versions(path):
    with path.open("rb") as file:
        magic = file.read(4)
        if magic != b"\xcf\xfa\xed\xfe":
            return []
        file.seek(0)
        header = file.read(32)
        commands = struct.unpack_from("<I", header, 16)[0]
        values = []
        for _ in range(commands):
            command_header = file.read(8)
            if len(command_header) != 8:
                raise ValueError(f"Truncated Mach-O: {path}")
            command, size = struct.unpack("<II", command_header)
            if size < 8:
                raise ValueError(f"Invalid load command: {path}")
            payload = file.read(size - 8)
            if command == 0x32:  # LC_BUILD_VERSION
                version = struct.unpack_from("<I", payload, 4)[0]
                values.append((version >> 16, (version >> 8) & 255, version & 255))
            elif command == 0x24:  # LC_VERSION_MIN_MACOSX
                version = struct.unpack_from("<I", payload)[0]
                values.append((version >> 16, (version >> 8) & 255, version & 255))
        return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path)
    parser.add_argument("--maximum", default="14.0.0")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    limit = tuple(int(x) for x in args.maximum.split("."))
    limit += (0,) * (3 - len(limit))
    records = []
    for path in args.app.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        for version in minimum_versions(path):
            records.append(
                {"file": str(path.relative_to(args.app)), "minimum": ".".join(map(str, version))}
            )
            if version > limit:
                raise ValueError(f"{path.relative_to(args.app)} needs {version}, above {limit}")
    if not records:
        raise ValueError("No Mach-O declarations found")
    report = {
        "target": ".".join(map(str, limit)),
        "checked_macho_declarations": len(records),
        "highest_declared_minimum": max(
            (r["minimum"] for r in records), key=lambda v: tuple(map(int, v.split(".")))
        ),
        "scope": "Thin arm64 Mach-O declarations; not a runtime test on older macOS.",
        "files": records,
    }
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "files"}, indent=2))


if __name__ == "__main__":
    main()
