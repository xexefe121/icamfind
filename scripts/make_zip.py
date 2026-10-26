"""Build iCamFind-code.zip from the files tracked by git (no secrets, no local data, no build output)."""

from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, check=True, capture_output=True, text=True).stdout.split()
    out = ROOT / "iCamFind-code.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            if f.startswith(".icamfind") or f.endswith(".env"):
                continue
            z.write(ROOT / f, f"icamfind/{f}")
    print(f"wrote {out.name} with {len(files)} files ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
