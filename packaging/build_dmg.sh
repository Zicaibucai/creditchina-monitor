#!/bin/zsh
set -euo pipefail

ROOT_DIR="${0:A:h:h}"
BUILD_DIR="$ROOT_DIR/build/macos-arm64"
APP_DIR="$BUILD_DIR/中建探员.app"
CONTENTS_DIR="$APP_DIR/Contents"
RESOURCES_DIR="$CONTENTS_DIR/Resources"
DMG_ROOT="$BUILD_DIR/dmg-root"
DIST_DIR="$ROOT_DIR/dist"

if [[ "$BUILD_DIR" != "$ROOT_DIR/build/macos-arm64" ]]; then
  print -u2 "构建目录校验失败"
  exit 1
fi

npm --prefix "$ROOT_DIR/frontend" run build

if ! "$ROOT_DIR/.venv/bin/python" -m PyInstaller --version >/dev/null 2>&1; then
  print -u2 "缺少 PyInstaller，请先执行：.venv/bin/pip install pyinstaller"
  exit 1
fi

rm -rf "$BUILD_DIR"
mkdir -p "$CONTENTS_DIR/MacOS" "$RESOURCES_DIR/backend" "$RESOURCES_DIR/runtime" \
  "$RESOURCES_DIR/frontend" "$RESOURCES_DIR/packaging" "$RESOURCES_DIR/defaults" "$DMG_ROOT" "$DIST_DIR"

"$ROOT_DIR/.venv/bin/python" -m PyInstaller \
  --noconfirm --clean --onedir \
  --name zhongjian-agent-backend \
  --distpath "$BUILD_DIR/pyinstaller-dist" \
  --workpath "$BUILD_DIR/pyinstaller-work" \
  --specpath "$BUILD_DIR" \
  --collect-all playwright \
  --collect-submodules uvicorn \
  "$ROOT_DIR/api_server.py"

cp -R "$BUILD_DIR/pyinstaller-dist/zhongjian-agent-backend/." "$RESOURCES_DIR/backend/"
cp "$(which node)" "$RESOURCES_DIR/runtime/node"
cp -R "$ROOT_DIR/frontend/dist/server" "$RESOURCES_DIR/frontend/server"
cp -R "$ROOT_DIR/frontend/dist/client" "$RESOURCES_DIR/frontend/client"
cp "$ROOT_DIR/packaging/frontend-server.mjs" "$RESOURCES_DIR/packaging/frontend-server.mjs"
cp "$ROOT_DIR/.env.example" "$RESOURCES_DIR/defaults/.env.example"
cp "$ROOT_DIR/monitor_companies.txt" "$RESOURCES_DIR/defaults/monitor_companies.txt"
cp "$ROOT_DIR/packaging/Info.plist" "$CONTENTS_DIR/Info.plist"

swiftc "$ROOT_DIR/packaging/ZhongjianAgentApp.swift" \
  -o "$CONTENTS_DIR/MacOS/中建探员" \
  -framework AppKit -framework WebKit

chmod +x "$CONTENTS_DIR/MacOS/中建探员" "$RESOURCES_DIR/backend/zhongjian-agent-backend" "$RESOURCES_DIR/runtime/node"
codesign --force --deep --sign - "$APP_DIR"

cp -R "$APP_DIR" "$DMG_ROOT/中建探员.app"
ln -s /Applications "$DMG_ROOT/Applications"
rm -f "$DIST_DIR/中建探员-arm64.dmg"
hdiutil create -volname "中建探员" -srcfolder "$DMG_ROOT" -ov -format UDZO "$DIST_DIR/中建探员-arm64.dmg"

print "已生成：$DIST_DIR/中建探员-arm64.dmg"
