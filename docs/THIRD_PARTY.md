# Third-party software

AICorp itself is Apache-2.0 (see `LICENSE`). It depends on the packages below; each keeps its own license.
The list is generated from the installed package metadata by `scripts/third_party.py` (versions as pinned in `requirements.txt`).

| Package | Version | License (from package metadata) |
|---|---|---|
| annotated-doc | 0.0.5 | MIT |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.15.1 | MIT |
| certifi | 2026.10.23 | Mozilla Public License 2.0 (MPL 2.0) |
| click | 8.5.0 | BSD-3-Clause |
| fastapi | 0.162.0 | MIT |
| greenlet | 3.5.6 | MIT AND PSF-2.0 |
| h11 | 0.16.0 | MIT License |
| httpcore | 1.0.9 | BSD-3-Clause |
| httptools | 0.9.0 | MIT |
| httpx | 0.28.1 | BSD License |
| idna | 3.20 | BSD-3-Clause |
| iniconfig | 2.3.1 | MIT |
| Jinja2 | 3.1.6 | BSD License |
| MarkupSafe | 3.0.4 | BSD-3-Clause |
| opentelemetry-api | 1.45.1 | Apache-2.0 |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| playwright | 1.64.1 | Apache-2.0 |
| pluggy | 1.7.0 | MIT License |
| psutil | 7.2.2 | BSD-3-Clause |
| pydantic | 2.14.1 | MIT |
| pydantic_core | 2.50.1 | MIT |
| pyee | 14.0.0 | MIT License |
| Pygments | 2.21.0 | BSD-2-Clause |
| pytest | 9.1.1 | MIT |
| pytest-timeout | 2.4.0 | DFSG approved; MIT License |
| python-dotenv | 1.2.4 | BSD-3-Clause |
| python-multipart | 0.0.32 | Apache-2.0 |
| PyYAML | 6.0.3 | MIT License |
| starlette | 1.10.0 | BSD-3-Clause |
| typing-inspection | 0.4.4 | MIT |
| typing_extensions | 4.16.0 | PSF-2.0 |
| uvicorn | 0.54.0 | BSD-3-Clause |
| uvloop | 0.23.0 | Apache Software License; MIT License |
| watchfiles | 1.3.0 | MIT License |
| websockets | 17.3 | BSD-3-Clause |

Note: certifi is licensed under the Mozilla Public License 2.0 (file-level copyleft). AICorp uses it unmodified as a dependency.

## Windows desktop app (`desktop/`)

Only the optional Windows app uses these; nothing here is needed to run AICorp. Versions are the pinned ones in `desktop/requirements.txt`.

Bundled into `AICorp.exe`:

| Package | Version | License |
|---|---|---|
| pywebview | 6.2.1 | BSD-3-Clause |
| pythonnet | 3.2.1 | MIT |
| clr_loader | 0.3.1 | not stated in the package metadata, check upstream |
| bottle | 0.13.4 | MIT |
| proxy_tools | 0.1.0 | MIT |
| cffi | 2.1.1 | MIT-0 |
| pycparser | 3.12 | BSD-3-Clause |
| typing_extensions | 4.16.0 | PSF-2.0 |

Build tools only (not shipped in the `.exe`):

| Package | Version | License |
|---|---|---|
| pyinstaller | 6.22.3 | GPL-2.0-or-later with a special exception that allows the built program to use any license |
| pyinstaller-hooks-contrib | 2026.9 | Apache-2.0; GPL-2.0 |
| Inno Setup (installer compiler, a separate Windows program) | 6.x | Inno Setup License (free, permissive); not part of the product |
| altgraph | 0.17.5 | MIT |
| pefile | 2024.8.26 | MIT |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| pywin32-ctypes | 0.2.3 | BSD-3-Clause |
| setuptools | 84.2.1 | MIT |

The `.exe` shows the page through the Microsoft Edge WebView2 runtime that ships with Windows 11 (Microsoft's own license; not redistributed here).

Playwright downloads a Chromium build on `playwright install`; that browser is a separate download under its own licenses and is not part of this repository.
