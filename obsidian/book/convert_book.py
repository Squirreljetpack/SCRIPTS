import hashlib
import os
import re
import shutil
import string
import sys
import time

import frontmatter
import requests

# needs a parser
# some solutions like https://stackoverflow.com/questions/47162098/is-it-possible-to-match-nested-brackets-with-a-regex-without-using-recursion-or but very iffy
# but pumping lemma makes it tricky (why are those not involved?)

# globals
filename = ""
accepted_formats = ["gut", "epub", "rtf", "txt"]
format_type = ""
is_forced = False


def set_type(t, force=False):
    global format_type
    global accepted_formats
    global is_forced
    if is_forced and not force:
        return False
    if t in accepted_formats:
        format_type = t
        is_forced = True
        print(f"set type: {t}")
        return True
    raise Exception("Invalid format_type")


def check_type(t_list):
    global format_type
    global accepted_formats
    if not isinstance(t_list, str):
        raise TypeError(f"Expected a string, but got {type(t_list).__name__}")
    return format_type == t_list


# Download imgs
def md5_3char(file_path):
    hasher = hashlib.md5()
    with open(file_path, "rb") as f:
        buf = f.read()
        hasher.update(buf)
    return hasher.hexdigest()[:4]


def get_with_retry(url, retries=3, delay=2):
    attempt = 0
    while attempt < retries:
        try:
            response = requests.get(url, stream=True)
            if response.status_code == 200:
                return response
            print(
                f"Attempt {attempt + 1}: Status code {response.status_code}. Retrying in {delay} seconds..."
            )
        except requests.RequestException as e:
            print(
                f"Attempt {attempt + 1}: Request failed with exception: {e}. Retrying in {delay} seconds..."
            )

        attempt += 1
        time.sleep(delay)

    return response


# No spaces
def download_images(content):
    pattern = re.compile(r'<img src="(.*?)".*>|!\[[^\[\]\\]*?\]\((.*?)\)')
    downloaded = 0
    global DOWNLOAD_FOLDER
    df_exists = os.path.exists(DOWNLOAD_FOLDER)

    for i, match in enumerate(pattern.finditer(content)):
        if not df_exists:
            os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
        print(match, match[1])
        url = match.group(1).strip() if match.group(1) else match.group(2).strip()
        if url.startswith("http"):
            try:
                response = get_with_retry(url)
                if response.status_code == 200:
                    # Save temporary file
                    temp_file_path = os.path.join(
                        DOWNLOAD_FOLDER, f"temporarycbfile_{i}"
                    )
                    with open(temp_file_path, "wb") as f:
                        f.writelines(response.iter_content(1024))

                    # Generate unique filename based on hash
                    file_ext = os.path.splitext(url)[1]
                    file_hash = md5_3char(temp_file_path)
                    new_filename = f"image_{file_hash}{file_ext}".strip()
                    new_file_path = os.path.join(DOWNLOAD_FOLDER, new_filename)

                    # Move file to new location with new name
                    os.rename(temp_file_path, new_file_path)

                    # Update content with new filename
                    content = re.sub(re.escape(url), new_file_path, content)

                    downloaded += 1
                else:
                    print(f"Could not download {url}")
            except Exception as e:
                print(f"Could not download {url}. Error: {e}")

    print("Downloaded:", downloaded)
    return content


def convert_markdown_to_html_images(content):
    # Regex pattern to match markdown images and capture the alt text and the image path
    # single nest
    pattern = r"!\[(.*?)\](?=\()(?:\(((?:[^()]*|\([^()]*\))*)\))"

    # Function to replace the markdown with HTML image tag
    def replacement(match):
        alt_text = match.group(1)
        image_path = match.group(2)
        print(match.group())
        print(match.groups())

        # Obsidian has relative paths, do we need more processing?
        # Apparently markdown can support spaces in src for captions
        return f'<img src="{image_path.split(" ")[0]}" alt="{alt_text}" width="300px">'

    # Replace markdown image syntax with HTML <img> tags
    updated_content = re.sub(pattern, replacement, content)

    return updated_content


def replace_bold_headings(content, heading: int = 1):
    return re.sub(
        r"^\s*\*\*(.*)\*\*", r"#" * heading + r" \1", content, flags=re.MULTILINE
    )


