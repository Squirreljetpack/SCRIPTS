#!/usr/bin/env python3
"""
compile_html_poetry.py

Converts HTML books of poetry into clean Markdown.
Uses a high-reasoning planning LLM to define logical chunk ranges and context slices,
and a standard transcribing LLM to convert the text and resolve footnotes/variants.
"""

import argparse
import inspect
import json
import os
import re
import sys
import time

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

# =========================================================================
# GLOBAL CONSTANTS & CONFIGURATION
# =========================================================================

# Outline Parsing & Preview
OUTLINE_TEXT_THRESHOLD = 50
PREVIEW_CHUNK_COUNT = 5
KEPT_ATTRS = {
    "id",
    "class",
    "name",
    "href",
}

# Network & API Limits
DELAY_BETWEEN_CHUNKS = 20
API_REQUEST_TIMEOUT = 15 * 60
API_MAX_RETRIES = 5
API_INITIAL_BACKOFF = 4

MAX_PLANNER_CONTINUATIONS = 8

REQUEST_TIMEOUT = 30
WEB_RETRY_DELAY = 5
WEB_RETRIES = 3
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


# Global session tracking for broken endpoints
BLACKLISTED_MODELS = set()

# LLM Configuration
CONFIG = {
    # System prompt for the Transcriber LLM (No Reasoning)
    "transcribe_system_prompt": inspect.cleandoc(
        """You convert HTML books of poetry into clean Markdown.

        HEADINGS — use exactly:
          #    Volume/book title
          ##   Major section or part
          ###  Specific poem title
          #### Subsection of a poem (e.g. Prologue, Part First)
          ##### Various subheadings

        If the chunk starts mid-poem with no heading, output verse directly — no heading invented.

        VERSE — one line of verse per output line; blank line between stanzas;
        Do attach all footnote references on the text such as [^A], [^2];
        no line numbers; no added indentation.
        TABLES — HTML tables in the source hold poem lines, variants, footnotes, and line numbers in columns.
        Flatten to plain verse: output the poem lines, but attach the variants and footnotes (which are provided in the ADDITIONAL CONTEXT block if needed).
        FOOTNOTES & VARIANTS — append their definitions at the end of your output using standard Markdown footnote format (e.g. [^A]: definition).
        REMOVE: page numbers, margin line-counts (5, 10, 15…), publisher boilerplate, nav links, the raw FOOTNOTES and VARIANTS blocks from the text flow.
        OUTPUT: clean Markdown only — no code fences, no preamble.
        Preserve the author's spelling and punctuation exactly."""
    ),
    "planner_custom_instructions": inspect.cleandoc(
        """- Make sure the `output_range` of your first chunk starts exactly at the start of the first actual poem (skipping TOC and front matter), and the `output_range` of your last chunk ends exactly at the end of the last actual poem (skipping the end matter/TOC/license).
        - In this document, the contexts should contain footnote and variant bodies. Many of the poems are tables, whose columns are for the main verse, variant links, footnote links and line numbers. Look at the links inside this table: for example <a name="fr1v62"></a><a href="#1v62">62</a> might indicate the inclusion of a section like the following: <a name="1v62">...<a href="#fr1v62">return</a><br>.
        - You should not need to exceed 2 ranges for the contexts array in this document. A poem can reference some links to variants or footnotes. Since the variant/footnotes referenced are consecutive, you usually need exactly 2 ranges - one for the segment of footnote body definitions, one for the segment of variant body definitions. To find the correct ranges for the contexts array, trace the href anchors from the main text to their corresponding definitions at the bottom of the poem. If present, group all contiguous variant definitions into one range, and all contiguous footnote definitions into a second range."
        """
    ),
    "planner_system_prompt": inspect.cleandoc(
        """You are a document planner. Your task is to split a HTML into logical chunks of roughly 500-2000 words each. Chunks can be multiple sections, or part of a section, but favor logical chunk starts and ends WHENEVER possible, so do not join sections together when you already have 500 words.
        You are given a collapsed, line-numbered outline of the book's HTML. The line numbers correspond to the 1-based line numbers in the original HTML file.
        You must analyze the book contents and output a JSON plan containing a list of chunks. These chunks should together constitute all of and only the MAIN content of the html.

        For each chunk, specify:
        1. `chunk_number`: sequential number starting at 1.
        2. `title`: a descriptive name of the section being transcribed.
        3. `output_range`: a two-element array `[start_line, end_line]` containing the chunk of HTML to be transcribed into Markdown.
        4. `contexts`: a list of two-element arrays (e.g., `[[start_line, end_line], ...]`) that contain all ADDITIONAL HTML necessary to transcribe this chunk (like footnotes). This is not the place to put actual text, only sections which define extra data (mainly footnotes) referenced in the output_range.
        - `output_range` and `contexts` should be DISJOINT.

        5. Do NOT list individual lines or single-line ranges repeatedly. If multiple footnotes, variants, or contexts are near each other, MERGE them into a single continuous bounding range spanning the entire list of footnote/variants involved.
        6. COMPLETION & LIMITS: If you have successfully planned the entire book up to the very end of the outline, add a `"complete": true` field as the VERY LAST key in the JSON object (after the `"chunks"` array, right before the closing brace `}`). If the outline is massive and you need to stop or pause generating due to output limits, plan as many chunks as you can and set `"complete": false` as the very last key of your partial JSON. This ensures that the `"complete"` status is only read if generation finished cleanly without truncation.

        IMPORTANT NOTES:
        {planner_custom_instructions}

        Output Schema:
        Your final output MUST contain a raw JSON object matching this exact schema:
        {{
        "chunks": [
            {{
            "chunk_number": 1,
            "title": "Peter Bell: Preface & Prologue",
            "output_range": [136, 1117],
            "contexts": [
                [6738, 6780],
                [4662, 4963]
            ]
            }}
        ],
        "complete": true
        }}"""
    ),
    # Postprocessing regex patterns
    "postprocess_regex_rules": [
        (r"^ {0,3}(\[\^[^\]]+\]: )\[\d+\]\s*:?\s*", r"\1"),
        (r"^ {0,3}(\[\^[^\]]+\]: )\d{4}\b\.?\s*", r"\1"),
        (r"^ {0,3}(\[\^[^\]]+\]: )\s*:?\s*", r"\1"),
    ],
    # High-reasoning choices for planning
    "planner_models": [
        "gemini-3.1-pro-preview",
        "gemini-3-flash-preview",
        "deepseek/deepseek-v4-pro",
        # "openai/o3-mini",
        # "openai/o1-mini",
        # "gemini-3.5-flash",
        "anthropic/claude-3.7-sonnet", # hopefully unreachable
    ],
    # Standard instruction models for direct transcription
    "transcriber_models": [
        "google/gemma-4-31b-it:free",
        "google/gemma-4-26b-a4b-it:free",
        "qwen/qwen3-next-80b-a3b-instruct:free",
        "gemini-3.5-flash",  # not flash-lite for safety
        "gemini-3.1-flash-lite",  # self attention should mean its probably ok at understanding complicated html
        # good fallback for rate limiting: very slow but never errors out with 429
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        # "meta-llama/llama-3.3-70b-instruct:free",
    ],
}

