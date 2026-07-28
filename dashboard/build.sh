#!/usr/bin/env bash
# Builds the dashboard with swiftc directly.
#
# Why not `swift build`? SwiftPM needs a full Xcode install; the Command Line
# Tools ship a PackageDescription library that cannot compile its own manifests.
# This script needs only the CLT, so the app builds on a stock macOS box.
# Package.swift is kept for anyone who does have Xcode.
set -euo pipefail
cd "$(dirname "$0")"

BUILD=.build
# Detect the host arch so this works on both Apple Silicon and Intel runners.
TARGET="$(uname -m)-apple-macosx14.0"
mkdir -p "$BUILD"

echo "==> Building TradebotKit (library)"
swiftc -target "$TARGET" -O \
  -module-name TradebotKit \
  -emit-module -emit-module-path "$BUILD/TradebotKit.swiftmodule" \
  -emit-library -o "$BUILD/libTradebotKit.dylib" \
  -Xlinker -install_name -Xlinker "@rpath/libTradebotKit.dylib" \
  Sources/TradebotKit/*.swift

echo "==> Building TradebotDashboard (app)"
swiftc -target "$TARGET" -O \
  -I "$BUILD" -L "$BUILD" -lTradebotKit \
  -Xlinker -rpath -Xlinker "@executable_path" \
  -o "$BUILD/TradebotDashboard" \
  Sources/TradebotDashboard/*.swift

echo "==> Building test runner"
swiftc -target "$TARGET" \
  -module-name TradebotKitTests \
  -I "$BUILD" -L "$BUILD" -lTradebotKit \
  -Xlinker -rpath -Xlinker "@executable_path" \
  -o "$BUILD/TradebotKitTests" \
  Tests/TradebotKitTests/*.swift

# macOS only grants a window to a real bundle. A bare executable launches,
# runs, and never shows UI -- so package it properly.
echo "==> Packaging TradebotDashboard.app"
APP="$BUILD/TradebotDashboard.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp "$BUILD/TradebotDashboard" "$APP/Contents/MacOS/"
cp "$BUILD/libTradebotKit.dylib" "$APP/Contents/MacOS/"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>              <string>Tradebot Dashboard</string>
  <key>CFBundleDisplayName</key>       <string>Tradebot Dashboard</string>
  <key>CFBundleExecutable</key>        <string>TradebotDashboard</string>
  <key>CFBundleIdentifier</key>        <string>dev.gkhurana.tradebot.dashboard</string>
  <key>CFBundleVersion</key>           <string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key>       <string>APPL</string>
  <key>LSMinimumSystemVersion</key>    <string>14.0</string>
  <key>NSHighResolutionCapable</key>   <true/>
  <key>NSPrincipalClass</key>          <string>NSApplication</string>
</dict>
</plist>
PLIST

echo
echo "Built:"
echo "  $APP                       — launch with ./run.sh"
echo "  $BUILD/TradebotKitTests    — run with ./test.sh"
