#!/usr/bin/env python3
"""
chunks_convert.py

Processes the entire PDF in chunks of 10 pages using docling and either
Google Gemini API or OpenRouter free models, saving intermediate chunks to allow resuming.
"""

import argparse
import glob
import json
import os
import re
import sys
import time

import requests
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption

# ==========================================
# CONFIGURATION & CONSTANTS
# ==========================================
CHUNK_SIZE = 10
DELAY_BETWEEN_CHUNKS = 20  # Seconds to wait after a successful conversion
CHUNK_TIMEOUT = 15 * 60  # 30 minutes — if a chunk takes longer, switch models
ALL_FAIL_RETRY_DELAY = 60  # Seconds to wait before retrying when every model fails

# API Retry and Backoff settings
MAX_RETRIES = 15
INITIAL_BACKOFF = 5
MAX_BACKOFF = 360
REQUEST_TIMEOUT = 300  # HTTP request timeout in seconds

# Default list of free models to try on OpenRouter
FREE_MODELS = [
    "gemini-3.1-flash-lite",  # good enough
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
    "nousresearch/hermes-3-llama-3.1-405b:free",
    "gemini-2.5-flash",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
]

# ---------------------------------------------------------------------------
# OPTIONAL SYSTEM PROMPT INJECTION
# Add any extra instructions here and they will be appended to the system
# prompt at runtime.  Leave as an empty string to disable.
# ---------------------------------------------------------------------------
SYSTEM_PROMPT_INJECTION = """\
General instructions:
0. You are transcribing The Norton Anthology of Poetry.

1. Convert tables of poem lines (where the left column is the poem line and the right column \
contains word glosses/definitions) into standard, clean poem lines (raw text).

2. Use markdown headers strictly as follows: use # only for major headings (e.g., major sections or authors), \
## for major subsections, and ### for all individual poem/prologue/story titles (e.g. 'The Wife of Bath's Prologue', \
'The Wife of Bath's Tale', 'The Knight's Tale').

3. The glosses on the right column are explanations of archaic/difficult words. You must match \
them to the corresponding word in the poem line and format them as markdown footnotes (e.g., [^1]). \
For example, if the line is 'For in his male he hadde a pilwe-beer' and the gloss is 'bag I pillowcase', \
you should output 'For in his male[^1] he hadde a pilwe-beer[^2]' and include the footnotes at \
the end of the poem/page:
[^1]: bag
[^2]: pillowcase

4. Include all explanatory numbered footnotes as markdown footnotes. We have pre-extracted and appended these footnotes to the end of the input under the heading '### FOOTER ELEMENTS'.
You MUST match every footnote indicator in the text (like numbers or markers next to words, or footnote markers in titles) \
to its corresponding definition in that list, and include the exact content as a markdown footnote definition at the \
bottom of the chunk.

5. Remove all page numbers, running headers/footers, verse line numbers and irrelevant layout clutter. However, do NOT remove footnote markers or indicators from section titles or text; convert them to markdown footnote links (e.g., [^1]) instead.

ADDITIONAL FORMATTING INSTRUCTIONS:
- NO TABLES: Do not output any Markdown or HTML tables. Convert all tables in the source into raw, clean poem lines (raw text).
- Poetry must be structured as separate lines of verse. If the OCR/Docling output combines multiple lines of poetry \
into a single row, cell, or paragraph, you MUST split them back into separate lines. Every line of verse must be on its own line.
- FOOTNOTE DEFINITIONS: Do not include the initial footnote number or bracketed index (such as '[5]' or '5') \
at the start of the footnote definitions at the bottom of the chunk. Only include the actual footnote content.
- UNIQUE FOOTNOTE NUMBERING: Number all footnotes in the chunk continuously starting from 1 (e.g., [^1], [^2], ..., [^N]) to avoid collision. Do not repeat footnote numbers within the same response.
- Fix obvious OCR spelling/transcription errors using your knowledge of the poems (e.g., 'wiste P' -> 'wiste I', 'swich a' -> 'swich', \
'gentiP' -> 'gentil', 'yen steepe, 2 -' -> 'yen steepe', 'A A fairer' -> 'A fairer').
- Remove any stray layout noise (like random pipe characters '|', degree symbols '°' or footnote numbers '0'/'1' \
inside words, e.g., 'male 0' -> 'male', 'pilwe-beer 0' -> 'pilwe-beer'). Keep the actual Middle English punctuation and spelling intact.

- EXCLUDE ALL VERSE NUMBERS: You MUST completely exclude all verse numbers (such as line counts '5', '10', '15', \
'120', '800', etc.) which appear in the margins or text to count lines. Do not output these numbers at \
the start, end, or middle of any poem lines. Remove them entirely.
"""

