#!/bin/zsh
time_blocks=(
  "05:00-10:00"
  "10:00-12:00"
  "12:00-14:00"
  "14:00-17:00"
  "17:00-20:00"
  "20:00-22:00"
)

if [[ -n "$1" ]]; then
  heading="#$1"
else
  current_time=$(date +%H:%M)
  heading=""
  for block in "${time_blocks[@]}"; do
    start=${block%%-*}
    end=${block##*-}
    if [[ "$current_time" > "$end" ]]; then
    # if [[ "$current_time" > "$start" && "$current_time" < "$end" ]]; then
      heading="$block"
      break
    fi
  done
fi

# filepath=$(printf "%s" "$OBSIDIAN_HOME/Personal/Journal/$(date +%Y/%d-%m-%y)$heading" | jq -sRr @uri)
# o "obsidian://open?path=$filepath"

swspace 5
fs :o "obsidian://adv-uri?vault=Personal&daily=true&heading=$heading"