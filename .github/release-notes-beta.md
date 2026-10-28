**Testing version (beta) of AICorp for Windows.** Please read before you download.

* **Not code-signed yet.** Windows SmartScreen will warn: *More info* -> *Run anyway*. Signing is planned ([policy](https://github.com/mdirfanali-dev/ai-corp/blob/main/docs/CODE-SIGNING-POLICY.md)).
* **Tested only on one computer** (Windows 11 + WSL2, Ubuntu). Starting the app while WSL is switched off, a computer without WSL, other screen sizes and other Windows versions are **not tested yet**.
* It needs **WSL2 with Ubuntu** and a copy of AICorp inside Linux; the app checks this for you and tells you what to do. See the [Windows setup guide](https://github.com/mdirfanali-dev/ai-corp/blob/main/docs/SETUP-WINDOWS.md).
* Real Claude work through the app has not been tested; the simulated mode is safe to try.

**Check your download.** In PowerShell, in the folder with the files:
`(Get-FileHash AICorp.exe -Algorithm SHA256).Hash.ToLower()` must equal the value in `SHA256SUMS`.
The file was built by GitHub Actions from the tagged commit of this repository.

Please report problems as an issue and say what you were doing.
