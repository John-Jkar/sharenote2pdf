#!/usr/bin/env python3
"""
sharenote2pdf — convert a Share Note (share.note.sx) page to PDF, faithfully.

Share Note (the Obsidian plugin, https://docs.note.sx) publishes notes as a
self-contained web page. Encrypted notes ship their content as AES-GCM
ciphertext inside the page; the decryption key lives in the URL fragment
(everything after the '#'). The page's own JavaScript decrypts and renders the
note in the browser using your full Obsidian theme and CSS.

To reproduce that exactly — "without changing anything" — this tool drives the
*same* rendering engine the website uses (Chromium). It loads the real page,
lets the page's own JS decrypt and lay out the note (real theme, real CSS,
real fonts and images), then prints that to PDF via the DevTools Protocol with
screen media and backgrounds preserved. The result matches what a visitor sees.

Nothing about the note is altered: the decryption key never leaves your
machine, the content is not re-parsed or re-styled, and no markup is rewritten.

Usage:
    python3 sharenote2pdf.py "https://share.note.sx/<id>#<key>"
    python3 sharenote2pdf.py "<url>" -o output.pdf
    python3 sharenote2pdf.py "<url>" --theme light --format Letter --margin 0.4in

Requires: selenium, and a Chromium/Chrome browser + matching chromedriver.
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import sys
import time

# ---- Paper sizes in inches (width, height) ---------------------------------
PAPER_SIZES = {
    "A4":     (8.27, 11.69),
    "A3":     (11.69, 16.54),
    "A5":     (5.83, 8.27),
    "Letter": (8.5, 11.0),
    "Legal":  (8.5, 14.0),
    "Tabloid": (11.0, 17.0),
}

# Candidate browser binaries and chromedrivers, in order of preference.
BROWSER_CANDIDATES = [
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/snap/bin/chromium",
]
CHROMEDRIVER_CANDIDATES = [
    "/usr/bin/chromedriver",
    "/usr/local/bin/chromedriver",
    "/snap/bin/chromium.chromedriver",
]

# JS that reports whether the note has finished decrypting/rendering, so we
# don't print a half-loaded ("Encrypted note") page. Returns one of:
#   "ready"      - content is present (decrypted, or an unencrypted note)
#   "failed"     - the page reported it couldn't decrypt with this key
#   "no-key"     - encrypted payload present but no key supplied / still locked
#   "pending"    - still working
READINESS_JS = r"""
const enc = document.getElementById('encrypted-data');
const tpl = document.getElementById('template-user-data');
const hasPayload = !!(enc && enc.textContent && enc.textContent.trim());
// Unencrypted note: no ciphertext payload, content is already in the page.
if (!hasPayload) return 'ready';
// Encrypted note: on success the placeholder element is replaced outright.
if (!tpl) return 'ready';
const html = tpl.innerHTML || '';
if (html.indexOf('Unable to decrypt') !== -1) return 'failed';
if (tpl.textContent.trim() === 'Encrypted note') return 'no-key';
return 'pending';
"""


def log(msg: str, *, verbose: bool = True) -> None:
    if verbose:
        print(f"[sharenote2pdf] {msg}", file=sys.stderr)


def first_existing(paths, override=None):
    if override:
        return override if os.path.exists(override) else None
    for p in paths:
        if os.path.exists(p):
            return p
    return None


def parse_length_inches(value: str) -> float:
    """Parse a CSS-ish length ('0', '0.4in', '10mm', '1cm', '12pt', '96px')."""
    v = value.strip().lower()
    m = re.fullmatch(r"([0-9]*\.?[0-9]+)\s*(in|mm|cm|pt|px)?", v)
    if not m:
        raise argparse.ArgumentTypeError(f"invalid length: {value!r}")
    num = float(m.group(1))
    unit = m.group(2) or "in"
    return {
        "in": num,
        "mm": num / 25.4,
        "cm": num / 2.54,
        "pt": num / 72.0,
        "px": num / 96.0,
    }[unit]


def sanitize_filename(name: str) -> str:
    name = (name or "").strip() or "note"
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name)  # illegal on common FS
    name = name.rstrip(". ")                             # trailing dot/space
    return (name or "note")[:180]


def build_driver(browser_bin, chromedriver_bin, window_width, window_height, verbose):
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    opts = Options()
    if browser_bin:
        opts.binary_location = browser_bin
    for arg in (
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--hide-scrollbars",
        "--force-color-profile=srgb",
        "--disable-extensions",
        "--mute-audio",
        f"--window-size={window_width},{window_height}",
    ):
        opts.add_argument(arg)

    service = Service(executable_path=chromedriver_bin) if chromedriver_bin else Service()
    log(f"launching browser: {browser_bin or '(selenium default)'}", verbose=verbose)
    driver = webdriver.Chrome(service=service, options=opts)
    driver.set_page_load_timeout(90)
    return driver


# CSS injected right before printing. Share Note reproduces Obsidian's reading
# view, whose content lives inside a fixed-height, internally-scrolling
# container. Two Obsidian tricks stop the whole note from printing as-is:
#   1. the container chain is locked to the viewport height with overflow:auto
#      (so only the first screenful would be captured), and
#   2. <body> and .workspace-leaf use `contain: strict`, which makes their size
#      ignore their content — so the page would collapse to nothing.
# This overrides both so the document flows to its true height and every page is
# captured. It changes no colours, fonts, spacing or content — only the scroll
# clipping and size-containment — so the note itself is untouched. The inner
# .markdown-preview-view keeps its own positioning so any banner/absolute
# content inside the note is not shifted.
UNCLIP_CSS = """
html, body,
.app-container, .horizontal-main-container,
.workspace, .workspace-split, .workspace-leaf,
.workspace-leaf-content, .view-content,
.markdown-reading-view {
    height: auto !important;
    min-height: 0 !important;
    max-height: none !important;
    overflow: visible !important;
    contain: none !important;
    position: static !important;
}
.markdown-preview-view {
    height: auto !important;
    min-height: 0 !important;
    max-height: none !important;
    overflow: visible !important;
    contain: none !important;
}
"""
# The floating "Share Note for Obsidian" bar is site chrome (position:fixed) and
# is removed in unclip_for_print() unless --keep-status-bar is given.


def unclip_for_print(driver, hide_status_bar: bool) -> None:
    driver.execute_script(
        """
        const style = document.createElement('style');
        style.id = 'sharenote2pdf-print';
        style.textContent = arguments[0];
        document.head.appendChild(style);
        // The floating "Share Note for Obsidian" bar is site chrome, not part
        // of the note. It carries an inline `display:flex !important;
        // position:fixed !important` that a stylesheet cannot override and, being
        // fixed, it stamps itself onto every printed page. Remove it outright.
        if (arguments[1]) {
            document.querySelectorAll('.status-bar').forEach(el => el.remove());
        }
        // Force a reflow so print layout sees the new heights.
        void document.body.offsetHeight;
        """,
        UNCLIP_CSS,
        hide_status_bar,
    )


def apply_theme(driver, theme: str) -> None:
    """Replicate the page's own light/dark toggle (see app.js)."""
    if theme == "light":
        driver.execute_script(
            "document.body.classList.remove('theme-dark');"
            "document.body.classList.add('theme-light');"
        )
    elif theme == "dark":
        driver.execute_script(
            "document.body.classList.remove('theme-light');"
            "document.body.classList.add('theme-dark');"
        )
    # 'auto' -> leave the page exactly as it loaded (faithful default).


