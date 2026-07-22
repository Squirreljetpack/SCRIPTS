import argparse
from html.parser import HTMLParser
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

# Maximum allowed lines a macro can span before the parser abandons the match
MAX_MACRO_LINES = 3


def extract_macro(text: str, start_idx: int, open_str: str, close_str: str):
    """
    Scans for matching closing delimiter across multiple lines up to MAX_MACRO_LINES.
    Handles string literals and bracket nesting depth.
    """
    i = start_idx + len(open_str)
    n = len(text)
    depth = 1
    lines_spanned = 1
    in_quote = None
    is_multi = len(close_str) > 1

    while i < n:
        char = text[i]

        # Track line breaks and enforce MAX_MACRO_LINES boundary
        if char == '\n':
            lines_spanned += 1
            if lines_spanned > MAX_MACRO_LINES:
                return None

        # String literal protection: ignore delimiters inside quotes
        if char in ('"', "'") and (i == 0 or text[i - 1] != '\\'):
            if in_quote == char:
                in_quote = None
            elif in_quote is None:
                in_quote = char

        elif not in_quote:
            # Multi-character closing (e.g. SugarCube '>>')
            if is_multi:
                if text.startswith(close_str, i):
                    end_idx = i + len(close_str)
                    return text[start_idx:end_idx], end_idx, open_str

            # Single-character balanced closing ((), {}, [])
            else:
                if char == open_str:
                    depth += 1
                elif char == close_str:
                    depth -= 1
                    if depth == 0:
                        return text[start_idx:i + 1], i + 1, open_str

        i += 1

    return None