# ==========================================
# SYSTEM PROMPT
# ==========================================

SYSTEM_PROMPT = """\
You are a meticulous text transcriber. Your task is to clean up OCR and markdown extracted a pdf.

1. You are processing pages in continuous chunks/batches. \
The first line of the input text is NOT necessarily a title. It is almost always a \
continuation of a text from the previous page. DO NOT format the first line as a header there is explicit evidence in the text that it is a new section title. Start directly with the text as-is.

2. Output ONLY the markdown containing only section headings, raw text, and the footnotes, formatted according to subsequent instructions.
"""


def build_system_prompt():
    """Return the final system prompt, optionally with the top-level injection appended."""
    injection = SYSTEM_PROMPT_INJECTION.strip()
    if injection:
        return SYSTEM_PROMPT + "\n\n" + injection
    return SYSTEM_PROMPT


# ==========================================
# API HELPER FUNCTIONS
# ==========================================


def find_working_openrouter_model(api_key, model_pool):
    """
    Checks which model from the provided pool is active, authenticating,
    and not rate-limited on OpenRouter. Returns None if none are available.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/google/antigravity",
        "X-Title": "Antigravity Code Assistant",
    }
    payload = {"messages": [{"role": "user", "content": "ping"}], "temperature": 0.1}

    print("Testing OpenRouter models for availability...")
    for model in model_pool:
        payload["model"] = model
        try:
            response = requests.post(
                "https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload, timeout=10
            )

            if response.status_code == 200:
                res_json = response.json()
                # Check for OpenRouter "Ghost 200" internal errors
                if "error" not in res_json:
                    print(f" -> [OK] Model {model} responded successfully.")
                    return model
                err_msg = res_json["error"].get("message", "Unknown provider error")
                print(f" -> [FAIL] {model} returned 200 with error: {err_msg}")
            else:
                print(f" -> [FAIL] {model} returned HTTP {response.status_code}")

        except requests.exceptions.RequestException as e:
            print(f" -> [TIMEOUT/NET FAIL] {model} could not be reached: {type(e).__name__}")

    print(" [!] No working models found in the requested OpenRouter pool.")
    return None


def call_openrouter(api_key, model, prompt, system_prompt):
    """Calls OpenRouter with exponential backoff on rate limits."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/google/antigravity",
        "X-Title": "Antigravity Code Assistant",
    }
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}],
        "temperature": 0.1,
    }

    backoff = INITIAL_BACKOFF
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                data=json.dumps(payload),
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code == 200:
                return response.json()["choices"][0]["message"]["content"]
            if response.status_code == 429:
                print(f"OpenRouter rate limit (429). Retrying in {backoff}s...")
            else:
                print(f"OpenRouter API Error {response.status_code}: {response.text}. Retrying in {backoff}s...")

            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)
        except Exception as e:
            print(f"Network error: {e}. Retrying in {backoff}s...")
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

    raise Exception("Failed to call OpenRouter after retries.")


def call_google_gemini(api_key, model, prompt, system_prompt):
    """Calls Google Gemini API directly with exponential backoff."""
    clean_model = model.split("/")[-1] if "/" in model else model
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{clean_model}:generateContent?key={api_key}"
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "generationConfig": {"temperature": 0.1},
    }

    backoff = INITIAL_BACKOFF
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
            if response.status_code == 200:
                return response.json()["candidates"][0]["content"]["parts"][0]["text"]
            if response.status_code in [429, 503]:
                print(f"Google API rate limit/overload ({response.status_code}). Retrying in {backoff}s...")
            else:
                print(f"Google API Error {response.status_code}: {response.text}. Retrying in {backoff}s...")

            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)
        except Exception as e:
            print(f"Network error: {e}. Retrying in {backoff}s...")
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

    raise Exception("Failed to call Google Gemini API after retries.")


