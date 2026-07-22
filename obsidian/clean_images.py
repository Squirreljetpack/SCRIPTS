import os
import re
import argparse
import urllib.parse
from pathlib import Path

def format_size(size_in_bytes):
    """Formats raw bytes into human-readable strings."""
    for unit in ['Bytes', 'KB', 'MB', 'GB', 'TB']:
        if size_in_bytes < 1024.0:
            return f"{size_in_bytes:.2f} {unit}"
        size_in_bytes /= 1024.0
    return f"{size_in_bytes:.2f} PB"

def find_unreferenced_images(vault_path):
    vault_dir = Path(vault_path).resolve()
    if not vault_dir.is_dir():
        print(f"Error: {vault_path} is not a valid directory.")
        return []
    
    # Regex patterns to capture image references
    wiki_re = re.compile(r'!\[\[([^\]|]+)(?:\|.*)?\]\]')
    md_re = re.compile(r'!\[.*?\]\(([^)]+)\)')
    img_re = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']')
    frontmatter_re = re.compile(r'^---\n(.*?)\n---', re.DOTALL)
    yaml_re = re.compile(r':\s*"?([^"\n]+\.(?:png|jpg|jpeg|gif|svg|webp|bmp|tiff))"?', re.IGNORECASE)

    image_exts = {'.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.bmp', '.tiff'}
    
    all_images = []
    all_md_files = []

    print(f"\nScanning vault: {vault_dir}...")

    # --- 1. Gather all files ---
    for root, dirs, files in os.walk(vault_dir, followlinks=False):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        
        root_path = Path(root)
        for file in files:
            file_path = root_path / file
            if file_path.is_symlink():
                continue
            
            if file_path.suffix.lower() in image_exts:
                all_images.append(file_path)
            elif file_path.suffix.lower() == '.md':
                all_md_files.append(file_path)

    # --- 2. Extract references ---
    references = set()
    for md_file in all_md_files:
        try:
            with open(md_file, 'r', encoding='utf-8') as f:
                content = f.read()
                
                for match in wiki_re.finditer(content):
                    references.add(match.group(1).strip())
                for match in md_re.finditer(content):
                    raw_ref = match.group(1).split('"')[0].split("'")[0].strip()
                    raw_ref = raw_ref.split('?')[0]
                    references.add(raw_ref)
                for match in img_re.finditer(content):
                    references.add(match.group(1).strip().split('?')[0])
                    
                fm_match = frontmatter_re.match(content)
                if fm_match:
                    for y_match in yaml_re.finditer(fm_match.group(1)):
                        references.add(y_match.group(1).strip())
        except Exception as e:
            print(f"  Warning: Could not read {md_file.name} - {e}")

    # --- 3. Clean and normalize references ---
    cleaned_refs = []
    for ref in references:
        ref_decoded = urllib.parse.unquote(ref)
        ref_path = Path(ref_decoded)
        parts = tuple(p.lower() for p in ref_path.parts if p not in ('.', '/', '\\'))
        if parts:
            cleaned_refs.append(parts)

    # --- 4. Tail-End Matching ---
    unreferenced = []
    for img_path in all_images:
        img_parts = tuple(p.lower() for p in img_path.parts)
        is_referenced = False
        
        for ref_parts in cleaned_refs:
            if len(ref_parts) <= len(img_parts):
                if img_parts[-len(ref_parts):] == ref_parts:
                    is_referenced = True
                    break
                    
        if not is_referenced:
            unreferenced.append(img_path)
            
    return unreferenced

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Find (and optionally delete) unreferenced images in Obsidian vaults.")
    parser.add_argument('vaults', metavar='VAULT_PATH', type=str, nargs='+',
                        help='One or more paths to Obsidian vaults to scan.')
    parser.add_argument('--delete', action='store_true',
                        help='Delete the unreferenced image files found during the scan.')
    
    args = parser.parse_args()
    total_orphans = 0
    total_bytes = 0
    
    for vault in args.vaults:
        if vault.startswith(('"', "'")) and vault.endswith(('"', "'")):
            vault = vault[1:-1]
            
        orphans = find_unreferenced_images(vault)
        
        if not orphans:
            print("  Success: No unreferenced images found.")
            continue
            
        print(f"  Found {len(orphans)} unreferenced images:")
        for img in orphans:
            # Safely grab the file size before any deletion attempt
            try:
                img_size = img.stat().st_size
            except Exception:
                img_size = 0

            if args.delete:
                try:
                    img.unlink()
                    print(f"  [DELETED] {img} ({format_size(img_size)})")
                    total_bytes += img_size
                except Exception as e:
                    print(f"  [ERROR] Could not delete {img} - {e}")
            else:
                # print(f"{img} ({format_size(img_size)})")
                print(img)
                total_bytes += img_size
                
        total_orphans += len(orphans)
        
    print("\n" + "="*40)
    readable_size = format_size(total_bytes)
    if args.delete:
        print(f"Scan complete. Successfully deleted {total_orphans} image(s) freeing up {readable_size}.")
    else:
        print(f"Scan complete. Found {total_orphans} unreferenced image(s) totaling {readable_size}.")
        print("Run with '--delete' to permanently remove them.")