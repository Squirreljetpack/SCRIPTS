#!/bin/bash

while true; do
  # Run xkill and capture its output
  output=$(xkill -button 1 2>&1)
  # Check the output to determine whether a window was killed
  if [[ $output == *"killing creator of resource"* ]]; then
    echo "$output" >&2
  else
    break
  fi
done