# =========================================================================
# CORE API CALLERS WITH EXPONENTIAL BACKOFF
# =========================================================================


def call_llm_with_backoff(url, headers, payload, max_retries=API_MAX_RETRIES, initial_backoff=API_INITIAL_BACKOFF):
    """
    Centralized execution pipeline for hitting API routes with structured
    exponential backoff. Silent during retries; only surfaces failures at terminal states.
    Tracks failed HTTP status codes across attempts to display in the final exception.
    """
    backoff = initial_backoff
    status_codes = []

    for attempt in range(max_retries):
        is_last_attempt = attempt == max_retries - 1
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=API_REQUEST_TIMEOUT)

            if r.status_code == 200:
                return r.json()

            if r.status_code == 400:
                raise Exception(f"HTTP_400_BAD_REQUEST: {r.text}")

            status_codes.append(f"HTTP {r.status_code}")

            if r.status_code in [429, 500, 502, 503]:
                if not is_last_attempt:
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                continue
            raise Exception(f"Terminal HTTP {r.status_code}: {r.text}")

        except requests.exceptions.RequestException as e:
            if is_last_attempt:
                print(f" -> Network failure connection aborted ({e})", file=sys.stderr)
            if not is_last_attempt:
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    history_str = ", ".join(status_codes) if status_codes else ""
    raise Exception(f"API Target execution failed after {max_retries} attempts. {history_str}")


def call_google_gemini(api_key, model, prompt, system_prompt, is_planning=False, max_retries=2):
    url = "https://generativelanguage.googleapis.com/v1beta/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}],
    }

    response_data = call_llm_with_backoff(url, headers, payload, max_retries=max_retries)
    if "error" in response_data:
        raise Exception(f"Internal Provider Error: {response_data['error'].get('message')}")
    return response_data["choices"][0]["message"]["content"]


def call_openai(api_key, model, prompt, system_prompt, max_retries=2):
    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}],
    }

    response_data = call_llm_with_backoff(url, headers, payload, max_retries=max_retries)
    if "error" in response_data:
        raise Exception(f"Internal Provider Error: {response_data['error'].get('message')}")
    return response_data["choices"][0]["message"]["content"]


def call_openrouter(api_key, model, prompt, system_prompt, is_planning=False, max_retries=2):
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/google/antigravity",
        "X-Title": "Antigravity HTML Converter",
    }
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}],
    }

    response_data = call_llm_with_backoff(url, headers, payload, max_retries=max_retries)
    if "error" in response_data:
        raise Exception(f"Internal Provider Error: {response_data['error'].get('message')}")
    return response_data["choices"][0]["message"]["content"]


