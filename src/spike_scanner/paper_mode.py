from __future__ import annotations

import argparse
import re
from pathlib import Path


def set_mode(mode: str, env_path: Path = Path(".env")) -> None:
    if mode not in {"off", "shadow", "paper"}:
        raise ValueError("Erlaubt: off, shadow, paper")
    text = env_path.read_text(encoding="utf-8-sig", errors="replace") if env_path.exists() else ""
    line = f"PAPERTRADING_MODE={mode}"
    if re.search(r"(?m)^\s*PAPERTRADING_MODE\s*=.*$", text):
        text = re.sub(r"(?m)^\s*PAPERTRADING_MODE\s*=.*$", line, text, count=1)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += line + "\n"
    env_path.write_text(text, encoding="utf-8")
    print(f"PAPERTRADING_MODE={mode}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Internen Papertrading-Modus setzen")
    parser.add_argument("mode", choices=["off", "shadow", "paper"])
    parser.add_argument("--env", default=".env")
    args = parser.parse_args()
    set_mode(args.mode, Path(args.env))


if __name__ == "__main__":
    main()
