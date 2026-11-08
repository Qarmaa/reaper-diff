#!/bin/sh
# Собирает "Reaper Diff.app": окно (app/*.swift) + rpp_diff.py и rpp.py в Resources/ptx_tools.
# Конвертер Reaper → Pro Tools не вкладывается: кнопка Pro Tools берёт установленную программу «Reaper to Pro Tools».
#   ./build.sh                 → dist/Reaper Diff.app и копия в /Applications
#   NO_INSTALL=1 ./build.sh D  → только сборка в D (так собирает release/release.sh)
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$HERE/dist}"
APP="$OUT/Reaper Diff.app"
VERSION="${APP_VERSION:-$(cat "$HERE/VERSION")}"

mkdir -p "$OUT"
CFG="$(mktemp -d)/UpdateConfig.swift"
"$HERE/release/make_config.sh" "$CFG"
swiftc -parse-as-library -target arm64-apple-macos14.0 \
    "$HERE/app/App.swift" "$HERE/app/Updater.swift" "$CFG" -o "$OUT/.ReaperDiff.new"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/ptx_tools"
mv "$OUT/.ReaperDiff.new" "$APP/Contents/MacOS/ReaperDiff"
cp "$HERE/engine/rpp_diff.py" "$HERE/engine/rpp.py" "$APP/Contents/Resources/ptx_tools/"
cp "$HERE/icon/DiffIcon.icns" "$APP/Contents/Resources/AppIcon.icns"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>Reaper Diff</string>
<key>CFBundleDisplayName</key><string>Reaper Diff</string>
<key>CFBundleIdentifier</key><string>local.pavel.reaper-diff</string>
<key>CFBundleIconFile</key><string>AppIcon</string>
<key>CFBundleExecutable</key><string>ReaperDiff</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>NSPrincipalClass</key><string>NSApplication</string>
<key>CFBundleShortVersionString</key><string>$VERSION</string>
<key>LSMinimumSystemVersion</key><string>14.0</string>
<key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST

codesign --force --sign - "$APP" >/dev/null 2>&1 || true
if [ -z "$NO_INSTALL" ]; then
    rm -rf "/Applications/Reaper Diff.app" && ditto "$APP" "/Applications/Reaper Diff.app"
    echo "/Applications/Reaper Diff.app"
else
    echo "$APP"
fi
