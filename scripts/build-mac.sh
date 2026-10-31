#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
: "${ASTADHWANI_PYTHON:?Set ASTADHWANI_PYTHON to the tested Python 3.11 executable}"
: "${ASTADHWANI_MODEL_DIR:?Set ASTADHWANI_MODEL_DIR to the reviewed local checkpoint}"
if [ "$(uname -m)" != arm64 ]; then echo 'This build targets Apple Silicon.' >&2; exit 1; fi
npm run build
mkdir -p build builds/mac/artifacts
APP="$PWD/builds/mac/artifacts/Astadhwani.app"
RES="$APP/Contents/Resources"
mkdir -p "$APP/Contents/MacOS" "$RES/studio/backend" "$RES/studio/web" "$RES/models/wave1"
BASE_PREFIX="$($ASTADHWANI_PYTHON -c 'import sys; print(sys.base_prefix)')"
if [ ! -e build/runtime-env/bin/python ]; then uv venv --python "$ASTADHWANI_PYTHON" build/runtime-env; fi
uv pip sync --python build/runtime-env/bin/python requirements-mac.lock
SITE="$PWD/build/runtime-env/lib/python3.11/site-packages"
# Copy the standalone CPython distribution, not an interpreter symlink into this machine.
if [ -d "$RES/python" ]; then rm -rf "$RES/python"; fi
if [ -d "$RES/site-packages" ]; then rm -rf "$RES/site-packages"; fi
ditto "$BASE_PREFIX" "$RES/python"
ditto "$SITE" "$RES/site-packages"
cp backend/server.py backend/model-profile.json "$RES/studio/backend/"
ditto builds/web/site "$RES/studio/web/dist"
# Explicit model allowlist: omit trainer state, training_args.bin and unrelated files.
for name in model.safetensors codec.pth config.json generation_config.json configuration_arktts.py modeling_arktts.py modeling_arktts_codec.py processing_arktts.py processor_config.json tokenizer.json tokenizer_config.json special_tokens_map.json chat_template.jinja; do
  test -f "$ASTADHWANI_MODEL_DIR/$name"
  cp -L "$ASTADHWANI_MODEL_DIR/$name" "$RES/models/wave1/$name"
done
swiftc -target arm64-apple-macos14.0 desktop/Astadhwani.swift -o "$APP/Contents/MacOS/Astadhwani" -framework AppKit -framework WebKit -O
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd"><plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>Astadhwani</string><key>CFBundleIdentifier</key><string>com.ampixa.astadhwani.preview</string><key>CFBundleName</key><string>Astadhwani</string><key>CFBundleDisplayName</key><string>Astadhwani</string><key>CFBundlePackageType</key><string>APPL</string><key>CFBundleShortVersionString</key><string>0.2.0</string><key>CFBundleVersion</key><string>2</string><key>LSMinimumSystemVersion</key><string>14.0</string><key>NSMicrophoneUsageDescription</key><string>Record a voice reference for local speech generation. Your audio stays on this Mac.</string><key>NSHighResolutionCapable</key><true/><key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST
# The launcher has an ad-hoc identity for local integrity; no Developer ID or notarization is claimed.
cp desktop/notices/* "$RES/"
python3 scripts/artifact-manifest.py "$APP"
echo "App assembled: $APP"

# Seal after all resources have been assembled. Runtime disables bytecode writes.
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"
