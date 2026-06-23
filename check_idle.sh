#!/bin/zsh

autoload -Uz colors && colors

info() {
  local last=$?
  if ((VERBOSE)); then
    if (($# == 1)); then
      print -u2 -- "${fg[blue]}[INFO]${reset_color} $1"
    else
      print -u2 -- "${fg[blue]}[INFO: $1]${reset_color} $2"
    fi
  fi
  return $last
}

infovar() {
  local last=$?
  if ((VERBOSE)); then
    while (($#)); do
      print -u2 -- "${fg[blue]}[INFO: $1]${reset_color} ${(P)1}"
      shift 1
    done
  fi
  return $last
}

bcuz() {
  ((VERBOSE)) && print -u2 -- "exit: $1"
  exit 0
}

case $OSTYPE in
  # Doesn't work with cron, maybe some dbus stuff, fuck cron.
  linux*)
    export DISPLAY=':1' 
    # https://www.reddit.com/r/linuxmint/comments/b1vs64/changing_the_behavior_of_the_cinnamon_lock_screen/
    cinnamon-screensaver-command -q | grep -q 'inactive' && bcuz cinnamon-screensaver
    xrandr | grep -q "DisplayPort-0 disconnected" && bcuz displayport0
    pgrep -x i3lock && bcuz i3lock
    xset q | grep "Monitor is Off" && bcuz monitor_powerstate
    ;;
  darwin*)
    BRIGHTNESS="$(/usr/local/bin/brightness -l | grep brightness | awk '{print $4}')"

    /usr/sbin/ioreg -r -k AppleClamshellState -d 4 | grep -i "AppleClamshellState" | head -1 | grep -qi "no"
    LID_CLOSED=$? # (1 for closed)

    (( LID_CLOSED || ! BRIGHTNESS )) && bcuz screen

    [ "$(/usr/libexec/PlistBuddy -c "print :IOConsoleUsers:0:CGSSessionScreenIsLocked" /dev/stdin 2>/dev/null <<< "$(/usr/sbin/ioreg -n Root -d1 -a)")" = "true" ]
    LOCKED=$?
    [ $LOCKED = 0 ] && bcuz locked

    IDLETIME="$(/usr/sbin/ioreg -c IOHIDSystem | awk '/HIDIdleTime/ {print int($NF/1000000000); exit}')"
    (( IDLETIME > 299 )) && infovar IDLE && bcuz idle
    ;;
  *)
    ;;
esac

exit 1