#!/bin/zsh

# todo: improve exit codes

### CONSTANTS + SETUP
[[ $OBSIDIAN_HOME != /* ]] && echo "\$OBSIDIAN_HOME not set" >&2 && exit 1
cd $OBSIDIAN_HOME || exit 1

DATE="$(date +%d-%m-%y)"
ATTRIBUTES_FILE=".git/info/attributes" # unsure if this works (https://stackoverflow.com/questions/56951474/using-gitattributes-as-merge-strategy-in-submodules), so automatic checkout is also implemented.

SCRIPT="$(readlink -f -- "$0")"

PROG="$(basename "$0")"
SCRIPT_DIR="$(dirname -- "$SCRIPT")"
[[ -n $SCRIPT_DIR ]] || exit 1

case $OSTYPE in
  darwin*)
    logger() {
      local TAG= line
      while getopts "t:" opt; do
          case ${opt} in
            t)
              TAG="[$OPTARG] "
            ;;
          esac
      done
      shift $((OPTIND -1))

      while IFS= read -r line; do
        echo "$TAG$line" | command logger
      done
    }
  ;;
esac

exec 3>&1 4>&2
if [ ! -t 1 ]; then
  # stdout is not a tty. Log stdout/stderr to coproc in addition.
  coproc logger -t "${PROG}"
  coproc_pid=$!
  exec > >(tee /dev/fd/3 >&p 2>/dev/null)
  exec 2> >(tee /dev/fd/4 >&p 2>/dev/null)  
else
  [[ $DEBUG == true ]] && set -x
fi

### UTILS
check_network() {
  /sbin/ping -c 1 -q google.com &>/dev/null
}

info() {
  echo "[INFO] $1"
}

warn() {
  echo "[WARN] $1"
}

err() {
  echo "[ERROR] $1"
}

submodule_names() {
  cd $OBSIDIAN_HOME; git submodule status | awk '{print $2}'
}

### PRELIMS
# Sync hotkeys
# Shunt large files and folders
# Template files

HOTKEY_SYNC_SCRIPT=$SSdir/sync_hotkeys.sh
SHUNT_SCRIPT=$SSdir/syncthing/sym2hash.zsh
TEMPLATING_SCRIPT=$SSdir/obsidian/preambles

echo "\n#########$DATE#########\n"

{
  info "Syncing obsidian hotkeys"
  cd $OBSIDIAN_HOME/BASE/.obsidian
  $HOTKEY_SYNC_SCRIPT --darwin "hotkeys.json##os.Darwin,e.json" --linux "hotkeys.json##os.Linux,e.json" ||
  warn "$HOTKEY_SYNC_SCRIPT failed"
} always {
  cd $OBSIDIAN_HOME
}

for d in */; do DRY=false $SHUNT_SCRIPT $d 20; done

$TEMPLATING_SCRIPT > /dev/null

### PLUGINS (Symlink + Modules)

echo "\n----- PLUGINS -----\n"

code_plugins=(obsidian-dirtreeist unitade shiki-highlighter)
PLUGIN_SUBPATH=".obsidian/plugins"

symlink_plugins() {
  theirs="$1/$PLUGIN_SUBPATH"
  theirs_tmp="${theirs}_tmp_$$"

  # Resolve the absolute path of the target directory
  abs_theirs="$(cd "$theirs" 2>/dev/null && pwd)"
  [ -n "$abs_theirs" ] || return

  # 1. Create the shadow directory and duplicate existing state
  mkdir -p "$theirs_tmp"
  if [ -d "$theirs" ]; then
    cp -a "$theirs/." "$theirs_tmp/"
  fi

  # 2. Clear dead links
  for link in "$theirs_tmp"/*; do
    if [ -L "$link" ] && [ ! -e "$link" ]; then
      rm "$link"
    fi
  done

  # 3. Link BASE plugins to shadow directory for a more atomic operation
  for p in *; do
    target="$theirs_tmp/$p"
    # Don't overwrite concrete plugins
    [ -e "$target" ] && [ ! -L "$target" ] && continue

    abs_p="$(pwd)/$p"
    
    # Find the deepest common ancestor directory
    common="$abs_theirs"
    while true; do
      [ "$abs_p" = "$common" ] && break
      [ "$common" = "/" ] && break
      case "$abs_p" in
        "$common/"*) break ;;
      esac
      common="${common%/*}"
      [ -z "$common" ] && common="/"
    done

    # Calculate how many directories up we need to go from $abs_theirs
    dir_up="${abs_theirs#"$common"}"
    dir_up="${dir_up#/}"

    if [ -n "$dir_up" ]; then
      rel_path=$(printf "%s\n" "$dir_up" | sed 's|[^/][^/]*|..|g')
    else
      rel_path=""
    fi

    remainder="${abs_p#"$common"}"
    remainder="${remainder#/}"

    if [ -n "$rel_path" ]; then
      target_link="${rel_path}/${remainder}"
    else
      target_link="${remainder}"
    fi

    rm -f "$target"
    ln -sf "$target_link" "$target"
  done

  mv "$theirs" "${theirs}_old_$$" &&
  mv "$theirs_tmp" "$theirs" &&
  rm -rf "${theirs}_old_$$"

  sync
}

copy_code_plugins() {
  for p in *; do
    (( ${code_plugins[(Ie)$1]} )) || continue
    cd ../code_plugins && {
      [[ -e "$1/$PLUGIN_SUBPATH/$p" ]] || cp -r $p "$1/$PLUGIN_SUBPATH/$p"
      cd ../plugins
    } || echo "code_plugins not found"
  done
  
}

cd $OBSIDIAN_HOME/BASE/$PLUGIN_SUBPATH || exit 1

while read -r submodule; do
  [[ $submodule == BASE ]] && continue
  symlink_plugins $OBSIDIAN_HOME/$submodule
done < <(submodule_names)

copy_code_plugins $OBSIDIAN_HOME/Reference

### SYNCING
resolve_submodule_conflicts() {
  local all=true

  while IFS= read -r conflict; do
    local strategy=${STRATEGY[$conflict]}
    if [[ -n $strategy ]]; then
      git checkout --$strategy -- "$conflict" && git add "$conflict" && continue
      err "Error resolving $conflict with --$strategy"
    fi
    all=false
  done < <(git ls-files --unmerged)
  
  if $all; then
    git commit --no-edit
  else
    err "Could not resolve all conflicts. Please review and commit manually."
    return 1
  fi
}


# For each submodule, 
# log git diff + last changed
# settle-or-update
# use our submodule pointer if we are ahead, otherwise theirs

cd "$OBSIDIAN_HOME" || exit 1

echo "\n----- SYNCING -----\n"

check_network || { echo "No network" ; exit 1; } # should we do commits still?

if [ "$CHECK" != true ] && [ "$CHECK" != false ]; then
	[ -t 0 ] && CHECK=false || CHECK=true # default force check when interactive 
fi
export CHECK

submodules_to_add=()
typeset -A STRATEGY

while IFS= read -r submodule; do
  cd "$OBSIDIAN_HOME/$submodule"
  [[ -e .skip ]] && {
      info "Skipped due to .skip" 
      continue
  }
  info "Entering $submodule"

  $SSdir/git_update_settled.zsh || {
    info "Skipped" 
    continue
  }

  branch="$(git branch --show-current)"
  if [[ -n "$branch" ]]; then
    # git -C "$submodule" fetch origin || continue: already fetched in settle-or-update
    git rev-list --left-right --count $branch...origin/$branch | read -r ahead behind
    if [[ -z $behind ]]; then
    	echo "behind for $submodule empty, skipping (try checking git branch -r)" >&2
    	continue
    fi
    
    if ((behind == 0)); then
      STRATEGY[$submodule]=ours
      (( ahead != 0 )) && submodules_to_add+=($submodule)
      continue
    fi
  else
	  err "Could not find git branch!"
  fi
  STRATEGY[$submodule]=theirs

done < <(submodule_names)

# Apply submodule changes
cd "$OBSIDIAN_HOME"
info "All submodules processed (Added: ${submodules_to_add[*]})" # todo: omit modules without change
for submodule in ${submodules_to_add[@]}; do
  git add "$submodule"
done
git commit -m "Updated ${submodules_to_add[*]}" # todo: AI commits

info "Finished committing ahead submodules"
printf '%s merge=%s\n' ${(kv)STRATEGY} > $ATTRIBUTES_FILE
### FINISH

# autostash for non-rebase
restore_stash=false
stash_name="autostash-$DATE"
git stash push -m "$stash_name" --quiet 2>/dev/null &&
git stash list | grep -q "$stash_name" &&
restore_stash=true

[ -t 0 ] && noedit= || noedit="--no-edit"

git pull --no-rebase $noedit || # Note: If we wanted to rebase, unlike regular rebase, git submodule conflicts are easy to resolve as the conflicts are single line. Add to keep, reset to drop.
resolve_submodule_conflicts

git push --recurse-submodules=on-demand --porcelain # Only pushes referenced commits, that is, ahead commits

$restore_stash && git stash pop --quiet
info "Execution complete"

# 1. Restore stdout and stderr to close the pipes to the `tee` subshells
exec 1>&3 2>&4

# See https://www.zsh.org/mla/users/1999/msg00619.html
coproc exit
kill -KILL "$coproc_pid" 2>/dev/null || :