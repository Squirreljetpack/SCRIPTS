#!/usr/bin/osascript

tell application "System Events"
    tell application process "NotificationCenter"
        repeat while exists (UI elements of scroll area 1 of window 1)
            try
                perform (first action of group 1 of UI element 1 of ¬
                    scroll area 1 of windows where description is "Close")
            end try
            delay 1
        end repeat
    end tell
end tell