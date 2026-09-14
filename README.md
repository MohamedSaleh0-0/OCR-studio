# Batch OCR Studio

A lightweight, local Windows desktop GUI for converting PDF files into
Markdown with Mistral OCR. It supports English/Arabic UI, pasted paths,
resumable runs, multiple API keys, and secure local credential storage.

Current release line: `v0.1.0`.

The repository contains no API keys. Runtime data is stored in the ignored
`data/` directory, while API secrets are stored through Windows Credential
Manager. See `keys.json.example` only for the import format.

## Run

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

The application stores non-secret data in `data/` beside the app. API secrets
are stored through Windows Credential Manager using `keyring`.

The existing `batch_ocr.py` CLI is intentionally not changed.

## Build a one-file executable

```powershell
./build.ps1
```

The generated `dist/BatchOCRStudio.exe` is a one-file Windows executable. The
app creates a `data/` folder beside the executable on first launch. API keys
are machine/user credentials and need to be entered again on a different
Windows account.

WebView2 is required by the desktop shell; it is already present on most
modern Windows installations.

## GitHub releases

Push a version tag to build and publish the executable automatically:

```powershell
git tag v0.1.0
git push origin v0.1.0
```

The GitHub Actions workflow builds `BatchOCRStudio.exe` and attaches it to a
GitHub release. Pull requests and pushes to `main` run the verification
workflow.
