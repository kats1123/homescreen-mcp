#!/bin/zsh
# Build Homescreen.app and Homescreen-<version>.dmg into build/.
#   macapp/build.sh            full build
#   SKIP_ENGINE=1 macapp/build.sh   reuse the last engine build (shell/UI-only changes)
set -euo pipefail

ROOT=${0:A:h:h}
BUILD=$ROOT/build
VENV=${HOMESCREEN_BUILD_VENV:-$HOME/.local/share/homescreen-mcp/buildvenv}
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' $ROOT/pyproject.toml)
APP=$BUILD/Homescreen.app

if [[ ! -x $VENV/bin/pyinstaller ]]; then
  python3 -m venv $VENV
  $VENV/bin/pip install -q --upgrade pip pyinstaller
fi

if [[ -z ${SKIP_ENGINE:-} ]]; then
  echo "• engine"
  $VENV/bin/pip install -q --no-deps --force-reinstall $ROOT
  $VENV/bin/pip install -q $ROOT
  (cd $ROOT/macapp && $VENV/bin/pyinstaller --noconfirm --log-level ERROR \
      --distpath $BUILD/dist --workpath $BUILD/work engine.spec)
fi

echo "• shell"
(cd $ROOT/macapp/Shell && swift build -c release --arch arm64 2>&1 | grep -E "error|Build complete")
SHELL_BIN=$(cd $ROOT/macapp/Shell && swift build -c release --arch arm64 --show-bin-path)/Homescreen

echo "• app bundle"
rm -rf $APP
mkdir -p $APP/Contents/MacOS $APP/Contents/Resources
cp $SHELL_BIN $APP/Contents/MacOS/Homescreen
cp -R $BUILD/dist/engine $APP/Contents/Resources/engine
cp $ROOT/LICENSE $ROOT/THIRD_PARTY_NOTICES.md $APP/Contents/Resources/
$VENV/bin/python $ROOT/macapp/make_icon.py $BUILD/AppIcon.iconset
iconutil -c icns $BUILD/AppIcon.iconset -o $APP/Contents/Resources/AppIcon.icns
cat > $APP/Contents/Info.plist <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Homescreen</string>
  <key>CFBundleDisplayName</key><string>Homescreen</string>
  <key>CFBundleIdentifier</key><string>io.github.kats1123.homescreen</string>
  <key>CFBundleExecutable</key><string>Homescreen</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.utilities</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSLocalNetworkUsageDescription</key><string>Homescreen discovers and connects to your paired iPhone or iPad to read and apply Home Screen layouts over Wi-Fi.</string>
  <key>NSBonjourServices</key><array><string>_remotepairing._tcp</string></array>
  <key>NSHumanReadableCopyright</key><string>MIT License. Not affiliated with Apple.</string>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST

echo "• ad-hoc signing (not notarized)"
codesign --force --deep --sign - $APP
codesign --verify --deep --strict $APP

echo "• dmg"
DMG=$BUILD/Homescreen-$VERSION.dmg
STAGE=$BUILD/dmg
rm -rf $STAGE $DMG && mkdir -p $STAGE
cp -R $APP $STAGE/ && ln -s /Applications $STAGE/Applications
hdiutil create -quiet -volname "Homescreen $VERSION" -srcfolder $STAGE -ov -format UDZO $DMG
rm -rf $STAGE
echo "built $APP ($(du -sh $APP | cut -f1)) and $DMG ($(du -sh $DMG | cut -f1))"
