# sharenote2pdf
Faithfully convert Share Note (share.note.sx) pages — including encrypted ones — to PDF, preserving your Obsidian theme, CSS, code highlighting and images.

# sharenote2pdf

Convert a [Share Note](https://docs.note.sx) (`share.note.sx`) page to PDF **without changing anything** — same theme, CSS, fonts, images, and layout you see in the browser.

## Why it works the way it does

Share Note (the Obsidian plugin) publishes a note as a self-contained web page. For **encrypted** notes, the page ships the content as AES‑256‑GCM ciphertext; the decryption key is the part of the share URL after the `#`:

```
https://share.note.sx/9i5zrsrv#CgL2Rt0eqIn6NkJFx31xCA
                      └── id ──┘ └──── decryption key ────┘
```

The page's own JavaScript reads the key from the URL fragment, decrypts the note **in your browser**, and renders it with your full Obsidian theme. The key never touches the server.

To reproduce that exactly, this tool drives the **same rendering engine** the website uses (Chromium). It loads the real page, lets the page's own JS decrypt and lay out the note, then prints *that* to PDF via the DevTools Protocol with **screen media** and **backgrounds** preserved. Nothing is re-parsed, re-styled, or rewritten:

- the decryption key stays on your machine (fragments are never sent over the network);
- content, colours, fonts, spacing, code highlighting, callouts and images are untouched;
- the only CSS it injects lets the note flow across pages instead of being trapped in Obsidian's fixed-height, internally-scrolling reading pane (which would otherwise print a single screenful) — see notes below.

## Requirements

- Python 3 with `selenium` installed
- A Chromium/Chrome browser **and** a matching `chromedriver` on your system
  (auto-detected: `/usr/bin/chromium`, `google-chrome`, …, and `/usr/bin/chromedriver`)

## Usage

```bash
# Faithful capture (dark theme exactly as shared) -> voluer.pdf
python3 sharenote2pdf.py "https://share.note.sx/9i5zrsrv#CgL2Rt0eqIn6NkJFx31xCA"

# Choose the output file
python3 sharenote2pdf.py "<url>" -o mynote.pdf

# US Letter, with a margin
python3 sharenote2pdf.py "<url>" --format Letter --margin 0.4in

# Try a light theme (best-effort; may wash out dark-tuned code blocks)
python3 sharenote2pdf.py "<url>" --theme light
```

Quote the URL in your shell so the `#key` isn't stripped as a comment.

### Options

| Option | Default | Meaning |
|---|---|---|
| `-o, --output` | note title | Output PDF path |
| `--theme {auto,light,dark}` | `auto` | `auto` = exactly as shared (faithful) |
| `--format` | `A4` | `A3/A4/A5/Letter/Legal/Tabloid` |
| `--margin` | `0` | Page margin (`0`, `0.4in`, `10mm`, …); `0` = full-bleed like the site |
| `--scale` | `1.0` | Render scale (0.1–2.0) |
| `--landscape` | off | Landscape orientation |
| `--keep-status-bar` | off | Keep the floating "Share Note for Obsidian" bar (site chrome, removed by default) |
| `--timeout` | `45` | Seconds to wait for decryption/render |
| `--browser` / `--chromedriver` | auto | Override binary paths |
| `-q, --quiet` | off | Suppress progress logs |

## Notes on fidelity

- **Default is faithful.** `--theme auto` keeps the note exactly as it loads (this note is a dark theme, so the PDF is dark, full-bleed, matching the website).
- **Un-clipping.** Obsidian's reading view keeps content in a fixed-height container with `overflow:auto`, and marks `<body>`/`.workspace-leaf` with `contain:strict`. The tool overrides only those layout properties (height/overflow/containment) so the note flows onto every page. It changes no visual styling of the note content.
- **Status bar.** The floating "Share Note for Obsidian" bar is part of the site UI, not the note, and (being `position:fixed`) would otherwise be stamped onto every page. It's removed by default; pass `--keep-status-bar` to keep it.
- **Attachments.** Images are fetched from the server at render time (Share Note stores attachments unencrypted), so an internet connection is needed and the note must still be online.
