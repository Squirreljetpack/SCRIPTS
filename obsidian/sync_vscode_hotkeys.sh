#!/bin/zsh

OS=$(uname -s)
BASEDIR="$HOME"
cd $BASEDIR || exit 1
FILE_DARWIN="Library/Application Support/Code/User/keybindings.json"
FILE_LINUX=".config/Code/User/keybindings.json##os.Linux,e.jsonc"
BACKUP_FILE="hotkeys.json.bak"

# Determine OURS and THEIRS based on the current OS
# if [[ "$OS" == "Darwin" ]]; then
#   OURS="$FILE_DARWIN"
#   THEIRS="$FILE_LINUX"
#   convert="darlin"
# elif [[ "$OS" == "Linux" ]]; then
  OURS="$FILE_LINUX"
  THEIRS="$FILE_DARWIN"
  convert="lindar"
# fi

: ${KEY_CTRL:="ctrl\+"}
: ${KEY_META:="meta\+"}
: ${KEY_MAC:="cmd\+"}

# Define the conversion functions
darlin() {
    sed -e "s/$KEY_CTRL/$KEY_META/g" -e "s/$KEY_MAC/$KEY_CTRL/g" "${@}"
}

lindar() {
    sed -e "s/$KEY_CTRL/$KEY_MAC/g" -e "s/$KEY_META/$KEY_CTRL/g" "${@}"
}

[[ -s "$THEIRS" ]] && cp -a "$THEIRS" "$BACKUP_FILE"
$convert "$OURS" > "$THEIRS".bak

# preserve modtime
# if cmp -s "$BACKUP_FILE" "$THEIRS"; then
#   mv -f "$BACKUP_FILE" "$THEIRS"
# else
#   rm -f "$BACKUP_FILE"
# fi



# NOTES
# PATCH_FILE="hotkeys.json.patch"
# CURRENT_FILE="hotkeys.json"
# Ideally 3 way merge will happen
# cleanup(){ 
# 	[[ -e "$PATCH_FILE" ]] && rm "$PATCH_FILE"
# }
# trap cleanup EXIT INT TERM

# Check if THEIRS has more lines than OURS
# A very unsound merge solution , trash
# just overwrite as a precommit hook

# LINES_OURS=$(wc -l < "$OURS")
# LINES_THEIRS=$(wc -l < "$THEIRS")

# if (( LINES_THEIRS > LINES_OURS )); then
# 	echo "$THEIRS is longer"
#   # Substitute Ctrl with Meta and Mod with Ctrl in FILE_DARWIN
#   $convert_from "$THEIRS" > "$BACKUP_FILE"

#   # Create a patch file from the differences
#   diff -u "$OURS" "$BACKUP_FILE" > "$PATCH_FILE"
#   if [[ -s "$PATCH_FILE" ]]; then
#     # Apply the patch to OURS
#     $convert 
#     patch "$OURS" "$PATCH_FILE"
#     [ -t 0 ] && bat "$PATCH_FILE"
#   fi
# fi
# Check if the patch file has any content
