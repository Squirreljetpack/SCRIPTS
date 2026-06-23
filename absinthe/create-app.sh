# Create Icon
mkdir Icon.iconset
echo "Create icon following format icon_256x256.png in Icon.iconset:"
read answer
if [[ "$answer" == "y" || "$answer" == "Y" ]]; then
    echo "Proceeding..."
else
    echo "Aborted." && rm -rf Icon.iconset && exit 0
fi

# Create App Bundle Structure
mkdir -p ~/Applications/Audacious.app/Contents/MacOS
mkdir -p ~/Applications/Audacious.app/Contents/Resources

# Copy Audacious Executable
cp /opt/homebrew/bin/audacious ~/Applications/Audacious.app/Contents/MacOS/

iconutil -c icns Icon.iconset
mv AudaciousIcon.icns ~/Applications/Audacious.app/Contents/Resources/

# Create Info.plist
cat >~/Applications/Audacious.app/Contents/Info.plist <<EOL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>audacious</string>
    <key>CFBundleIdentifier</key>
    <string>com.audacious.app</string>
    <key>CFBundleName</key>
    <string>Audacious</string>
    <key>CFBundleDisplayName</key>
    <string>Audacious</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleIconFile</key>
    <string>AudaciousIcon</string>
    <!-- You can add more keys as needed -->
</dict>
</plist>
EOL

# Set Executable Permissions
chmod +x ~/Applications/Audacious.app/Contents/MacOS/audacious

rm -rf Icon.iconset
