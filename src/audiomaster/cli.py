from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict
from pathlib import Path

from .engine import FORMATS, GENRES, Masterer, MasteringError


def choose(title: str, options) -> str:
    keys = list(options)

    print(f"\n{title}")
    for number, key in enumerate(keys, start=1):
        print(f"  {number}. {key}")

    while True:
        answer = input("Choose a number or name: ").strip().lower()

        if answer in keys:
            return answer

        try:
            index = int(answer) - 1
            if 0 <= index < len(keys):
                return keys[index]
        except ValueError:
            pass

        print("Invalid choice.")


def ask_lufs() -> float:
    while True:
        text = input("Desired integrated LUFS [-14]: ").strip() or "-14"

        try:
            value = float(text)
            if math.isfinite(value) and -70 <= value <= -5:
                return value
        except ValueError:
            pass

        print("Enter a finite number between -70 and -5.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Deterministic audio mastering using bundled FFmpeg. "
            "Output is 48 kHz stereo."
        )
    )
    parser.add_argument("input", nargs="?")
    parser.add_argument("-o", "--output")
    parser.add_argument("--genre", choices=list(GENRES))
    parser.add_argument("--lufs", type=float)
    parser.add_argument("--format", choices=list(FORMATS))
    parser.add_argument("--true-peak", type=float, default=-1.0)
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the complete report as JSON.",
    )

    args = parser.parse_args(argv)
    interactive = sys.stdin.isatty()

    if not interactive and (
        args.input is None
        or args.genre is None
        or args.lufs is None
        or args.format is None
    ):
        parser.error(
            "Non-interactive use requires an input path, --genre, "
            "--lufs, and --format."
        )

    try:
        source = args.input or input("Input audio file: ").strip()
        genre = args.genre or choose("Genre", GENRES)
        target = args.lufs if args.lufs is not None else ask_lufs()
        output_format = args.format or choose("Export format", FORMATS)

        source_path = Path(source).expanduser()
        suffix = FORMATS[output_format].suffix
        default_output = source_path.with_name(
            source_path.stem + ".mastered" + suffix
        )

        if args.output:
            output = args.output
        elif interactive:
            output = (
                input(f"Output file [{default_output}]: ").strip()
                or str(default_output)
            )
        else:
            output = str(default_output)

        report = Masterer().master(
            source,
            output,
            genre=genre,
            target_lufs=target,
            output_format=output_format,
            true_peak_ceiling=args.true_peak,
            overwrite=args.force,
        )

        if args.json:
            print(json.dumps(asdict(report), indent=2))
        else:
            print(f"\nSaved: {report.output_path}")
            print(
                f"Loudness: {report.after.integrated_lufs:.2f} LUFS "
                f"(requested {report.target_lufs:.2f})"
            )
            print(
                f"True peak: {report.after.true_peak_dbtp:.2f} dBTP"
            )
            print(
                f"Loudness range: "
                f"{report.after.loudness_range_lu:.2f} LU"
            )
            print(
                f"Compressor: {report.plan.compressor_ratio:.2f}:1"
            )
            print(
                f"Normalization: {report.normalization_mode}; "
                f"render attempts: {report.render_attempts}"
            )

            for warning in report.warnings:
                print(f"Warning: {warning}")

        return 0

    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except (MasteringError, ValueError, OSError, EOFError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1