def replace_spaces(content):
    match_count = [0]

    def replace_match(match):
        match_count[0] += 1
        return "\xa0" * len(match.group(0))

    updated_content = re.sub(r"^ +", replace_match, content, flags=re.MULTILINE)

    print(f"Number of spaces replaced: {match_count[0]}")

    return updated_content


def trim_whitespace(content):
    lines = content.splitlines()
    trimmed_lines = []
    empty_line_count = 0

    for line in lines:
        if line.strip() == "":
            empty_line_count += 1
            # Add the empty line only if we have less than 3 consecutive empty lines
            if empty_line_count <= 3:
                trimmed_lines.append(line.strip())
        else:
            empty_line_count = 0
            trimmed_lines.append(line.rstrip())

    trimmed = "\n".join(trimmed_lines)
    return trimmed


# \[\[(\d+)\]\]\(.*?#FN.*?\)\s* -> [^$1]: , \[\[(\d+)\]\]\(.*?#Foot.*?\) -> [^$1]
# relies on common info in naming of refs, since the id of the elements are lost
def replace_footnotes(content, general_footnote=False):
    endnote_pattern = re.compile(r"\[(\d+)\]\(#.*?(b\d+)\.html#.*?endnote\1\) *")
    chapnote_pattern = re.compile(r"\[(\d+)\]\(#notes.*?(b\d+)\.html#.*?note\1\)")
    footnote_pattern = re.compile(r"\[(\d+)\]\(#(.*?).html_F\1\) *")
    ref_pattern = re.compile(r"\[(\d+)\]\(#(.*?).html_FT\1\)")

    # Need multiline for ^ to match string start
    gut_footnote_pattern = re.compile(
        r"^\[\[([\dA-Z]+?)\]\]\(.*?#.*?(?:ref|anchor|citation).*?\1.*?\)",
        flags=re.IGNORECASE | re.MULTILINE,
    )
    gut_ref_pattern = re.compile(
        r"\[\[([\dA-Z]+?)\]\]\(.*?#.*?(?:fn|footnote).*?\1.*?\)", flags=re.IGNORECASE
    )

    # replacing footnotes: add a space after
    # \[(\d+)\]\(.*?#r\d+?\).\s*

    general_ref_pattern = re.compile(r"\[?\[(\d+)\]\]?\([^\s]*?\)")

    content, c1 = endnote_pattern.subn(lambda m: f"[^{m[2]}_note{m[1]}]: ", content)

    content, c2 = chapnote_pattern.subn(lambda m: f"[^{m[2]}_note{m[1]}]", content)

    content, c3 = footnote_pattern.subn(
        lambda m: f"[^{m[2]}_footnote{m[1]}]: ", content
    )

    content, c4 = ref_pattern.subn(lambda m: f"[^{m[2]}_footnote{m[1]}]", content)

    print(f"Replaced: {c1} endnotes, {c2} chapnotes, {c3} footnotes, {c4} refs")

    if check_type("gut"):
        content, c5 = gut_footnote_pattern.subn(lambda m: f"[^{m[1]}]: ", content)
        content, c6 = gut_ref_pattern.subn(lambda m: f"[^{m[1]}]", content)

        print(f"Replaced (gutenburg format): {c5} footnotes, {c6} refs")

    if general_footnote:
        content, c7 = general_ref_pattern.subn(lambda m: f"[^{m[1]}]", content)
        print("General refs replaced: ", c7)

    return content


from itertools import groupby


def remove_nums(content):
    # 1. Find all numbers in order of appearance
    # Using (\d+) to capture only the digits
    matches = re.findall(r"\[(\d+)\]\s?", content)

    if not matches:
        return content

    # Convert captured strings to integers
    nums = [int(n) for n in matches]

    # 2. Group consecutive numbers without sorting
    ranges = []
    # We use a custom counter to check for mathematical continuity in the sequence
    for k, g in groupby(enumerate(nums), lambda x: x[1] - x[0]):
        group = [val for idx, val in g]
        if len(group) > 1:
            ranges.append(f"{group[0]}-{group[-1]}")
        else:
            ranges.append(str(group[0]))

    range_str = ", ".join(ranges)
    print(f"Found pages in order: {range_str}")

    # 3. Prompt and Remove
    choice = input("Do you want to remove them? (yes/no): ").strip().lower()

    if choice[0] == "y":
        # Replace the full pattern [digits] plus the trailing space
        new_content, count = re.subn(r"\[\d+\]\s?", "", content)
        print(f"Removed {count} numbers.")
        return new_content

    print("No changes made.")
    return content