def call_llm_auto(model_list, prompt, system_prompt, is_planning=False, continuous_retry=False):
    gemini_key = os.environ.get("GEMINI_API_KEY")
    or_key = os.environ.get("OPENROUTER_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")

    round_num = 1
    while True:
        last_exception = None
        active_pool = [m for m in model_list if m not in BLACKLISTED_MODELS]

        if not active_pool:
            raise Exception("All configured targets in the model list have been blacklisted due to terminal errors.")

        for idx, raw_model in enumerate(active_pool, 1):
            print(f" -> [{idx}/{len(active_pool)}] Attempting evaluation pool target: {raw_model} ...")

            try:
                if raw_model.startswith("gemini-") or "gemini-3" in raw_model:
                    if gemini_key:
                        res = call_google_gemini(
                            gemini_key, raw_model, prompt, system_prompt, is_planning=is_planning, max_retries=2
                        )
                        print("    Success via Direct Google Gemini API.")
                        return res, raw_model, "google"
                    print("    Skipped: GEMINI_API_KEY environment variable missing.")
                    continue

                if raw_model.startswith("openai/"):
                    clean_model = raw_model.replace("openai/", "")
                    if openai_key:
                        res = call_openai(openai_key, clean_model, prompt, system_prompt, max_retries=2)
                        print("    Success via Direct OpenAI API.")
                        return res, clean_model, "openai"
                    print("    Skipped: OPENAI_API_KEY environment variable missing.")
                    continue

                if raw_model.startswith("openrouter/"):
                    clean_model = raw_model.replace("openrouter/", "")
                    if or_key:
                        res = call_openrouter(
                            or_key, clean_model, prompt, system_prompt, is_planning=is_planning, max_retries=2
                        )
                        print("    Success via OpenRouter link segment.")
                        return res, clean_model, "openrouter"
                    print("    Skipped: OPENROUTER_API_KEY environment variable missing.")
                    continue

                if or_key:
                    res = call_openrouter(
                        or_key, raw_model, prompt, system_prompt, is_planning=is_planning, max_retries=2
                    )
                    print("    Success via fallback OpenRouter routing.")
                    return res, raw_model, "openrouter"
                print("    Skipped: Model string has no explicit prefix and OPENROUTER_API_KEY is missing.")

            except Exception as e:
                if "HTTP_400_BAD_REQUEST" in str(e):
                    error_msg = str(e).split("HTTP_400_BAD_REQUEST:", 1)[-1].strip()
                    print(f" [!] 400 Client Error: {error_msg}", file=sys.stderr)
                    if continuous_retry:
                        print(f"     Blacklisting {raw_model} for the rest of this execution session.", file=sys.stderr)
                        BLACKLISTED_MODELS.add(raw_model)
                else:
                    print(f"    Target endpoint skipped/failed: {e}", file=sys.stderr)

                last_exception = e

        if not continuous_retry:
            raise Exception(f"All target models in configuration file failed. Last Exception Trace: {last_exception}")

        print(f"\n[!] Complete model pool exhausted (Round {round_num}). Cooling down 30s before rotating list...\n")
        time.sleep(30)
        round_num += 1


# =========================================================================
# DOCUMENT HANDLING & EXTRACTION LOGIC
# =========================================================================


def clean_text(text):
    return " ".join(str(text).split()).strip()