# ==========================================
# MODEL FALLBACK HELPERS
# ==========================================


def _next_model(current_model, model_pool):
    """
    Return the next model in *model_pool* after *current_model*.
    If *current_model* is not in the pool, or is the last entry, wrap around to index 0.
    Returns None if the pool is empty.
    """
    if not model_pool:
        return None
    try:
        idx = model_pool.index(current_model)
        return model_pool[(idx + 1) % len(model_pool)]
    except ValueError:
        return model_pool[0]


def call_llm_with_model_fallback(gemini_key, openrouter_key, model, prompt, system_prompt, model_pool):
    """
    Call the LLM, enforcing a CHUNK_TIMEOUT wall-clock limit.
    If the call raises an exception OR exceeds CHUNK_TIMEOUT seconds,
    rotate to the next model in *model_pool* and retry.
    Raises RuntimeError if every model in the pool fails.

    Uses *gemini_key* for gemini-* models and *openrouter_key* for all others.
    Returns (result_text, model_that_succeeded).
    """
    # Build a deterministic rotation starting from *model*
    if model_pool and model in model_pool:
        start_idx = model_pool.index(model)
        ordered = model_pool[start_idx:] + model_pool[:start_idx]
    else:
        ordered = [model] + (model_pool or [])

    # Deduplicate while preserving order
    seen = set()
    rotation = []
    for m in ordered:
        if m not in seen:
            seen.add(m)
            rotation.append(m)

    last_exc = None
    for candidate in rotation:
        t_start = time.monotonic()
        try:
            print(f"[Model attempt] Using model: {candidate}")
            if candidate.startswith("gemini-"):
                result = call_google_gemini(gemini_key, candidate, prompt, system_prompt)
            else:
                result = call_openrouter(openrouter_key, candidate, prompt, system_prompt)

            elapsed = time.monotonic() - t_start
            if elapsed > CHUNK_TIMEOUT:
                # Call succeeded but took too long — still accept the result
                print(
                    f"  [WARN] Model {candidate} returned a result but took {elapsed:.0f}s "
                    f"(>{CHUNK_TIMEOUT}s). Accepting result and will switch model next batch."
                )
            return result, candidate

        except Exception as exc:
            elapsed = time.monotonic() - t_start
            last_exc = exc
            print(f"  [FAIL] Model {candidate} failed after {elapsed:.0f}s: {exc}. Switching to next model...")

    raise RuntimeError(f"All models in the rotation failed for this chunk. Last error: {last_exc}")


# ==========================================
# CUSTOMIZABLE PROCESSING LOGIC
# ==========================================


def extract_footnotes(result):
    """
    Extract footnotes and page-footer elements from a Docling conversion result.

    Returns a list of (page_context: str, footnote_text: str) tuples.

    Running headers that Docling mislabels as ``page_footer`` are detected via
    an all-caps + short-length heuristic so no PDF-specific strings need to be
    hardcoded here.
    """
    extracted_footnotes = []
    current_footnote = None
    current_page = "Unknown Page"

    for text_item in getattr(result.document, "texts", []):
        label = getattr(text_item, "label", "")
        text = getattr(text_item, "text", "").strip()
        if not text:
            continue

        if label == "page_header":
            current_page = text

        if label in ["footnote", "page_footer"]:
            is_marker = re.match(r"^(?:\d+[\.\s]|\[\d+\]|\*)\s*", text)
            is_page_num = re.match(r"^\d+$", text)

            if is_marker and not is_page_num:
                if current_footnote:
                    extracted_footnotes.append((current_page, current_footnote))
                current_footnote = text
            elif current_footnote and label == "page_footer":
                # Bare page numbers and short all-caps strings are running
                # headers mislabelled as footers — close the footnote instead
                # of appending them.
                is_header_clutter = bool(re.match(r"^\d+$", text)) or (text == text.upper() and len(text) <= 60)
                if not is_header_clutter:
                    current_footnote += " " + text
                else:
                    extracted_footnotes.append((current_page, current_footnote))
                    current_footnote = None
            elif current_footnote:
                extracted_footnotes.append((current_page, current_footnote))
                current_footnote = None
        elif current_footnote:
            extracted_footnotes.append((current_page, current_footnote))
            current_footnote = None

    if current_footnote:
        extracted_footnotes.append((current_page, current_footnote))

    # Table footnotes
    for table in getattr(result.document, "tables", []):
        if hasattr(table, "footnotes") and table.footnotes:
            for fn_ref in table.footnotes:
                cref = fn_ref.cref
                match = re.match(r"^#/texts/(\d+)$", cref)
                if match:
                    idx = int(match.group(1))
                    if idx < len(result.document.texts):
                        text = result.document.texts[idx].text
                        tbl_page = "Unknown Page"
                        for check_idx in range(idx, -1, -1):
                            if getattr(result.document.texts[check_idx], "label", "") == "page_header":
                                tbl_page = result.document.texts[check_idx].text
                                break
                        if not any(f[1] == text for f in extracted_footnotes):
                            extracted_footnotes.append((tbl_page, text))

    return extracted_footnotes


