#!/bin/zsh

IMAGES=${1:-~/Pictures/219Paintings}

background='#282a36'
selection='#44475a'
comment='#6272a4'
orange='#ffb86c'
red='#4c3743'
magenta='#ff79c6'
blue='#6272a4'

hide='00'
alpha='55'
alpha2='55'
white='#d8dee9'
red='#4c3743'
green='#80A070'

# obsolete args: 
# --slideshow-random-selection \
# --time-font=sans-serif

i3lock \
  --insidever-color=$blue$hide \
  --insidewrong-color=$blue$hide \
  --inside-color=$blue$hide \
  --ringver-color=$blue$hide \
  --ringwrong-color=$red$alpha \
  --ring-color=$blue$hide \
  --keyhl-color=$white$alpha \
  --line-color=$magenta$hide \
  --bshl-color=$orange$hide \
  --separator-color=$selection$hide \
  --verif-text="" \
  --wrong-text="" \
  --noinput="" \
  --time-str="  %H:%M" \
  --time-size="18" \
  --date-str="" \
  --time-pos="3200:1300" \
  --time-color=$white$alpha \
  --date-color=$white$alpha \
  --lock-text="" \
  --lockfailed="" \
  --radius=120 \
  --ring-width=10 \
  --pass-media-keys \
  --pass-screen-keys \
  --pass-volume-keys \
  --slideshow-interval 60 \
  -k \
   --ignore-empty-password \
  -i  "$(find $IMAGES -name "*.jpg" -o -name "*.png" | shuf -n1)"  &
sleep 5

while pgrep i3lock;
do
  xset dpms force off
  sleep 120
done

xset dpms 0 0 900
[ -t 1] && xset q

