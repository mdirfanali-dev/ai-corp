AICorp for Windows. It needs WSL2 with Ubuntu; the app checks your computer and guides you. See the [Windows setup guide](https://github.com/mdirfanali-dev/ai-corp/blob/main/docs/SETUP-WINDOWS.md).

**Check your download.** In PowerShell, in the folder with the files:
`(Get-FileHash AICorp.exe -Algorithm SHA256).Hash.ToLower()` must equal the value in `SHA256SUMS`.
The file was built by GitHub Actions from the tagged commit of this repository.