class TwineExtractor(HTMLParser):

    def __init__(self):
        super().__init__()
        self.output = []
        self.current_tag = None
        self.passage_name = None
        self.document_title = None
        self._temp_title = []
        self._current_passage_lines = []

    def handle_starttag(self, tag, attrs):
        tag_lower = tag.lower()
        if tag_lower == "title":
            self.current_tag = "title"
            self.output.append("\n# ")
        elif tag_lower == "tw-passagedata":
            self.current_tag = "tw-passagedata"
            attrs_dict = dict(attrs)
            self.passage_name = attrs_dict.get("name", "Untitled Passage")
            self.output.append(f"\n\n### {self.passage_name}\n\n")
            self._current_passage_lines = []

    def handle_endtag(self, tag):
        tag_lower = tag.lower()
        if tag_lower == "title":
            self.document_title = "".join(self._temp_title).strip()
            self.current_tag = None
        elif tag_lower == "tw-passagedata":
            raw_text = "".join(self._current_passage_lines)
            processed_text = self.post_process_passage(raw_text)
            self.output.append(processed_text)
            self._current_passage_lines = []
            self.current_tag = None

    def handle_data(self, data):
        if self.current_tag == "title":
            self._temp_title.append(data)
            self.output.append(data)
        elif self.current_tag == "tw-passagedata":
            self._current_passage_lines.append(data)

    @classmethod
    def process_macros_and_directives(cls, text: str) -> str:
        """Scans passage text and wraps Twine macros into Markdown code blocks."""
        tokens = []
        i = 0
        n = len(text)
        in_code_block = False

        while i < n:
            line_start = (i == 0 or text[i - 1] == '\n')

            # Check if we are entering or exiting a Markdown triple-backtick code block
            if line_start and text.startswith("```", i):
                in_code_block = not in_code_block

            # Skip directive processing entirely when inside an existing code block
            if in_code_block:
                tokens.append(text[i])
                i += 1
                continue

            # Measure leading spaces/tabs on current line
            indent_len = 0
            while i + indent_len < n and text[i + indent_len] in (' ', '\t'):
                indent_len += 1
            scan_pos = i + indent_len

            macro_match = None
            if scan_pos < n:
                # Detect Chapbook section modifiers like [css] or [javascript]
                if text.startswith("[css]", scan_pos) or text.startswith("[javascript]", scan_pos):
                    lang = "css" if text.startswith("[css]", scan_pos) else "javascript"
                    header_len = len(lang) + 2  # Length of [css] or [javascript]
                    
                    content_start = scan_pos + header_len
                    
                    # Section ends at the next bracket directive (e.g. [continued]) or end of passage text
                    match = re.search(r"(\r?\n[ \t]*\[)", text[content_start:])
                    if match:
                        content_end = content_start + match.start()
                    else:
                        content_end = n

                    code_content = text[content_start:content_end].strip()
                    macro_match = (code_content, content_end, f"section_{lang}")

                # Standard macro detection
                elif text.startswith("<<", scan_pos):
                    macro_match = extract_macro(text, scan_pos, open_str="<<", close_str=">>")
                elif text[scan_pos] == '(' and re.match(r"^\([a-zA-Z\-]+:", text[scan_pos:]):
                    macro_match = extract_macro(text, scan_pos, open_str="(", close_str=")")
                elif text[scan_pos] == '{':
                    macro_match = extract_macro(text, scan_pos, open_str="{", close_str="}")
                elif text[scan_pos] == '[':
                    macro_match = extract_macro(text, scan_pos, open_str="[", close_str="]")

            if macro_match:
                macro_str, end_pos, open_str = macro_match

                # Handle [css] / [javascript] sections formatting
                if open_str.startswith("section_"):
                    lang = open_str.split("_")[1]
                    
                    prev_line_end = i - 1 if i > 0 else -1
                    needs_leading_newline = False
                    if prev_line_end > 0:
                        prev_char_pos = prev_line_end - 1
                        while prev_char_pos >= 0 and text[prev_char_pos] in (' ', '\t', '\r'):
                            prev_char_pos -= 1
                        needs_leading_newline = (
                            prev_char_pos >= 0 and text[prev_char_pos] != '\n'
                        )

                    leading_spacing = "\n\n" if needs_leading_newline else "\n"
                    tokens.append(f"{leading_spacing}```{lang}\n{macro_str}\n```\n")
                    i = end_pos
                    continue

                # Check if this macro sits standalone on its line
                rem_pos = end_pos
                while rem_pos < n and text[rem_pos] in (' ', '\t', '\r'):
                    rem_pos += 1
                is_standalone = line_start and (rem_pos == n or text[rem_pos] == '\n')

                # Chapbook [...] never matches inline, only when standalone
                if open_str == '[' and not is_standalone:
                    tokens.append(text[i])
                    i += 1
                    continue

                if is_standalone:
                    # Check if preceding line contains non-empty text
                    prev_line_end = i - 1 if i > 0 else -1
                    needs_leading_newline = False
                    if prev_line_end > 0:
                        prev_char_pos = prev_line_end - 1
                        while prev_char_pos >= 0 and text[prev_char_pos] in (' ', '\t', '\r'):
                            prev_char_pos -= 1
                        needs_leading_newline = (
                            prev_char_pos >= 0 and text[prev_char_pos] != '\n'
                        )

                    leading_spacing = "\n\n" if needs_leading_newline else "\n"

                    # Check if trailing line contains non-empty text
                    after_newline_pos = rem_pos + (1 if rem_pos < n and text[rem_pos] == '\n' else 0)
                    needs_trailing_newline = (
                        after_newline_pos < n and text[after_newline_pos] not in ('\n', '\r')
                    )

                    trailing_spacing = "\n\n" if needs_trailing_newline else "\n"
                    tokens.append(f"{leading_spacing}> `{macro_str}`{trailing_spacing}")

                    # Advance past trailing newline
                    i = after_newline_pos
                    continue
                else:
                    # Preserve leading spaces/tabs swallowed by scan_pos before inline macros
                    if indent_len > 0:
                        tokens.append(text[i:scan_pos])

                    tokens.append(f"`{macro_str}`")
                    i = end_pos
                    continue

            # Default: normal character
            tokens.append(text[i])
            i += 1

        return "".join(tokens)

    def post_process_passage(self, text: str) -> str:
        """Applies formatting rules to passage content."""

        # 1. Convert initial metadata up to '--' into a code block + '---' divider
        frontmatter_pattern = r"^(.*?)(?:\r?\n|^)--(?:\r?\n|$)"

        def format_frontmatter(match):
            meta_content = match.group(1).strip()
            if meta_content:
                return f"```\n{meta_content}\n```\n\n---\n"
            return "---\n"

        text = re.sub(
            frontmatter_pattern, format_frontmatter, text, count=1, flags=re.DOTALL
        )

        # 2. Transform {embed passage: 'name'} -> > [name](#name%20with%20encoding)
        def format_embed(match):
            passage_title = match.group(1).strip()
            encoded_anchor = encode_anchor(passage_title)
            
            return f"> [{passage_title}](#{encoded_anchor})"

        embed_pattern = r"\{embed\s+passage:\s*['\"]([^'\"]+)['\"]\}"
        text = re.sub(embed_pattern, format_embed, text, flags=re.IGNORECASE)

        # 3. Transform Twine links [[...]] into Obsidian-compatible Markdown links
        def format_twine_link(match):
            raw_link = match.group(1).strip()

            if "|" in raw_link:
                label, target = raw_link.split("|", 1)
            elif "->" in raw_link:
                label, target = raw_link.split("->", 1)
            elif "<-" in raw_link:
                target, label = raw_link.split("<-", 1)
            else:
                label = target = raw_link

            label = label.strip()
            target = target.strip()
            
            encoded_target = encode_anchor(target)

            return f"[{label}](#{encoded_target})"

        twine_link_pattern = r"\[\[(.*?)\]\]"
        text = re.sub(twine_link_pattern, format_twine_link, text)

        # 4. Process Directives & Macros via single-pass scanner
        text = self.process_macros_and_directives(text)

        return text

    def get_result(self) -> tuple[str, str]:
        text = "".join(self.output)
        lines = [line.rstrip() for line in text.splitlines()]
        cleaned_text = "\n".join(lines)
        formatted_markdown = re.sub(r"\n{3,}", "\n\n", cleaned_text).strip()

        title = self.document_title or "output"
        safe_title = re.sub(r'[\\/*?:"<>|]', "", title).strip() or "output"

        return formatted_markdown, safe_title


