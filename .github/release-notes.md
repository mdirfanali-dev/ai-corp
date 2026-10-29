AICorp for Windows. It needs WSL2 with Ubuntu; the app checks your computer and guides you. See the [Windows setup guide](https://github.com/mdirfanali-dev/ai-corp/blob/main/docs/SETUP-WINDOWS.md).

**Which file?** `AICorp-Setup-<version>.exe` is the installer (Start Menu, desktop shortcut, uninstall in Windows *Apps*; installs for your user only, no administrator rights). `AICorp.exe` is the same app without installing.
Uninstalling never touches your AICorp data (it lives inside Linux).

**Check your download.** In PowerShell, in the folder with the files:
`(Get-FileHash <file> -Algorithm SHA256).Hash.ToLower()` must equal that file's line in `SHA256SUMS`.
The file was built by GitHub Actions from the tagged commit of this repository.