# ==========================================
# CORE PROCESSING LOGIC
# ==========================================


def process_chunk(
    pdf_path,
    start_page,
    end_page,
    output_dir,
    converter,
    model,
    gemini_key,
    openrouter_key,
    model_pool,
    overwrite=False,
):
    """
    Process one chunk of pages.

    Returns (success: bool, model_used: str).  *model_used* is the model that
    ultimately produced the result (may differ from *model* if fallback occurred).
    """
    chunk_name = f"chunk_{start_page:04d}_{end_page:04d}.md"
    output_path = os.path.join(output_dir, chunk_name)

    if os.path.exists(output_path) and not overwrite:
        print(f"Chunk {start_page}-{end_page} already exists. Skipping.")
        return True, model, True

    print(f"\n--- Processing pages {start_page} to {end_page} ---")
    try:
        # 1. Run Docling
        result = converter.convert(pdf_path, page_range=(start_page, end_page))
        raw_md = result.document.export_to_markdown()

        # 1.5 Extract footnotes from the document and append to markdown
        extracted_footnotes = extract_footnotes(result)
        if extracted_footnotes:
            raw_md += "\n\n### FOOTER ELEMENTS\n"
            for page_ctx, fn_text in extracted_footnotes:
                raw_md += f"- [Context: {page_ctx}] {fn_text}\n"

        # 2. Build system prompt (with optional injection)
        system_prompt = build_system_prompt()

        # 3. Clean with LLM (with model fallback on failure / timeout)
        cleaned_md, model_used = call_llm_with_model_fallback(
            gemini_key, openrouter_key, model, raw_md, system_prompt, model_pool
        )

        if model_used != model:
            print(f"  [INFO] Fell back to model: {model_used}")

        # Post-process to prevent footnote collisions across chunks
        cleaned_md = re.sub(r"\[\^(\d+)\]", rf"[^p{start_page}_\1]", cleaned_md)

        # 4. Save chunk
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(cleaned_md)
        print(f"Saved: {output_path}")
        return True, model_used, False
    except Exception as e:
        print(f"Error processing pages {start_page}-{end_page}: {e}")
        return False, model, False


# ==========================================
# MAIN EXECUTION ENTRYPOINT
# ==========================================


def merge_chunks(output_dir, dest_path):
    """
    Merge all chunk_*.md files in *output_dir* (sorted by start page) into a
    single markdown file at *dest_path*.
    """
    chunk_files = glob.glob(os.path.join(output_dir, "chunk_*.md"))
    if not chunk_files:
        print("[merge] No chunk files found — nothing to merge.")
        return

    def _start_page(filepath):
        m = re.search(r"chunk_(\d+)_", os.path.basename(filepath))
        return int(m.group(1)) if m else 0

    chunk_files.sort(key=_start_page)
    print(f"[merge] Merging {len(chunk_files)} chunks → {dest_path}")

    with open(dest_path, "w", encoding="utf-8") as out:
        for filepath in chunk_files:
            basename = os.path.basename(filepath)
            print(f"[merge]   {basename}")
            with open(filepath, encoding="utf-8") as f:
                content = f.read()
            out.write(f"\n<!-- Chunk: {basename} -->\n")
            out.write(content)
            out.write("\n\n")

    print(f"[merge] Done. Saved to: {dest_path}")


