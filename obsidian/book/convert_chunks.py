#!/usr/bin/env python3
"""
chunks_convert.py

Processes the entire PDF in chunks of 10 pages using docling and either
Google Gemini API or OpenRouter free models, saving intermediate chunks to allow resuming.
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests

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

# Default list of free models to try
FREE_MODELS = [
    "gemini-3.1-flash-lite",  # good enough
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
    # "nousresearch/hermes-3-llama-3.1-405b:free", # likely not designed for transcription
    "gemini-2.5-flash",
    "nvidia/nemotron-3-ultra-550b-a55b:free",  # context is probably overkill here
]

CUSTOM_INSTRUCTIONS = ""

DEFAULT_STANDARD_SYSTEM_PROMPT = """\
You are a meticulous text transcriber. Your task is to clean up OCR and markdown extracted from a document.
Output ONLY the clean markdown. Do not wrap in extra commentary or explanations.
"""

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
    import os  # Keeping os restricted inside the API call block for clean environment keys access if needed
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
# CORE PROCESSING LOGIC
# ==========================================


def process_chunk(
    file_path,
    start_page,
    end_page,
    output_dir,
    converter,
    model,
    gemini_key,
    openrouter_key,
    model_pool,
    overwrite=False,
    mode="standard",
    system_prompt_content="",
    chunk_index=None,
    save_docling=False,
    skip_llm=False,
    regen=False,
):
    """
    Process one chunk of pages or a whole file if chunk_index is provided.

    Returns (success: bool, model_used: str, skipped: bool).  *model_used* is the model that
    ultimately produced the result (may differ from *model* if fallback occurred).
    """
    file_path = Path(file_path)
    output_dir = Path(output_dir)
    filename_stem = file_path.stem

    if chunk_index is not None:
        chunk_name = f"chunk_{chunk_index:04d}_{filename_stem}.md"
        docling_name = f"docling_{chunk_index:04d}_{filename_stem}.md"
    else:
        chunk_name = f"chunk_{start_page:04d}_{end_page:04d}.md"
        docling_name = f"docling_{start_page:04d}_{end_page:04d}.md"

    output_path = output_dir / chunk_name
    docling_path = output_dir / docling_name

    if start_page is not None and end_page is not None:
        chunk_info = (
            f"Chunk {chunk_index} (pages {start_page}-{end_page})"
            if chunk_index is not None
            else f"pages {start_page}-{end_page}"
        )
    else:
        chunk_info = f"Chunk {chunk_index}" if chunk_index is not None else "whole file"

    if output_path.exists() and not overwrite:
        print(f"Output for {file_path.name} ({chunk_info}) already exists. Skipping.")
        return True, model, True

    print(f"\n--- Processing file: {file_path.name} ({chunk_info}) ---")

    try:
        # 1. Run Docling (or use existing)
        if docling_path.exists() and not regen:
            print(f"Using existing docling output: {docling_path}")
            raw_md = docling_path.read_text(encoding="utf-8")
        else:
            if start_page is not None and end_page is not None:
                result = converter.convert(file_path, page_range=(start_page, end_page))
            else:
                result = converter.convert(file_path)
            raw_md = result.document.export_to_markdown()

            # Mode specific plugins
            if mode == "poetry":
                extracted_footnotes = extract_footnotes(result)
                if extracted_footnotes:
                    raw_md += "\n\n### FOOTER ELEMENTS\n"
                    for page_ctx, fn_text in extracted_footnotes:
                        raw_md += f"- [Context: {page_ctx}] {fn_text}\n"

            if save_docling:
                docling_path.write_text(raw_md, encoding="utf-8")
                print(f"Saved docling output: {docling_path}")

        # 2. Build system prompt
        system_prompt = system_prompt_content

        # 3. Clean with LLM (with model fallback on failure / timeout)
        if skip_llm:
            cleaned_md = raw_md
            model_used = model
        else:
            cleaned_md, model_used = call_llm_with_model_fallback(
                gemini_key, openrouter_key, model, raw_md, system_prompt, model_pool
            )

        if model_used != model:
            print(f"  [INFO] Fell back to model: {model_used}")

        # if mode == "___":
        # Post-process to prevent footnote collisions across chunks
        #     cleaned_md = re.sub(r"\[\^(\d+)\]", rf"[^p{chunk_index}_\1]", cleaned_md)

        # 4. Save chunk
        output_path.write_text(cleaned_md, encoding="utf-8")
        print(f"Saved: {output_path}")
        return True, model_used, False
    except Exception as e:
        if chunk_index is not None:
            print(f"Error processing file {file_path.name}: {e}")
        else:
            print(f"Error processing pages {start_page}-{end_page}: {e}")
        return False, model, False


# ==========================================
# MAIN EXECUTION ENTRYPOINT
# ==========================================


def load_system_prompt(args):
    """
    Loads the base system prompt based on args.prompt, then appends
    CUSTOM_INSTRUCTIONS if it is non-empty.
    """
    base_prompt = None
    args_prompt = Path(args.prompt) if args.prompt else None

    # 1. Evaluate args.prompt if provided
    if args_prompt:
        if args_prompt.is_dir():
            # If it's a directory, look for the mode-based file inside it
            prompt_file = args_prompt / f"chunks_{args.mode}.txt"
            if prompt_file.exists():
                base_prompt = prompt_file.read_text(encoding="utf-8").strip()
        elif args_prompt.is_file():
            # If it's a specific file, load it directly
            base_prompt = args_prompt.read_text(encoding="utf-8").strip()
        else:
            # It's an argument, but it doesn't exist as a file or directory
            print(f"Error: prompt path not found: {args.prompt}")
            sys.exit(1)

    # 2. Try loading default file from the script directory if no prompt was provided/found
    if not base_prompt:
        script_dir = Path(__file__).resolve().parent
        prompt_file = script_dir / f"chunks_{args.mode}.txt"

        if prompt_file.exists():
            base_prompt = prompt_file.read_text(encoding="utf-8").strip()

    # 3. Fallback to standard default if nothing else worked
    if not base_prompt:
        base_prompt = DEFAULT_STANDARD_SYSTEM_PROMPT

    # 4. Append CUSTOM_INSTRUCTIONS if it exists and is non-empty
    if "CUSTOM_INSTRUCTIONS" in globals() and CUSTOM_INSTRUCTIONS and CUSTOM_INSTRUCTIONS.strip():
        base_prompt = f"{base_prompt}\n\n{CUSTOM_INSTRUCTIONS.strip()}"

    return base_prompt


def find_chunks_for_file(output_dir, stem):
    """
    Scans output_dir for chunk files belonging to a specific file stem,
    supporting both naming formats (global_chunk_idx and page ranges),
    and returns a list of sorted (index, filepath) tuples.
    """
    chunks = []
    output_dir = Path(output_dir)
    pattern1 = re.compile(rf"^chunk_(\d+)_{re.escape(stem)}\.md$")
    pattern2 = re.compile(rf"^chunk_(\d+)_(\d+)(?:_{re.escape(stem)})?\.md$")
    
    for item in output_dir.iterdir():
        if item.is_file():
            filename = item.name
            match1 = pattern1.match(filename)
            if match1:
                idx = int(match1.group(1))
                chunks.append((idx, item))
            else:
                match2 = pattern2.match(filename)
                if match2:
                    idx = int(match2.group(1))
                    chunks.append((idx, item))
                    
    chunks.sort(key=lambda x: x[0])
    return chunks


def merge_sequence_chunks(input_files, output_dir, merge_option):
    """
    Handles merging for sequence of paths.

    If merge_option == "":
        Merge chunks of single files back together to get x.md next to x.pdf.
    If merge_option is a non-empty string:
        Merge all files together into a single destination file, with '# Filename' at the top of each file's joined content.
    """
    output_dir = Path(output_dir)
    if merge_option == "":
        # Merge per-file
        for file_path in input_files:
            file_path = Path(file_path)
            file_dir = file_path.resolve().parent
            stem = file_path.stem
            dest_path = file_dir / f"{stem}.md"

            chunks = find_chunks_for_file(output_dir, stem)
            if not chunks:
                print(f"[merge] No chunks found for {stem} in {output_dir}")
                continue

            print(f"[merge] Merging {len(chunks)} chunks for {stem} → {dest_path}")

            with open(dest_path, "w", encoding="utf-8") as out:
                for idx, chunk_file in chunks:
                    print(f"[merge]   {chunk_file.name}")
                    out.write(chunk_file.read_text(encoding="utf-8"))
                    out.write("\n\n")

    elif merge_option:
        # Merge everything into a single file
        merge_path = Path(merge_option)
        if merge_path.is_absolute():
            dest_path = merge_path
        else:
            first_file_dir = Path(input_files[0]).resolve().parent
            dest_path = first_file_dir / merge_option

        print(f"[merge] Merging all files into a single output → {dest_path}")

        with open(dest_path, "w", encoding="utf-8") as out:
            for file_path in input_files:
                file_path = Path(file_path)
                basename = file_path.name
                stem = file_path.stem

                chunks = find_chunks_for_file(output_dir, stem)
                if not chunks:
                    continue

                # Write '# Filename' header before appending this file's chunks
                out.write(f"\n# {basename}\n\n")

                for idx, chunk_file in chunks:
                    print(f"[merge] Adding chunk: {chunk_file.name}")
                    out.write(chunk_file.read_text(encoding="utf-8"))
                    out.write("\n\n")

        print(f"[merge] Done. Saved to: {dest_path}")


def natural_sort_key(p):
    # Standardizing natural sort to accept either Path objects or strings gracefully
    s = p.name if isinstance(p, Path) else str(p)
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r"(\d+)", s)]


def main():
    parser = argparse.ArgumentParser(
        description="Batch convert PDF to Clean Markdown with LLM APIs.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("paths", nargs="+", help="Paths to files or directories")
    parser.add_argument("--start", type=int, default=1, help="Start page/pdf")
    parser.add_argument("--end", type=int, default=None, help="End page")
    parser.add_argument("--output-dir", default="output_chunks", help="Directory to save chunks")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing chunks")
    parser.add_argument(
        "--no-merge", action="store_true", help="Skip merging chunks into a single .md file on completion"
    )
    parser.add_argument(
        "--merge",
        metavar="FILENAME",
        nargs="?",
        const="",
        default="",
        help="Override the auto-generated merge filename or set to empty string to merge chunks of single files back together.",
    )

    parser.add_argument(
        "--mode",
        default="standard",
        help="Processing mode. Will cause the program to attempt to look for a system prompt from chunks_{mode}.txt the prompts directory",
    )
    parser.add_argument(
        "--prompt",
        type=str,
        default=str(Path(__file__).resolve().parent / "prompts"),
        help="Path to the prompt file, or directory containing prompt files in the form chunk_{mode}.txt.",
    )

    parser.add_argument(
        "--extract",
        nargs="?",
        const="store",
        default=None,
        choices=["store", "regen", "direct", "preview"],
        help=(
            "If specified, saves raw extraction outputs alongside chunks. "
            "Options include: "
            "- None; "
            "- store: save raw outputs; "
            "- preview: print first extraction to stdout and exits; "
            "- direct: skip remote LLM cleaning pass; "
            "- regen: forces regeneration of docling artifacts."
        ),
    )

    parser.add_argument(
        "--docling",
        default="standard",
        choices=["standard", "ocr", "vlm"],
        help="Configure the extraction pipeline architecture engine.",
    )

    parser.add_argument(
        "--delay",
        type=float,
        default=DELAY_BETWEEN_CHUNKS,
        help="Seconds to wait between chunks",
    )

    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Contextual heavy-imports deferred inside main to completely remove generic execution delays
    import os  # Deferred import required exclusively for environment resolution safely inside core block
    import pypdf
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    # Load system prompt content
    system_prompt_content = load_system_prompt(args)

    model = None
    model_pool = []
    skip_llm = args.extract == "direct"
    save_docling = args.extract in ("store", "regen")
    regen_active = args.extract == "regen"

    # Resolve API keys from environment
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")

    if not skip_llm:
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

    # Set up Docling
    if args.docling == "vlm":
        from docling.datamodel.pipeline_options import VlmPipelineOptions
        from docling.pipeline.vlm_pipeline import VlmPipeline

        pipeline_options = VlmPipelineOptions()
        pdf_format_option = PdfFormatOption(
            pipeline_cls=VlmPipeline,
            pipeline_options=pipeline_options,
        )
        print("Using VLM-based document pipeline.")
        if not os.environ.get("HF_TOKEN"):
            print("Warning: HF_TOKEN environment variable is not set. Downloads might be slow or rate-limited.")
    else:
        pipeline_options = PdfPipelineOptions()
        pipeline_options.do_ocr = args.docling == "ocr"
        if args.docling == "ocr":
            try:
                from docling.datamodel.pipeline_options import OcrMacOptions

                pipeline_options.ocr_options = OcrMacOptions()
                pipeline_options.ocr_options.force_full_page_ocr = True
                print("Using macOS Vision OCR (ocrmac) with forced full-page OCR.")
            except Exception as e:
                print(f"Vision OCR (ocrmac) not available, using default OCR: {e}")
                if hasattr(pipeline_options.ocr_options, "force_full_page_ocr"):
                    pipeline_options.ocr_options.force_full_page_ocr = True
        pdf_format_option = PdfFormatOption(pipeline_options=pipeline_options)

    converter = DocumentConverter(format_options={InputFormat.PDF: pdf_format_option})

    # Expand paths to input files using Pathlib
    input_files = []
    for path_str in args.paths:
        path_obj = Path(path_str)
        if path_obj.is_dir():
            dir_files = [f for f in path_obj.iterdir() if f.is_file() and not f.name.startswith(".")]
            dir_files.sort(key=natural_sort_key)
            input_files.extend(dir_files)
        else:
            # Handles string wildcards safely via pure pathlib matching
            parent_dir = path_obj.parent if path_obj.parent != Path() else Path(".")
            glob_pattern = path_obj.name
            globbed = [f for f in parent_dir.glob(glob_pattern) if f.is_file()]
            if globbed:
                globbed.sort(key=natural_sort_key)
                input_files.extend(globbed)
            else:
                input_files.append(path_obj)

    # Keep only existing files
    input_files = [f for f in input_files if f.is_file()]

    # Make sure the final input files list is sorted naturally
    input_files.sort(key=natural_sort_key)

    if not input_files:
        print("Error: No files found to process.")
        sys.exit(1)

    is_sequence_mode = (len(input_files) > 1) or (args.mode != "poetry")

    global_chunk_idx = 1
    if is_sequence_mode:
        # --start and --end slice files in sequence mode
        start_file_idx = max(1, args.start)
        end_file_idx = min(len(input_files), args.end or len(input_files))

        # Calculate initial chunk index
        for idx in range(start_file_idx - 1):
            f_path = input_files[idx]
            reader = pypdf.PdfReader(f_path)
            p_count = len(reader.pages)

            f_chunks = (p_count + CHUNK_SIZE - 1) // CHUNK_SIZE
            global_chunk_idx += f_chunks

        # Slice input files list
        input_files = input_files[start_file_idx - 1 : end_file_idx]

        if not input_files:
            print("Error: No files in the selected range to process.")
            sys.exit(1)
    else:
        start_page = max(1, args.start)
        global_chunk_idx = ((start_page - 1) // CHUNK_SIZE) + 1

    if args.extract == "preview":
        target_file = input_files[0]

        print(f"--- Docling Preview for {target_file.name} ---")

        try:
            filename_stem = target_file.stem
            docling_name = f"docling_{global_chunk_idx:04d}_{filename_stem}.md"
            dest_path = output_dir / docling_name

            if dest_path.exists() and not regen_active:
                print(f"Using existing docling output: {dest_path}")
                raw_md = dest_path.read_text(encoding="utf-8")
            else:
                if not is_sequence_mode:
                    # --- Single File Mode ---
                    start_page = max(1, args.start)

                    reader = pypdf.PdfReader(target_file)
                    # Fallback to total pages if args.end is None
                    end_page = min(len(reader.pages), args.end or len(reader.pages))

                    result = converter.convert(target_file, page_range=(start_page, end_page))
                else:
                    # --- Sequence Mode ---
                    reader = pypdf.PdfReader(target_file)
                    p_count = len(reader.pages)

                    # args.end picks the end page range, or defaults to 2 if None
                    end_page_fallback = args.end if args.end is not None else 2
                    preview_end_page = min(p_count, end_page_fallback)

                    print(f"Limiting preview to pages 1-{preview_end_page} of {p_count} total pages.")

                    result = converter.convert(target_file, page_range=(1, preview_end_page))

                raw_md = result.document.export_to_markdown()
                dest_path.write_text(raw_md, encoding="utf-8")
                print(f"\n[Preview] Saved docling output to: {dest_path}")

            print(raw_md)
        except Exception as e:
            print(f"Error during docling preview: {e}")
        sys.exit(0)

    if not skip_llm:
        provider_label = "Google Gemini" if model.startswith("gemini-") else "OpenRouter"
        print(f"Using Provider: {provider_label}, Model: {model}")
        print(f"Model fallback pool: {model_pool}")
    else:
        print("Skipping LLM cleaning pass due to --extract=direct.")

    success_count = 0
    fail_count = 0
    active_model = model  # Tracks which model to try first for the next chunk

    print(f"Starting chunked conversion of {len(input_files)} file(s)...")

    # Loop through each input file
    for file_path in input_files:
        is_pdf = True
        try:
            reader = pypdf.PdfReader(file_path)
            total_pages = len(reader.pages)
        except Exception as e:
            print(f"Error reading {file_path.name} as pdf, attempting whole file: {e}")
            is_pdf = False
            total_pages = 1

        # start and end indicate pages for single files
        if not is_sequence_mode:
            start_page = max(1, args.start)
            if is_pdf:
                end_page = min(total_pages, args.end) if args.end is not None else total_pages
            else:
                end_page = 1
        else:
            start_page = 1
            end_page = total_pages

        if start_page > end_page:
            print(
                f"Skipping {file_path.name}: start page ({start_page}) is greater than end page ({end_page})."
            )
            continue

        print(
            f"Processing {file_path.name} (pages {start_page} to {end_page}, total pages in file: {total_pages})..."
        )

        current = start_page
        while current <= end_page:
            if is_pdf:
                chunk_end = min(current + CHUNK_SIZE - 1, end_page)
                start_p = current
                end_p = chunk_end
                print_msg = f"pages {start_p}-{end_p}"
            else:
                start_p = None
                end_p = None
                print_msg = "whole file"

            success, active_model, skipped = process_chunk(
                file_path=file_path,
                start_page=start_p,
                end_page=end_p,
                output_dir=args.output_dir,
                converter=converter,
                model=active_model,
                gemini_key=gemini_key,
                openrouter_key=openrouter_key,
                model_pool=model_pool,
                overwrite=args.overwrite,
                mode=args.mode,
                system_prompt_content=system_prompt_content,
                chunk_index=global_chunk_idx,
                save_docling=save_docling,
                skip_llm=skip_llm,
                regen=regen_active,
            )
            if success:
                success_count += 1
                if not skipped:
                    if not skip_llm:
                        print(f"  [OK] Waiting {args.delay}s before next chunk...")
                        time.sleep(args.delay)
                if is_pdf:
                    current += CHUNK_SIZE
                else:
                    current = end_page + 1
                global_chunk_idx += 1
            else:
                fail_count += 1
                # All models in the pool failed for this chunk — do NOT advance.
                # Rotate through the pool from the top and retry after a delay.
                active_model = model_pool[0] if model_pool else None
                print(
                    f"  [ALL FAIL] Every model failed for {file_path.name} ({print_msg}). "
                    f"Retrying entire model cycle in {ALL_FAIL_RETRY_DELAY}s..."
                )
                time.sleep(ALL_FAIL_RETRY_DELAY)

    print(f"\nBatch processing finished. Successfully processed {success_count} chunks. Failed {fail_count} chunks.")

    # Merge logic
    should_merge = not args.no_merge

    if should_merge:
        if fail_count == 0:
            merge_sequence_chunks(input_files, args.output_dir, args.merge)
        else:
            print(f"[merge] Skipped — {fail_count} chunk(s) failed. Fix failures and re-run to merge.")


if __name__ == "__main__":
    main()