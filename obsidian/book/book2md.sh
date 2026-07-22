#!/bin/zsh
set -euxo pipefail

# Can be used any folder containing the book as name in Downloads/Books

# FUNCIONS
command -v "epub2md" > /dev/null 2>&1 || exit 1

# -e, not -f, tho pandoc does require specific type of epub
find_file() {
  DEFAULT_DIR="$HOME/Downloads"
  local file_path

  if [[ -e "./$1" ]]; then
    file_path=$(realpath "./$1")
  elif [[ -e "$DEFAULT_DIR/$1" ]]; then
    file_path=$(realpath "$DEFAULT_DIR/$1")
  else
    echo "File '$1' not found in current directory or $DEFAULT_DIR"
    return 1
  fi
  echo "$file_path"
}

# BEGIN
name=$(path-normalize ${${1:t}%%.epub})
author=$2 # destination folder in Books
[[ -n $author ]] || {
  printf author/folder:
  read -r author
}

local filepath="$(find_file ${1%%.epub}.epub)"
echo $filepath


# set working dir
if [ -d ~/Documents/Obsidian/XB/$author ]; then
  VAULT=XB
else
  VAULT=Books
  mkdir -p ~/Documents/Obsidian/$VAULT/$author
fi
cd ~/Documents/Obsidian/$VAULT/$author

# Join epub2md output
mkdir -p $name
cp -r $filepath $name/$name.epub
epub2md $name/$name.epub
ls $name/$name | sed "s|^|$name/$name/|" | sort -V | while read -r f; do [ -f "$f" ] && { echo $f; cat $f >> $name.md;} >> $name.md ; done

# rename images
sed -i '' 's|(./images|(./.'${name}'.assets|g' "${name}.md"
mv $name/$name/images ".${name}.assets" || :

# check book
ob-open $name.md || :
printf "Awaiting confirmation for convertbook"
read

# cleanup
${0:a:h}/.pixi/envs/default/bin/python ${0:a:h}/convert_book.py $name.md
rm -rf "$name"