def main():
    parser = argparse.ArgumentParser(description="Batch convert PDF to Clean Markdown with LLM APIs.")
    parser.add_argument("pdf", help="Path to the PDF file")
    parser.add_argument("--start", type=int, default=1, help="Start page")
    parser.add_argument("--end", type=int, default=2246, help="End page")
    parser.add_argument("--output-dir", default="output_chunks", help="Directory to save chunks")
    parser.add_argument(
        "--delay",
        type=float,
        default=DELAY_BETWEEN_CHUNKS,
        help="Seconds to wait between chunks (default: %(default)s)",
    )
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing chunks")
    parser.add_argument(
        "--no-merge", action="store_true", help="Skip merging chunks into a single .md file on completion"
    )
    parser.add_argument(
        "--merge",
        metavar="FILENAME",
        default=None,
        help="Override the auto-generated merge filename (e.g. --merge=output.md)",
    )

    args = parser.parse_args()
    os.makedirs(args.output_dir, exist_ok=True)

    model = None

    # Resolve API keys from environment
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")

    if not openrouter_key:
        print("Error: OPENROUTER_API_KEY environment variable not set.")
        sys.exit(1)
    # Keep cycling through FREE_MODELS until one responds.
    while model is None:
        model = find_working_openrouter_model(openrouter_key, FREE_MODELS)
        if model is None:
            print(f"  [ALL FAIL] No working model found. Retrying in {ALL_FAIL_RETRY_DELAY}s...")
            time.sleep(ALL_FAIL_RETRY_DELAY)

    # Build the full model rotation pool for fallback.
    # The initially selected model is always tried first.
    model_pool = [model] + [m for m in FREE_MODELS if m != model]

    # Set up Docling with OCR disabled
    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = False
    converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)})

    start_page = max(1, args.start)
    end_page = min(2246, args.end)

    provider_label = "Google Gemini" if model.startswith("gemini-") else "OpenRouter"
    print(f"Starting chunk conversion from page {start_page} to {end_page}...")
    print(f"Using Provider: {provider_label}, Model: {model}")
    print(f"Model fallback pool: {model_pool}")

    current = start_page
    success_count = 0
    fail_count = 0
    active_model = model  # Tracks which model to try first for the next chunk

    while current <= end_page:
        chunk_end = min(current + CHUNK_SIZE - 1, end_page)
        success, active_model, skipped = process_chunk(
            args.pdf,
            current,
            chunk_end,
            args.output_dir,
            converter,
            active_model,
            gemini_key,
            openrouter_key,
            model_pool,
            args.overwrite,
        )
        if success:
            success_count += 1
            if not skipped:
                print(f"  [OK] Waiting {args.delay}s before next chunk...")
                time.sleep(args.delay)
            current += CHUNK_SIZE
        else:
            fail_count += 1
            # All models in the pool failed for this chunk — do NOT advance.
            # Rotate through the pool from the top and retry after a delay.
            active_model = model_pool[0]
            print(
                f"  [ALL FAIL] Every model failed for pages {current}-{chunk_end}. "
                f"Retrying entire model cycle in {ALL_FAIL_RETRY_DELAY}s..."
            )
            time.sleep(ALL_FAIL_RETRY_DELAY)

    print(f"\nBatch processing finished. Successfully processed {success_count} chunks. Failed {fail_count} chunks.")

    if not args.no_merge:
        if fail_count == 0:
            if args.merge:
                dest = args.merge if os.path.isabs(args.merge) else os.path.join(os.path.dirname(os.path.abspath(args.pdf)), args.merge)
            else:
                pdf_stem = os.path.splitext(os.path.basename(args.pdf))[0]
                dest = os.path.join(os.path.dirname(os.path.abspath(args.pdf)), f"{pdf_stem}.md")
            merge_chunks(args.output_dir, dest)
        else:
            print(
                f"[merge] Skipped — {fail_count} chunk(s) failed. Fix failures and re-run (without --start/--end) to merge."
            )


if __name__ == "__main__":
    main()
