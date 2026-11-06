# Development

Python 3.11+ and [uv](https://docs.astral.sh/uv/) are required.

```bash
git clone https://github.com/marcobertenghi/redact-pdf
cd redact-pdf
```

```bash
uv sync                    # create .venv with runtime and dev dependencies
uv run pytest              # tests
uv run pytest --cov=redact_pdf
uv run ruff check .        # lint
uv run ruff format .       # format
uv run mypy src            # type check
```

Project layout:

```
redact-pdf/
├── src/redact_pdf/
│   ├── core.py        # redaction, verification and flattening
│   ├── document.py    # shared file handling, output paths and cleanup
│   ├── boxes.py       # apply-boxes: manual boxes become real redactions
│   ├── images.py      # image areas no page shows (cropped, off the page)
│   ├── pages.py       # remove-pages: drop pages completely
│   ├── matching.py    # finding terms by character position, email domains
│   ├── names.py       # person names taken from email addresses
│   ├── pdfstrings.py  # text outside the page content: string objects, marked content
│   ├── metadata.py    # deep metadata removal, lossless JPEG cleaning
│   ├── cli.py         # command-line interface
│   └── __main__.py    # python -m redact_pdf
├── tests/             # pytest suite with generated sample PDFs
├── docs/              # command reference and development guide
├── examples/          # example term list
└── pyproject.toml
```

Document operations share file handling and sanitization in `document.py`; matching and PDF syntax handling stay in their own modules. When changing the save pipeline, preserve the order of cleanup, orphan removal and string rewriting, and keep verification on the saved output.

Contributions are welcome. Please open an issue first for larger changes, and make sure `ruff`, `mypy` and `pytest` pass. Never commit real documents or term lists; `.gitignore` excludes `*.pdf` and `terms.txt` by default.