def wait_for_ready(driver, timeout: float, verbose: bool) -> str:
    deadline = time.time() + timeout
    state = "pending"
    while time.time() < deadline:
        try:
            state = driver.execute_script(READINESS_JS)
        except Exception:
            state = "pending"
        if state in ("ready", "failed", "no-key"):
            return state
        time.sleep(0.25)
    return state


def settle_resources(driver, verbose: bool) -> None:
    """Force lazy content in, then wait for images and fonts to finish."""
    try:
        driver.execute_script(
            """
            const h = document.body.scrollHeight;
            let y = 0;
            while (y < h) { window.scrollTo(0, y); y += window.innerHeight; }
            window.scrollTo(0, 0);
            """
        )
    except Exception:
        pass

    # Wait for all <img> to settle (errored images also report complete=true).
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            done = driver.execute_script(
                "return Array.from(document.images).every(i => i.complete);"
            )
        except Exception:
            done = True
        if done:
            break
        time.sleep(0.25)

    # Wait for web fonts, so text metrics match the live page.
    try:
        driver.set_script_timeout(20)
        driver.execute_async_script(
            "const cb = arguments[arguments.length - 1];"
            "if (document.fonts && document.fonts.ready) {"
            "  document.fonts.ready.then(() => cb(true)); } else { cb(true); }"
        )
    except Exception:
        pass
    time.sleep(0.4)  # brief paint settle


def print_to_pdf(driver, *, paper_w, paper_h, margin_in, scale, landscape) -> bytes:
    # Render using on-screen styles (not print CSS), so the PDF matches the site.
    driver.execute_cdp_cmd("Emulation.setEmulatedMedia", {"media": "screen"})
    result = driver.execute_cdp_cmd(
        "Page.printToPDF",
        {
            "printBackground": True,      # keep theme background + code/callout fills
            "preferCSSPageSize": False,
            "landscape": bool(landscape),
            "paperWidth": paper_w,
            "paperHeight": paper_h,
            "marginTop": margin_in,
            "marginBottom": margin_in,
            "marginLeft": margin_in,
            "marginRight": margin_in,
            "scale": scale,
            "displayHeaderFooter": False,
            "transferMode": "ReturnAsBase64",
        },
    )
    return base64.b64decode(result["data"])