def fetch_url_html(url: str) -> str:
    """Downloads HTML text from a given URL."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/115.0.0.0 Safari/537.36"
        )
    }
    parsed = urllib.parse.urlparse(url)
    quoted_path = urllib.parse.quote(parsed.path, safe="/")
    url = urllib.parse.urlunparse(parsed._replace(path=quoted_path))
    
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req) as response:
            return response.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"Error fetching URL '{url}': {e}", file=sys.stderr)
        sys.exit(1)

def encode_anchor(heading_text):
    kept_punctuation = "()?!.,;\"'*&%$"
    
    pattern = rf'[^\w\s\-{re.escape(kept_punctuation)}]'
    
    stripped = re.sub(pattern, '', heading_text)
    
    encoded = urllib.parse.quote(stripped, safe=kept_punctuation[2:])
    
    return encoded

def main():
    parser = argparse.ArgumentParser(
        description="Extract title and Twine passage contents from a local file or web URL into Markdown format."
    )

    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument(
        "file_path",
        type=Path,
        nargs="?",
        help="Path to the local Twine HTML file to process",
    )
    input_group.add_argument(
        "-u",
        "--url",
        nargs="?",
        const="PROMPT",
        metavar="URL",
        help="URL of the Twine HTML page. If omitted, prompts for URL input.",
    )

    parser.add_argument(
        "-o",
        "--output",
        nargs="?",
        const="AUTO",
        default=None,
        metavar="FILENAME",
        help="Save output to a file. If no filename is supplied, uses <Title>.md",
    )

    args = parser.parse_args()

    if args.url is not None:
        target_url = args.url
        if target_url == "PROMPT":
            try:
                target_url = input("Enter Twine HTML URL: ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nAborted.", file=sys.stderr)
                sys.exit(1)

        if not target_url.startswith(("http://", "https://")):
            print("Error: URL must start with http:// or https://", file=sys.stderr)
            sys.exit(1)

        content = fetch_url_html(target_url)
    else:
        if not args.file_path or not args.file_path.is_file():
            print(f"Error: Valid file path required.", file=sys.stderr)
            sys.exit(1)
        try:
            content = args.file_path.read_text(encoding="utf-8")
        except Exception as e:
            print(f"Error reading file '{args.file_path}': {e}", file=sys.stderr)
            sys.exit(1)

    extractor = TwineExtractor()
    extractor.feed(content)
    markdown_result, file_title = extractor.get_result()

    if args.output is not None:
        if args.output == "AUTO":
            output_file = Path.cwd() / f"{file_title}.md"
        else:
            output_file = Path(args.output)

        try:
            output_file.write_text(markdown_result, encoding="utf-8")
            print(f"Saved output to: {output_file}")
        except Exception as e:
            print(f"Error writing to output file '{output_file}': {e}", file=sys.stderr)
            sys.exit(1)
    else:
        print(markdown_result)


if __name__ == "__main__":
    main()