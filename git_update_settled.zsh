#!/bin/zsh

setopt extended_glob

### CONSTANTS

usage() {
  "$0 -i SETTLE_INTERVAL_MIN (Default: 15)"
}

if [ "$CHECK" != true ] && [ "$CHECK" != false ]; then
	CHECK=true
fi

SETTLE_INTERVAL_MIN=15

while getopts "is:" opt; do 
  case $opt in
    i) SETTLE_INTERVAL_MIN=$OPTARG ;;
    s) CHECK=false ;;
    *) usage ;;
  esac
done

shift $((OPTIND-1))

### UTILS

conflicts() {
  git status --porcelain | grep -q '^U'
}

get_mod() {
  if [ $GNU_STAT != true ] && [ $GNU_STAT != false ]; then
    if stat --version &>/dev/null; then
      GNU_STAT=true
    else
      GNU_STAT=false
    fi
  fi
  
  if $GNU_STAT; then
    stat -c "%Y %n" "$@"
  else
	  stat -f "%m %N" "$@"
  fi
}

is_detached() {
  ! git symbolic-ref -q HEAD >/dev/null
}

info() {
  echo "[INFO] $1"
}

dbg() {
  echo "[DEBUG] $1"
}

warn() {
  echo "[WARN] $1"
}

err() {
  echo "[ERROR] $1"
}

### CHECK SETTLED

if $CHECK; then
  diff_lines="$(git --no-pager diff HEAD --name-only --diff-filter=ACM && git ls-files --others --exclude-standard)"

  git status -sb | awk '!/^ / { print }'; git --no-pager diff --compact-summary # log summary

  conflicts && err "Conflicts found, skipping" && exit 1

  filtered_diffs=()
  while IFS= read -r f; do
    [[ "$f" != .obsidian/plugins/* ]] && filtered_diffs+=("$f")
  done <<< "$diff_lines"
    
  if [[ -n "$filtered_diffs" ]]; then
    get_mod $filtered_diffs | sort -nr -k 1,1 | read -r diff_time diff_last
    
    diff_interval=$(( $(date +%s) - diff_time ))
    diff_min=$(( diff_interval / 60))
    echo "Last changed (${diff_min}m ago): $diff_last @ $diff_time"
  else
    CHECK=false # always commit when no ACM diffs
  fi
fi

# This is not perfect at not losing progress, as a file may have unsaved changes. Just save more/autosave ig.
if ! $CHECK || ((diff_min > SETTLE_INTERVAL_MIN)); then
  git fetch
  git add -A # need to include untracked in stash

  should_exit=false
  git pull --rebase --autostash || should_exit=true
  
  if is_detached; then
   err "Rebase failed"
   exit 1
  elif git status --porcelain | grep -qE '^.?U'; then
  # log state when conflicted
  git --no-pager diff
  git --no-pager diff --cached

  # reset to previous state
  err "Stash apply failed, reverting"
  git checkout -f
  exit 1
  fi

  $should_exit && exit 1 # safety exit

  git add -A
  # AI message here
  git commit -m "$MACHINE_NAME Autocommit"
  exit 0
fi

exit 1 # exit 1 to skip