def fetch_html(url_or_path, book_dir):
    os.makedirs(book_dir, exist_ok=True)
    cache_path = os.path.join(book_dir, "source.html")

    if os.path.exists(url_or_path):
        print(f"Loading HTML from local file: {url_or_path}")
        with open(url_or_path, encoding="utf-8") as f:
            return f.read()

    if os.path.exists(cache_path):
        print(f"Loading cached HTML from: {cache_path}")
        with open(cache_path, encoding="utf-8") as f:
            return f.read()

    print(f"Downloading: {url_or_path} ...")
    headers = {"User-Agent": USER_AGENT}

    for i in range(WEB_RETRIES):
        try:
            r = requests.get(url_or_path, headers=headers, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                with open(cache_path, "w", encoding="utf-8") as f:
                    f.write(r.text)
                print(f"Saved HTML cache to: {cache_path}")
                return r.text
            print(f"Failed download (Status {r.status_code}), retrying...")
            time.sleep(WEB_RETRY_DELAY)
        except Exception as e:
            print(f"Connection error: {e}, retrying...")
            time.sleep(WEB_RETRY_DELAY)

    raise Exception(f"Failed to fetch HTML from {url_or_path}")


def parse_collapsed_numbered_outline(html_content, text_threshold=OUTLINE_TEXT_THRESHOLD):
    soup = BeautifulSoup(html_content, "html.parser")
    body = soup.body if soup.body else soup

    output_lines = []
    node_lines = {}
    current_line = 1

    for node in soup.descendants:
        if isinstance(node, Tag):
            if node.sourceline is not None:
                current_line = node.sourceline
            node_lines[id(node)] = current_line
        elif isinstance(node, NavigableString):
            s = str(node)
            leading_chunk = s[: len(s) - len(s.lstrip("\n\r "))]
            start_line_of_text = current_line + leading_chunk.count("\n")
            node_lines[id(node)] = start_line_of_text
            current_line += s.count("\n")

    def get_exact_line(node):
        return node_lines.get(id(node), 1)

    INLINE_TAGS = {"br", "i", "b", "em", "strong", "span", "sup", "sub"}

    def format_attrs(node):
        attrs = []
        for k, v in node.attrs.items():
            if k in KEPT_ATTRS:
                val = " ".join(v) if isinstance(v, list) else v
                attrs.append(f'{k}="{val}"')
        return (" " + " ".join(attrs)) if attrs else ""

    def traverse(node, depth=0):
        indent = "  " * depth

        if isinstance(node, NavigableString):
            text = str(node).strip()
            if not text:
                return
            line = get_exact_line(node)
            if len(text) > text_threshold:
                snippet = text[:text_threshold].replace("\n", " ")
                output_lines.append(f'{line}: {indent}[{len(text)} chars: "{snippet}..."]')
            else:
                output_lines.append(f"{line}: {indent}{text}")
            return

        if isinstance(node, Tag):
            if node.name in {"head", "style", "script", "noscript", "meta", "link"}:
                return

            line = get_exact_line(node)
            open_tag = f"<{node.name}{format_attrs(node)}>"
            close_tag = f"</{node.name}>"

            clean_children = [c for c in node.children if not (isinstance(c, NavigableString) and not str(c).strip())]

            if not clean_children:
                output_lines.append(f"{line}: {indent}{open_tag.replace('>', ' />')}")
                return

            is_full_text_block = all(
                isinstance(c, NavigableString) or (isinstance(c, Tag) and c.name in INLINE_TAGS) for c in clean_children
            )

            if is_full_text_block:
                combined_text = node.get_text(separator=" ", strip=True)
                if len(combined_text) > text_threshold:
                    snippet = combined_text[:text_threshold].replace("\n", " ") if combined_text else "[no text]"
                    output_lines.append(
                        f'{line}: {indent}{open_tag}[... {len(combined_text)} chars: "{snippet}..."]{close_tag}'
                    )
                    return

            output_lines.append(f"{line}: {indent}{open_tag}")

            if node.name in {"table", "tbody", "ul", "ol"}:
                target_tag = "tr" if node.name in {"table", "tbody"} else "li"
                repeating_elements = [c for c in node.children if isinstance(c, Tag) and c.name == target_tag]

                if len(repeating_elements) > 10:
                    first_5 = set(repeating_elements[:5])
                    last_5 = set(repeating_elements[-5:])
                    collapsed_marker_added = False

                    for child in node.children:
                        if child in repeating_elements:
                            if child in first_5 or child in last_5:
                                traverse(child, depth + 1)
                            elif not collapsed_marker_added:
                                start_l = get_exact_line(repeating_elements[5])
                                hidden_count = len(repeating_elements) - 10
                                output_lines.append(f"{start_l}: {indent}  [{hidden_count}x <{target_tag}>]")
                                collapsed_marker_added = True
                        else:
                            traverse(child, depth + 1)
                    output_lines.append(f"{line}: {indent}{close_tag}")
                    return

            idx = 0
            while idx < len(clean_children):
                child = clean_children[idx]
                is_inline_or_text = isinstance(child, NavigableString) or (
                    isinstance(child, Tag) and child.name in INLINE_TAGS
                )

                if is_inline_or_text:
                    end_idx = idx
                    text_parts = []

                    while end_idx < len(clean_children):
                        c = clean_children[end_idx]
                        if isinstance(c, NavigableString):
                            text_parts.append(str(c))
                            end_idx += 1
                        elif isinstance(c, Tag) and c.name in INLINE_TAGS:
                            tag_text = c.get_text(separator=" ", strip=True)
                            if tag_text:
                                text_parts.append(tag_text)
                            end_idx += 1
                        else:
                            break

                    combined_text = " ".join([p for p in text_parts if p]).strip()

                    if len(combined_text) > text_threshold:
                        chunk_start_line = get_exact_line(child)
                        snippet = combined_text[:text_threshold].replace("\n", " ") if combined_text else "[no text]"
                        output_lines.append(
                            f'{chunk_start_line}: {indent}  [{len(combined_text)} chars: "{snippet}..."]'
                        )
                        idx = end_idx
                        continue
                    while idx < end_idx:
                        c = clean_children[idx]
                        if isinstance(c, Tag) and c.name == "br":
                            br_start_line = get_exact_line(c)
                            br_count = 0
                            temp_idx = idx
                            while (
                                temp_idx < end_idx
                                and isinstance(clean_children[temp_idx], Tag)
                                and clean_children[temp_idx].name == "br"
                            ):
                                br_count += 1
                                temp_idx += 1

                            if br_count > 1:
                                output_lines.append(f"{br_start_line}: {indent}  <br /> [{br_count}x]")
                                idx = temp_idx
                            else:
                                output_lines.append(f"{br_start_line}: {indent}  <br />")
                                idx += 1
                        else:
                            traverse(c, depth + 1)
                            idx += 1
                    continue

                traverse(child, depth + 1)
                idx += 1

            output_lines.append(f"{line}: {indent}{close_tag}")

    traverse(body)
    return output_lines


def postprocess_transcription(markdown_text):
    cleaned = markdown_text.strip()

    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    for pattern, replacement in CONFIG["postprocess_regex_rules"]:
        cleaned, count = re.subn(pattern, replacement, cleaned)
        # Log the result if any replacements were made
        if count > 0:
            print(f"Replaced {count} instances of pattern: {pattern}")
    return cleaned


def _clean_and_parse_json(text):
    """
    Safely locates the JSON object within the text to bypass reasoning/CoT blocks
    emitted by reasoning models (e.g. o1, gemini-3.1-pro).
    """
    text = text.strip()
    start_idx = text.find("{")
    if start_idx != -1:
        clean_text = text[start_idx:]
        clean_text = re.sub(r"\s*```$", "", clean_text)
        try:
            return json.loads(clean_text)
        except json.JSONDecodeError:
            pass

    return json.loads(text)


def _looks_truncated(text):
    t = text.rstrip()
    return not (t.endswith("}") or t.endswith("]}"))


def slice_outline(outline_lines, start_line):
    """
    Returns outline lines starting from start_line.
    Each line starts with 'line_number: '
    """
    sliced = []
    current_val = 0
    for line in outline_lines:
        m = re.match(r"^(\d+):", line.strip())
        if m:
            current_val = int(m.group(1))
        if current_val >= start_line:
            sliced.append(line)
    return sliced


def has_content_to_plan(outline_lines):
    # Check if there is any line that has text/headings or start tags (excluding closing tags)
    for line in outline_lines:
        cleaned = re.sub(r"^\d+:", "", line).strip()
        cleaned = re.sub(r"<\/[^>]+>", "", cleaned).strip()
        if re.search(r"[a-zA-Z0-9]", cleaned):
            return True
    return False


def extract_complete_chunks(text):
    """
    Attempts to parse the entire text as JSON. If that fails, scans for
    balanced curly brace blocks representing individual chunks.
    """
    # 1. Try full parse first
    try:
        data = _clean_and_parse_json(text)
        if isinstance(data, dict) and "chunks" in data and isinstance(data["chunks"], list):
            return data["chunks"]
        if isinstance(data, list):
            if all(isinstance(x, dict) and "chunk_number" in x and "output_range" in x for x in data):
                return data
    except Exception:
        pass

    # 2. Scan for individual balanced chunk objects
    chunks = []
    n = len(text)
    i = 0
    while i < n:
        if text[i] == '{':
            # Try to find the matching '}'
            brace_count = 1
            j = i + 1
            in_string = False
            escape = False
            while j < n and brace_count > 0:
                char = text[j]
                if escape:
                    escape = False
                elif char == '\\':
                    escape = True
                elif char == '"':
                    in_string = not in_string
                elif not in_string:
                    if char == '{':
                        brace_count += 1
                    elif char == '}':
                        brace_count -= 1
                j += 1
            
            if brace_count == 0:
                candidate = text[i:j]
                try:
                    obj = json.loads(candidate)
                    if isinstance(obj, dict) and "chunk_number" in obj and "output_range" in obj:
                        chunks.append(obj)
                except json.JSONDecodeError:
                    pass
                i = j
                continue
        i += 1
    return chunks


def get_html_slices_for_chunk(chunk, raw_lines):
    out_range = chunk.get("output_range", [])
    if not out_range or len(out_range) < 2:
        raise ValueError(f"Invalid output_range in chunk {chunk.get('chunk_number', '?')}: {out_range}")

    out_start, out_end = int(out_range[0]), int(out_range[1])
    out_slice_str = "\n".join(raw_lines[out_start - 1 : out_end])

    extra_slices = []
    for ctx in chunk.get("contexts", []):
        if not ctx or len(ctx) < 2:
            continue
        start, end = int(ctx[0]), int(ctx[1])
        slice_str = "\n".join(raw_lines[start - 1 : end])
        extra_slices.append((start, end, slice_str))

    return out_start, out_end, out_slice_str, extra_slices


def build_plan_preview(plan_data, raw_lines):
    chunks = plan_data.get("chunks", [])[:PREVIEW_CHUNK_COUNT]
    parts = []
    for c in chunks:
        out_start, out_end, out_slice_str, extra_slices = get_html_slices_for_chunk(c, raw_lines)
        header = (
            f"{'=' * 70}\n"
            f"Chunk {c.get('chunk_number', '?')}: {c.get('title', '')}\n"
            f"  output_range: lines {out_start}–{out_end}\n"
            f"{'=' * 70}"
        )
        ctx_parts = [f"  --- PRIMARY TEXT (output_range): lines {out_start}–{out_end} ---\n{out_slice_str}"]
        for i, (start, end, html_slice) in enumerate(extra_slices):
            ctx_parts.append(f"  --- ADDITIONAL CONTEXT[{i + 1}]: lines {start}–{end} ---\n{html_slice}")
        parts.append(header + "\n" + "\n\n".join(ctx_parts))
    return "\n\n".join(parts)


def extract_html_title(html_content):
    """
    Return a filesystem-safe version of the HTML <title> tag, falling back to
    an empty string if no title is found.
    """
    soup = BeautifulSoup(html_content, "html.parser")
    tag = soup.find("title")
    if not tag:
        return ""
    raw = tag.get_text(separator=" ", strip=True)
    # Replace any character that isn't alphanumeric, space, hyphen, or dot
    sanitized = re.sub(r"[^\w\s\-\.]", "", raw).strip()
    # Collapse internal whitespace to single spaces
    sanitized = re.sub(r"\s+", " ", sanitized)
    return sanitized


def validate_no_overlaps(chunks):
    """
    Validates that the output_ranges of the chunks do not overlap.
    Raises ValueError if an overlap is detected.
    """
    sorted_chunks = []
    for c in chunks:
        out_range = c.get("output_range")
        if out_range and len(out_range) >= 2:
            try:
                start = int(out_range[0])
                end = int(out_range[1])
                sorted_chunks.append((start, end, c.get("chunk_number", "?"), c.get("title", "")))
            except (ValueError, TypeError):
                pass

    sorted_chunks.sort(key=lambda x: x[0])

    for i in range(len(sorted_chunks) - 1):
        curr_start, curr_end, curr_num, curr_title = sorted_chunks[i]
        next_start, next_end, next_num, next_title = sorted_chunks[i+1]
        
        if next_start <= curr_end:
            raise ValueError(
                f"Overlap detected between chunk {curr_num} ('{curr_title}', range [{curr_start}, {curr_end}]) "
                f"and chunk {next_num} ('{next_title}', range [{next_start}, {next_end}])."
            )


def process_book(url, output_dir, delay=DELAY_BETWEEN_CHUNKS, plan_mode=None, no_merge=False, merge_filename=None):
    print(f"\n===== Converting: {url} =====")

    url_slug = re.sub(r"[^a-zA-Z0-9]", "_", url.split("/")[-1].split(".")[0])
    book_dir = os.path.join(output_dir, url_slug)
    os.makedirs(book_dir, exist_ok=True)

    html_content = fetch_html(url, book_dir)
    raw_lines = html_content.splitlines()

    plan_path = os.path.join(book_dir, "plan.json")
    outline_path = os.path.join(book_dir, "outline.txt")

    needs_outline = plan_mode is not None or not os.path.exists(plan_path)
    if needs_outline:
        outline = parse_collapsed_numbered_outline(html_content)
        with open(outline_path, "w", encoding="utf-8") as f:
            f.write("\n".join(outline))
        print(f"Outline saved to: {outline_path}")

    if plan_mode == "outline":
        print("Plan mode is 'outline'. Stopping execution.")
        return

    # ==========================================
    # 1. PLANNING STAGE (High Reasoning Models)
    # ==========================================
    if not plan_mode and os.path.exists(plan_path):
        print(f"Loading existing chunk plan from: {plan_path}")
        with open(plan_path, encoding="utf-8") as f:
            plan_data = json.load(f)
    else:
        print("Generating chunk plan...")
        if 'outline' not in locals():
            if os.path.exists(outline_path):
                with open(outline_path, encoding="utf-8") as f:
                    outline = [line.rstrip('\r\n') for line in f]
            else:
                outline = parse_collapsed_numbered_outline(html_content)
                with open(outline_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(outline))
        
        outline_text = "\n".join(outline)

        planner_system_prompt = CONFIG["planner_system_prompt"].replace(
            "{planner_custom_instructions}", CONFIG.get("planner_custom_instructions", "")
        )

        completed_chunks = []
        raw_path = plan_path.replace(".json", "_raw.txt")

        # Clear raw file on first attempt
        if os.path.exists(raw_path):
            os.remove(raw_path)

        for attempt in range(MAX_PLANNER_CONTINUATIONS + 1):
            label = "initial" if attempt == 0 else f"continuation {attempt}"
            print(f"Calling Planner LLM ({label})...")

            if attempt == 0:
                current_prompt = (
                    "Here is the collapsed line-numbered HTML document outline. Analyze it and generate the complete chunk plan from the very start of the book text to the very end.\n\n"
                    f"COLLAPSED OUTLINE:\n{outline_text}"
                )
            else:
                if not completed_chunks:
                    print("WARNING: No completed chunks found so far. Retrying with initial prompt...")
                    current_prompt = (
                        "Here is the collapsed line-numbered HTML document outline. Analyze it and generate the complete chunk plan from the very start of the book text to the very end.\n\n"
                        f"COLLAPSED OUTLINE:\n{outline_text}"
                    )
                else:
                    context_chunks = completed_chunks[-5:]
                    last_chunk = completed_chunks[-1]
                    out_range = last_chunk.get("output_range", [1, 1])
                    last_line = int(out_range[1])
                    next_chunk_number = len(completed_chunks) + 1
                    
                    current_prompt = (
                        "You are a document planner. Your task is to split a HTML into logical chunks of roughly 500-2000 words each.\n"
                        f"You have already planned the first {len(completed_chunks)} chunks of the book.\n"
                        f"Here are the last few chunks for context:\n"
                        f"{json.dumps(context_chunks, indent=2)}\n\n"
                        f"The last planned chunk ends at HTML line {last_line}.\n\n"
                        "Please generate the remaining chunk plan starting from chunk {next_chunk_number} for the rest of the book.\n"
                        f"Make sure to continue planning from line {last_line} to the end of the outline.\n"
                        "Start your response with the next chunk and continue until the end of the book.\n"
                        "Your output must be a single JSON object containing the remaining chunks, followed by a \"complete\" boolean as the VERY LAST field (set to true if you have completed the plan for the rest of the book outline, or false if you need to pause/stop before the end):\n"
                        "{\n"
                        '  "chunks": [\n'
                        "    ...\n"
                        "  ],\n"
                        '  "complete": true\n'
                        "}\n\n"
                        f"COLLAPSED OUTLINE:\n"
                        f"{outline_text}"
                    )

            # Pass is_planning=True to hook up thinking_config values inside payloads
            response, used_model, used_provider = call_llm_auto(
                CONFIG["planner_models"],
                current_prompt,
                planner_system_prompt,
                is_planning=True,
                continuous_retry=False,
            )
            print(f"  -> Got response via {used_model} ({used_provider}), {len(response):,} chars")

            with open(raw_path, "a", encoding="utf-8") as f:
                f.write(f"\n--- RESPONSE {label} ({used_model}) ---\n")
                f.write(response)

            response_clean = response.strip()

            new_chunks = extract_complete_chunks(response_clean)
            print(f"  -> Extracted {len(new_chunks)} complete chunks from the response.")

            # Filter out duplicates and add to completed_chunks
            existing_numbers = {c.get("chunk_number") for c in completed_chunks if c.get("chunk_number") is not None}
            added_any = False
            for chunk in new_chunks:
                c_num = chunk.get("chunk_number")
                if c_num not in existing_numbers:
                    completed_chunks.append(chunk)
                    existing_numbers.add(c_num)
                    added_any = True

            # Check if this response parsed cleanly as a complete JSON object with "complete": true
            is_complete = False
            try:
                data = _clean_and_parse_json(response_clean)
                if isinstance(data, dict) and data.get("complete") is True:
                    is_complete = True
            except Exception:
                pass

            # If it's a complete JSON with "complete": true, and we have chunks, we can stop
            if is_complete and len(completed_chunks) > 0:
                print(f"Plan JSON generated successfully after {attempt} call(s).")
                break

            # If we couldn't parse any new chunks, let's output warnings
            if not added_any:
                print("WARNING: No new complete chunks could be parsed from this response. We might be stuck.")
                if attempt == MAX_PLANNER_CONTINUATIONS:
                    print(f"ERROR: Reached max calls ({MAX_PLANNER_CONTINUATIONS}) and JSON still incomplete.")
                    # Fallback: if we have some chunks, break to save partial plan
                    if len(completed_chunks) > 0:
                        break
                    raise Exception(f"Planner failed to produce any valid chunks after {MAX_PLANNER_CONTINUATIONS} calls.")

            print("  -> Plan is incomplete. Requesting continuation plan for the remaining outline...")

        else:
            # Loop finished without breaking
            if not completed_chunks:
                raise Exception(f"Planner failed to produce complete JSON after {MAX_PLANNER_CONTINUATIONS} continuations.")

        # Validate that chunk output_ranges don't overlap before saving to disk
        validate_no_overlaps(completed_chunks)

        # Save the finalized chunks
        plan_data = {"chunks": completed_chunks}
        with open(plan_path, "w", encoding="utf-8") as f:
            json.dump(plan_data, f, indent=2)
        print(f"Saved chunk plan to: {plan_path}")

    preview_path = plan_path.replace(".json", "_preview.txt")
    preview_text = build_plan_preview(plan_data, raw_lines)
    with open(preview_path, "w", encoding="utf-8") as f:
        f.write(preview_text)
    print(f"Plan preview saved to: {preview_path}")

    if plan_mode == "print":
        print(preview_text)
        return

    # ==========================================
    # 2. TRANSCRIBING STAGE (Standard Models)
    # ==========================================
    chunks = plan_data.get("chunks", [])
    print(f"Processing {len(chunks)} chunks defined in the plan (using standard transcriber models).")

    for idx, c in enumerate(chunks):
        chunk_number = c.get("chunk_number", idx + 1)
        chunk_path = os.path.join(book_dir, f"chunk_{chunk_number:04d}.md")
        if os.path.exists(chunk_path):
            print(f"Chunk {chunk_number}/{len(chunks)} already exists. Skipping.")
            continue

        print(f"\n--- Transcribing Chunk {chunk_number}/{len(chunks)}: '{c['title']}' ---")

        out_start, out_end, out_slice_str, extra_slices = get_html_slices_for_chunk(c, raw_lines)

        prompt_parts = [
            "Convert the following primary HTML book segment into clean Markdown.\n",
            "### PRIMARY TEXT TO TRANSCRIBE",
            "```html",
            out_slice_str,
            "```\n",
        ]

        if extra_slices:
            extra_context_parts = [s[2] for s in extra_slices]
            extra_context_html = "\n\n\n".join(extra_context_parts)
            prompt_parts.extend(
                [
                    "### ADDITIONAL CONTEXT",
                    "(Do NOT transcribe this text directly. Use it ONLY to resolve footnotes/variants referenced in the primary text)",
                    "```html",
                    extra_context_html,
                    "```\n",
                    "Resolve any footnotes/variants referenced inside the primary text by extracting ",
                    "their definitions from the ADDITIONAL CONTEXT and appending them at the end of your output.",
                ]
            )

        prompt_parts.append("Output clean Markdown only.")
        prompt = "\n".join(prompt_parts)

        print(f"Calling Transcriber LLM (prompt size: {len(prompt):,} chars)...")

        # is_planning defaults to False here so transcription bypasses thinking token budgets
        transcribed, used_model, used_provider = call_llm_auto(
            CONFIG["transcriber_models"],
            prompt,
            CONFIG["transcribe_system_prompt"],
            is_planning=False,
            continuous_retry=True,
        )
        print(f"Transcribed chunk {chunk_number} using {used_model} ({used_provider})")

        processed = postprocess_transcription(transcribed)
        processed = re.sub(r"\[\^([^\]]+)\]", rf"[^c{chunk_number}_\1]", processed)

        with open(chunk_path, "w", encoding="utf-8") as f:
            f.write(processed)
        print(f"Saved chunk: {chunk_path}")

        if delay > 0:
            print(f"Waiting {delay}s...")
            time.sleep(delay)

    # ==========================================
    # 3. MERGE FINAL BOOK
    # ==========================================
    if no_merge:
        print("\n[merge] Skipped (--no-merge).")
        return

    html_title = extract_html_title(html_content)
    if html_title:
        merged_name = f"{html_title}.md"
    else:
        merged_name = f"{url_slug}_clean.md"

    print(f"\nMerging all chunks for {url_slug} into final book...")
    if merge_filename:
        merged_path = merge_filename if os.path.isabs(merge_filename) else os.path.join(output_dir, merge_filename)
    else:
        html_title = extract_html_title(html_content)
        if html_title:
            merged_name = f"{html_title}.md"
        else:
            merged_name = f"{url_slug}_clean.md"
        merged_path = os.path.join(output_dir, merged_name)
    chunk_files = sorted(
        [f for f in os.listdir(book_dir) if re.match(r"chunk_\d+\.md$", f)],
        key=lambda f: int(re.search(r"chunk_(\d+)\.md", f).group(1)),
    )
    with open(merged_path, "w", encoding="utf-8") as outfile:
        for cf in chunk_files:
            with open(os.path.join(book_dir, cf), encoding="utf-8") as infile:
                outfile.write(infile.read())
                outfile.write("\n\n")
    print(f"Merged output saved to: {merged_path}")


def main():
    parser = argparse.ArgumentParser(description="Compile HTML books of poetry into clean Markdown via LLM.")
    parser.add_argument("--urls", nargs="+", help="HTML URLs or local file paths to compile", required=True)
    parser.add_argument("--output-dir", default="output_html", help="Directory to save clean Markdown chunks/books")
    parser.add_argument(
        "--delay",
        type=float,
        default=DELAY_BETWEEN_CHUNKS,
        help="Delay (in seconds) after a chunk is resolved (default: %(default)s)",
    )
    parser.add_argument(
        "--plan",
        nargs="?",
        const="regen",
        choices=["regen", "print", "outline"],
        default=None,
        dest="plan_mode",
        help="Regenerate plan ('regen'), print preview ('print'), or stop after outline ('outline').",
    )
    parser.add_argument("--no-merge", action="store_true", help="Skip merging chunks into the final .md file")
    parser.add_argument(
        "--merge",
        metavar="FILENAME",
        default=None,
        help="Override the auto-generated merged output filename (e.g. --merge=output.md)",
    )

    args = parser.parse_args()
    os.makedirs(args.output_dir, exist_ok=True)

    urls = args.urls
    if len(urls) == 1 and os.path.exists(urls[0]):
        filepath = urls[0]
        print(f"Reading URLs from file: {filepath}")
        with open(filepath, encoding="utf-8") as f:
            urls = [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]
        print(f"Found {len(urls)} URLs in file.")

    for url in urls:
        try:
            process_book(
                url,
                args.output_dir,
                delay=args.delay,
                plan_mode=args.plan_mode,
                no_merge=args.no_merge,
                merge_filename=args.merge,
            )
        except Exception as e:
            print(f"Failed to process {url}: {e}")

    print("\nAll done!")


if __name__ == "__main__":
    main()
