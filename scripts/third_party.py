#!/usr/bin/env python3
"""Write docs/THIRD_PARTY.md from the license metadata of the installed packages listed in requirements.txt.

    python scripts/third_party.py            # rewrite the file
    python scripts/third_party.py --check    # exit 1 if a package has no permissive license (used before a release)
"""
from __future__ import annotations

import argparse
import re
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PERMISSIVE = ("mit", "bsd", "apache", "psf", "python software foundation", "isc", "mpl", "zlib", "unlicense", "0bsd", "cc0")
COPYLEFT = ("gpl", "agpl", "lgpl", "sspl", "eupl")


def license_of(dist: metadata.Distribution) -> str:
    meta = dist.metadata
    expr = (meta.get("License-Expression") or "").strip()
    if expr:
        return expr
    classifiers = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    classifiers = [c for c in classifiers if c.lower() != "osi approved"]
    if classifiers:
        return "; ".join(classifiers)
    text = (meta.get("License") or "").strip().splitlines()
    return text[0][:80] if text and len(text[0]) < 80 else "unknown"


# The Windows desktop app is built on Windows (desktop/build.ps1), so its tools are not installed here; read from the metadata of the
# pinned versions in desktop/requirements.txt (checked when the app was built).
DESKTOP_BUNDLED = [("pywebview", "6.2.1", "BSD-3-Clause"), ("pythonnet", "3.2.1", "MIT"), ("clr_loader", "0.3.1", "not stated in the package metadata, check upstream"),
                   ("bottle", "0.13.4", "MIT"), ("proxy_tools", "0.1.0", "MIT"), ("cffi", "2.1.1", "MIT-0"), ("pycparser", "3.12", "BSD-3-Clause"),
                   ("typing_extensions", "4.16.0", "PSF-2.0")]
DESKTOP_BUILD_ONLY = [("pyinstaller", "6.22.3", "GPL-2.0-or-later with a special exception that allows the built program to use any license"),
                      ("pyinstaller-hooks-contrib", "2026.9", "Apache-2.0; GPL-2.0"), ("Inno Setup (installer compiler, a separate Windows program)", "6.x", "Inno Setup License (free, permissive); not part of the product"), ("altgraph", "0.17.5", "MIT"), ("pefile", "2024.8.26", "MIT"),
                      ("packaging", "26.3", "Apache-2.0 OR BSD-2-Clause"), ("pywin32-ctypes", "0.2.3", "BSD-3-Clause"), ("setuptools", "84.2.1", "MIT")]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    names = [re.split(r"[=<>~!\[]", line.strip())[0] for line in (ROOT / "requirements.txt").read_text().splitlines() if line.strip() and not line.startswith("#")]
    rows, problems = [], []
    for name in sorted(set(names), key=str.lower):
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            problems.append(f"{name}: not installed, cannot read its license")
            continue
        lic = license_of(dist)
        low = lic.lower()
        if any(c in low for c in COPYLEFT) and "lgpl" not in low or not any(p in low for p in PERMISSIVE):
            problems.append(f"{name} {dist.version}: license '{lic}' is not on the permissive list - review before release")
        rows.append((dist.metadata["Name"], dist.version, lic))
    if args.check:
        for p in problems:
            print(p)
        return 1 if problems else 0
    lines = ["# Third-party software", "", "AICorp itself is Apache-2.0 (see `LICENSE`). It depends on the packages below; each keeps its own license.",
             "The list is generated from the installed package metadata by `scripts/third_party.py` (versions as pinned in `requirements.txt`).", "",
             "| Package | Version | License (from package metadata) |", "|---|---|---|"]
    lines += [f"| {n} | {v} | {l} |" for n, v, l in rows]
    mpl = [n for n, _, l in rows if "mpl" in l.lower() or "mozilla" in l.lower()]
    if mpl:
        lines += ["", f"Note: {', '.join(mpl)} is licensed under the Mozilla Public License 2.0 (file-level copyleft). AICorp uses it unmodified as a dependency."]
    if problems:
        lines += ["", "## Needs review", ""] + [f"- {p}" for p in problems]
    lines += ["", "## Windows desktop app (`desktop/`)", "",
              "Only the optional Windows app uses these; nothing here is needed to run AICorp. Versions are the pinned ones in `desktop/requirements.txt`.",
              "", "Bundled into `AICorp.exe`:", "", "| Package | Version | License |", "|---|---|---|"]
    lines += [f"| {n} | {v} | {l} |" for n, v, l in DESKTOP_BUNDLED]
    lines += ["", "Build tools only (not shipped in the `.exe`):", "", "| Package | Version | License |", "|---|---|---|"]
    lines += [f"| {n} | {v} | {l} |" for n, v, l in DESKTOP_BUILD_ONLY]
    lines += ["", "The `.exe` shows the page through the Microsoft Edge WebView2 runtime that ships with Windows 11 (Microsoft's own license; not redistributed here).",
              "", "Playwright downloads a Chromium build on `playwright install`; that browser is a separate download under its own licenses and is not part of this repository."]
    (ROOT / "docs" / "THIRD_PARTY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote docs/THIRD_PARTY.md ({len(rows)} packages, {len(problems)} to review)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