# experimental
def remove_pagenums(content, roman_pages=True):
    global format_type
    # if check_type("gut"):
    #     # optional remove pg indicator?
    #     pattern2=re.compile(r"\[(?:pg).? ?\d+\]",re.IGNORECASE)
    #     content, count=pattern2.subn("",content)
    #     print(f"{count} [pg #] removed")

    if roman_pages:
        pattern3 = re.compile(r"\[[LXVIC]+\]", re.IGNORECASE)
        content, count = pattern3.subn("", content)
        print(f"{count} roman page numbers removed")

    page_pattern = re.compile(r"\[PG ?[LXVIC\d]+\]", re.IGNORECASE)
    content, count = page_pattern.subn("", content)
    print(f"{count} [PG _] removed")
    return content


def add_cover_image_to_markdown(
    content, metadata, clobber=False, remove_from_post=True
):
    # Check the possible cover image paths
    global DOWNLOAD_FOLDER
    image_extensions = ["jpg", "png", "jpeg"]
    possible_image_paths = [
        f"{DOWNLOAD_FOLDER}images/cover.{ext}" for ext in image_extensions
    ] + [f"{DOWNLOAD_FOLDER}cover.{ext}" for ext in image_extensions]

    cover_image_path = None
    for path in possible_image_paths:
        if os.path.exists(path):
            cover_image_path = path
            break

    if cover_image_path:
        print(cover_image_path)
        # Check if the YAML front matter exists and add the cover-img field if it doesn't exist
        try:
            if metadata["cover-img"].strip() != "":
                print(f"cover-img field was: {metadata['cover-img']}")
                if not clobber:
                    return content, metadata
        except:
            pass
        metadata["cover-img"] = cover_image_path
        if remove_from_post:
            content, count = re.subn(
                rf"<img src=\"{re.escape(cover_image_path)}\".*?>", "", content
            )
            if count:
                print(f"Removed {count} cover-img's from post")
    return content, metadata


def epub_headings(content):
    allowed_chars = r"\.\\\_ \w\#\?\:\-\!\$\%\^\/,\~”\“’"
    pattern = re.compile(
        r"\[([^\[\]]+?)\]\([%s]+(content|toc)[%s]+\)" % (allowed_chars, allowed_chars),
        re.IGNORECASE,
    )
    content, count = pattern.subn(lambda x: f"# {x[1]}", content)
    print(f"epub headings subbed:{count}")
    return content


def prepend_hashes(content, extraheadings=False, title_chars=100):
    prefix = r"^\s*\*?\*?"
    suffix = r"\.?\*?\*?$"
    allowed_chars = f"\\w {re.escape(string.punctuation.replace('*', '').replace('#', '').replace('_', ''))}]"

    mids = {
        "roman_numerals": r"M{0,4}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})",
        "part": r"Part\s(One|Two|Three|Four|Five|Six|Seven|Eight|Nine|Ten|Eleven|Twelve|Thirteen|Fourteen|Fifteen|Sixteen|Seventeen|Eighteen|Nineteen|Twenty)",
        # "numbers": r"[0-9]+.?",
    }
    shortline = "(?!.*---)[%s]{1,%s}" % (allowed_chars, title_chars)

    patterns = {
        k: re.compile(prefix + v + suffix, re.IGNORECASE) for k, v in mids.items()
    }
    extrapattern = re.compile(prefix + shortline + suffix, re.IGNORECASE)
    changed = []
    lines = content.splitlines()

    # even without extraheadings, this is pretty eager
    for i in range(1, len(lines) - 1):
        line = lines[i]
        if line:
            for k, pattern in patterns.items():
                if pattern.match(line):
                    if k == "part":
                        lines[i] = "## " + line
                        changed.append((line, i))
                        break
                    if k == "roman_numerals":
                        lines[i] = "#### " + line
                        changed.append((line, i))
                        break
                    else:  # noqa: RET508
                        lines[i] = "### " + line
                        changed.append((line, i))
                        break

    if extraheadings:
        print("Searching: Extra headings")
        for i in range(1, len(lines) - 10):
            line = lines[i]
            if line and (
                extrapattern.match(line)
                and (lines[i - 1].strip() == "" and lines[i + 1].strip() == "")
            ):
                lines[i] = "## " + line
                changed.append((line, i))

    print("Hashes prepended:")
    print(changed)
    return "\n".join(lines)