def convert(url, output, theme, fmt, margin_in, scale, landscape,
            window_width, window_height, timeout, browser_bin,
            chromedriver_bin, hide_status_bar, verbose):
    if "#" not in url:
        log("WARNING: URL has no '#<key>' fragment. If this note is encrypted, "
            "it cannot be decrypted without the key and will render as "
            "'Encrypted note'.", verbose=True)

    driver = build_driver(browser_bin, chromedriver_bin, window_width,
                          window_height, verbose)
    try:
        log(f"loading {url}", verbose=verbose)
        driver.get(url)

        log("waiting for the note to decrypt / render ...", verbose=verbose)
        state = wait_for_ready(driver, timeout, verbose)
        if state == "failed":
            raise SystemExit("Error: the page could not decrypt this note — the "
                             "key in the URL (#...) is wrong or incomplete.")
        if state == "no-key":
            raise SystemExit("Error: this note is encrypted and no valid key was "
                             "supplied. Make sure the full URL including the "
                             "'#<key>' fragment is passed (quote it in the shell).")
        if state != "ready":
            log("WARNING: timed out waiting for content; printing whatever "
                "rendered so far.", verbose=True)

        apply_theme(driver, theme)
        settle_resources(driver, verbose)
        unclip_for_print(driver, hide_status_bar=hide_status_bar)

        title = (driver.title or "").strip()
        log(f"note title: {title!r}", verbose=verbose)

        paper_w, paper_h = PAPER_SIZES[fmt]
        log(f"printing to PDF ({fmt} {paper_w}x{paper_h}in, margin={margin_in:.3g}in, "
            f"scale={scale}, theme={theme}) ...", verbose=verbose)
        pdf = print_to_pdf(driver, paper_w=paper_w, paper_h=paper_h,
                           margin_in=margin_in, scale=scale, landscape=landscape)

        if not output:
            output = sanitize_filename(title) + ".pdf"
        with open(output, "wb") as f:
            f.write(pdf)
        log(f"wrote {output} ({len(pdf):,} bytes)", verbose=True)
        return output
    finally:
        driver.quit()


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="sharenote2pdf",
        description="Convert a Share Note (share.note.sx) page to PDF, faithfully "
                    "reproducing the live rendering (theme, CSS, fonts, images).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("url", help="Full share URL, including the '#<key>' fragment "
                               "for encrypted notes. Quote it in the shell.")
    p.add_argument("-o", "--output", help="Output PDF path (default: derived from "
                                          "the note's title).")
    p.add_argument("--theme", choices=["auto", "light", "dark"], default="auto",
                   help="'auto' keeps the note exactly as it loads — the faithful "
                        "default. 'light'/'dark' flip the theme class; note that "
                        "'light' can wash out code blocks in notes whose custom "
                        "CSS is tuned for dark mode.")
    p.add_argument("--format", dest="fmt", choices=sorted(PAPER_SIZES), default="A4",
                   help="Paper size.")
    p.add_argument("--margin", type=parse_length_inches, default="0",
                   help="Page margin (e.g. 0, 0.4in, 10mm). 0 = full-bleed, most "
                        "like the website.")
    p.add_argument("--scale", type=float, default=1.0,
                   help="Render scale (0.1-2.0). <1 fits more per page.")
    p.add_argument("--landscape", action="store_true", help="Landscape orientation.")
    p.add_argument("--keep-status-bar", action="store_true",
                   help="Keep the floating 'Share Note for Obsidian' bar (hidden "
                        "by default; it is site chrome, not note content).")
    p.add_argument("--width", type=int, default=1000, dest="window_width",
                   help="Browser window width in px (affects layout width).")
    p.add_argument("--height", type=int, default=1400, dest="window_height",
                   help="Browser window height in px.")
    p.add_argument("--timeout", type=float, default=45.0,
                   help="Seconds to wait for the note to decrypt/render.")
    p.add_argument("--browser", help="Path to Chromium/Chrome binary "
                                     "(auto-detected if omitted).")
    p.add_argument("--chromedriver", help="Path to chromedriver "
                                          "(auto-detected if omitted).")
    p.add_argument("-q", "--quiet", action="store_true", help="Suppress progress logs.")
    args = p.parse_args(argv)

    verbose = not args.quiet
    if not (0.1 <= args.scale <= 2.0):
        p.error("--scale must be between 0.1 and 2.0")

    browser_bin = first_existing(BROWSER_CANDIDATES, args.browser)
    if args.browser and not browser_bin:
        p.error(f"--browser path does not exist: {args.browser}")
    chromedriver_bin = first_existing(CHROMEDRIVER_CANDIDATES, args.chromedriver)
    if args.chromedriver and not chromedriver_bin:
        p.error(f"--chromedriver path does not exist: {args.chromedriver}")

    try:
        convert(
            url=args.url, output=args.output, theme=args.theme, fmt=args.fmt,
            margin_in=args.margin, scale=args.scale, landscape=args.landscape,
            window_width=args.window_width, window_height=args.window_height,
            timeout=args.timeout, browser_bin=browser_bin,
            chromedriver_bin=chromedriver_bin,
            hide_status_bar=not args.keep_status_bar, verbose=verbose,
        )
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
