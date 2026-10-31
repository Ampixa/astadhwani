#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
OUTPUT="$PWD/builds/mac/artifacts"
APP="$OUTPUT/Astadhwani.app"
test -f "$APP/Contents/Info.plist"
codesign --verify --deep --strict "$APP"
STAGE="$PWD/build/dmg-stage"
mkdir -p "$STAGE"
ditto "$APP" "$STAGE/Astadhwani.app"
if [ ! -e "$STAGE/Applications" ]; then ln -s /Applications "$STAGE/Applications"; fi
cat > "$STAGE/READ ME FIRST.txt" <<'TXT'
Astadhwani Studio — Apple Silicon local research preview

Drag Astadhwani.app into Applications and open it. Python, dependencies and the wave-1 checkpoint are bundled; no Docker, Node or separate Python installation is required.

This is an ad-hoc-signed local build, not Developer ID signed or notarized for external distribution. If macOS blocks an externally transferred copy, do not disable security protections. A properly signed/notarized release is a separate step.

This preview includes restricted research weights. Do not redistribute publicly. Public Astadhwani release clearance and the wave-4 model update are separate.

Your voices and generations are stored in Application Support/Astadhwani/Studio, not inside the application. Replacing the app does not intentionally erase that workspace. Use the UI to remove local audio.
TXT
hdiutil create -volname 'Astadhwani Studio Preview' -srcfolder "$STAGE" -ov -format UDZO "$OUTPUT/Astadhwani-0.2.0-arm64.dmg"
hdiutil verify "$OUTPUT/Astadhwani-0.2.0-arm64.dmg"
(cd "$OUTPUT" && shasum -a 256 Astadhwani-0.2.0-arm64.dmg > Astadhwani-0.2.0-arm64.dmg.sha256)