def replace_tags(content):
    content, count = re.subn(r"\<\?xml.*?\>", "", content)
    content = re.sub(r".*/.*\.md\n", "", content)
    print(f"{count} tags replaced")
    return content


def escapes(content):
    # Sample text

    # Regular expression to find [[\w\s]+] not followed by (
    # [] is not matched as its not standard special md syntax.
    pattern = re.compile(r"\[\[[\w\s?]+?\]\](?!\()")

    def escape_brackets(match):
        return match.group(0).replace("[", r"\[").replace("]", r"\]")

    last = 1
    brackets = 0
    while last > 0:
        content, last = pattern.subn(escape_brackets, content)
        brackets += last
    underscores = 0

    def process_line(line):
        nonlocal underscores
        # Find all unescaped underscores
        unescaped_underscores = [m.start() for m in re.finditer(r"(?<!\\)_", line)]

        if len(unescaped_underscores) % 2 == 1 and not ("<" in line and ">" in line):
            last_unescaped_index = unescaped_underscores[-1]
            line = (
                line[:last_unescaped_index] + r"\_" + line[last_unescaped_index + 1 :]
            )
            underscores += 1
            print(line)

        return line

    lines = content.split("\n")
    processed_lines = [process_line(line) for line in lines]
    print("brackets:", brackets)
    print("underscores:", underscores)
    return "\n".join(processed_lines)


def remove_broken_heading_links(content):
    pattern = r"^(#+)\s*\[(.*?)\**\]\(.*\)$"
    content, c1 = re.subn(pattern, r"\1 \2", content, flags=re.MULTILINE)
    pattern = r"^(#+)\s*\**(.*?)\**$"
    content, c2 = re.subn(pattern, r"\1 \2", content, flags=re.MULTILINE)
    print(c1, c2, "heading links removed")
    return content


def sanitize_filename(filename):
    sanitized = re.sub(r"[()\/\\]", "", filename)  # Remove special characters
    if filename != sanitized:
        print(f"Special characters removed from filename: {filename}")
    return sanitized.replace(" ", "_")


def strip_gutenberg_lines(content, incheadings=True, striptoc=True, toc_search_dist=20):
    # Define the patterns to identify the start and end markers
    start_marker = re.compile(r"\*\*\* START.*PROJECT GUTENBERG.*")
    end_marker = re.compile(r"\*\*\* END.*PROJECT GUTENBERG.*")

    start_match = start_marker.search(content)
    if start_match:
        content = content[start_match.end() :]
    heading_pattern = re.compile(r"^(\#+)\s+(.*)", re.MULTILINE)
    headings = list(heading_pattern.finditer(content))
    if len(headings) == 0:
        set_type("txt")
        return content

    if incheadings:  # note that this introduces nonrepeatability
        min_hashes = min(len(match.group(1)) for match in headings)
        if min_hashes > 1:
            print("Increasing heading size")
            remove_pattern = re.compile(
                r"^" + "#" * (min_hashes - 1) + r"\s*", re.MULTILINE
            )
            content = re.sub(remove_pattern, "", content)

    if striptoc:
        content_heading_index = -1
        for i in range(min(toc_search_dist, len(headings) - 1)):
            heading_text = headings[i][2]
            if "content" in heading_text.lower():
                content_heading_index = i
                break

        if content_heading_index != -1:
            print(
                f"Contents removed. Next heading is:{headings[content_heading_index + 1]}"
            )
            content = content[headings[content_heading_index + 1].start() :]

    end_match = end_marker.search(content)
    if not end_match:
        return content
    set_type("gut")

    end_pos = end_match.start()
    # Grab the text before the match
    preceding_text = content[:end_pos].rstrip()

    # Get the last line
    last_newline = preceding_text.rfind("\n")
    last_line = preceding_text[last_newline + 1 :]

    # 4. If "Project Gutenberg" is in that line, adjust the end_pos
    if "Project Gutenberg" in last_line:
        end_pos = last_newline

    return content[:end_pos].rstrip()


def remove_all_contents():
    return


