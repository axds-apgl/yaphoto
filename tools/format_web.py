"""Format the browser code consistently; --check is used in CI."""

import argparse
from pathlib import Path

import jsbeautifier


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    path = Path(__file__).resolve().parents[1] / "web" / "app.js"
    options = jsbeautifier.default_options()
    options.indent_size = 2
    options.wrap_line_length = 100
    options.end_with_newline = True
    original = path.read_text(encoding="utf-8")
    formatted = jsbeautifier.beautify(original, options)
    if args.check:
        if original != formatted:
            print("Run: python tools/format_web.py")
            return 1
    else:
        path.write_text(formatted, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
