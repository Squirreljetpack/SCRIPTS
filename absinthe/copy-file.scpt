#!/usr/bin/osascript
on run args
  set abs_path to do shell script "readlink -f -- " & (first item of args)
  set the clipboard to POSIX file abs_path
end