def main(
    file_path,
    extraheadings=False,
    removepagenums=False,
    general_footnote=False,
    roman_pages=False,
    dl_images=False,
):
    with open(file_path, encoding="utf-8") as f:
        post = frontmatter.loads(f.read())
        content = post.content

    # Fix filenames
    global file_name
    global DOWNLOAD_FOLDER
    file_dir = os.path.dirname(file_path)
    # root, ext=os.path.splitext(file_path)
    global filename
    file_name = sanitize_filename(os.path.basename(file_path))
    new_file_path = os.path.join(file_dir, file_name)
    print(new_file_path)
    shutil.move(file_path, new_file_path)
    file_path = new_file_path
    DOWNLOAD_FOLDER = f".{os.path.splitext(file_name)[0]}.assets/"

    content = strip_gutenberg_lines(content)
    if check_type("gut"):
        pass
    if check_type("txt"):
        content = replace_bold_headings(content)
    if check_type("epub"):
        pass
    if "PROJECT GUTENBERG" in content:
        print("warn: remnant Gutenburg marginalia found")

    content = trim_whitespace(content)
    # content=convert_markdown_to_html_images(content)
    content = replace_spaces(content)
    content = replace_tags(content)

    content = replace_footnotes(content, general_footnote=general_footnote)
    content = remove_broken_heading_links(content)
    content = prepend_hashes(content, extraheadings=extraheadings)
    content = epub_headings(content)

    # content=escapes(content)
    # content = remove_pagenums(content, roman_pages=roman_pages)

    # interactive
    content = remove_nums(content)

    if dl_images:
        content = download_images(content)
    content, metadata = add_cover_image_to_markdown(content, post.metadata)
    post.metadata = metadata
    post.content = content

    # # second pass
    content = strip_gutenberg_lines(content)

    with open(file_path, "w", encoding="utf-8") as file:
        file.write(frontmatter.dumps(post))


# remove broken links
# Automatically remove TOC/Up to first chapter/License
# gutenberg/vs epub/vs txt specialization

if __name__ == "__main__":
    kwarg_dict = {}
    a = sys.argv
    if len(a) < 2:
        print("Usage: python script_name.py <file_path>")
        sys.exit(1)
    for i in range(len(a)):
        if a[i] == "--eh":
            kwarg_dict["extraheadings"] = True
        if a[i] == "-rp":
            kwarg_dict["removepagenums"] = True
        if a[i] == "-t" and i < len(a) - 1:
            set_type(a[i + 1], force=True)
        if a[i] == "-gf":
            kwarg_dict["general_footnote"] = True
        if a[i] == "-rrp":
            kwarg_dict["roman_pages"] = True
            kwarg_dict["removepagenums"] = True
        if a[i] == "-d":
            kwarg_dict["dl_images"] = True
        if a[i] in accepted_formats:
            set_type(a[i], force=True)
            check_type(None)
    main(a[1], **kwarg_dict)


# txt:
# ^\*\*(.*)\*\*
# ### $1


# _“_(.+?)_”_
# _“$1”_

# _(\d+)
# \_(\d+)


# look for harper collins as they are well formatted


# def strip_until_matching_bracket(s):
#             i = 0
#             bracket_count = 0
#             # Iterate through the string
#             while i < len(s):
#                 # Count opening brackets
#                 if s[i] == '[':
#                     bracket_count += 1
#                 # Count closing brackets
#                 elif s[i] == ']':
#                     bracket_count -= 1
#                     # If all opening brackets are matched
#                     if bracket_count == 0:
#                         break
#                 i += 1
#             # Skip the next character if it is '('
#             if i < len(s) - 1 and s[i + 1] != '(':
#                 print(f"WARNING: unexpected {s[i + 1]}")
#             return s[i + 2:-1]

# # https://stackoverflow.com/questions/5454322/python-how-to-match-nested-parentheses-with-regex
# # https://stackoverflow.com/questions/47162098/is-it-possible-to-match-nested-brackets-with-a-regex-without-using-recursion-or
# def convert_markdown_to_html_images(content):
#     # Regex pattern to match markdown images and capture the alt text and the image path
#     pattern = r'!\[(.*?)\](?=\()(?:(?=.*?\((?!.*?\2)(.*\)(?!.*\3).*))(?=.*?\)(?!.*?\3)(.*)).)+?.*?(?=\2)[^(]*(?=\3$)'
