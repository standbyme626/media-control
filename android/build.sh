#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

SDK=${SDK:-${ANDROID_HOME:-$HOME/Android/Sdk}}
BT=${BUILD_TOOLS:-$(ls -d "$SDK"/build-tools/* 2>/dev/null | sort -V | tail -1)}
PLATFORM=${PLATFORM:-$(ls -d "$SDK"/platforms/android-*/android.jar 2>/dev/null | sort -V | tail -1)}
KS=${KS:-keystore.jks}
KSPASS=${KSPASS:-mediacontrol}

[ -x "$BT/aapt2" ] || { echo "aapt2 not found under $BT"; exit 1; }
[ -f "$PLATFORM" ] || { echo "android.jar not found under $PLATFORM"; exit 1; }

VER=$(grep -oP 'android:versionName="\K[^"]+' AndroidManifest.xml)
APK="MediaControl-v${VER}.apk"

rm -rf build
mkdir -p build/classes build/dex

ASSET_ARGS=()
if [ -f config.properties ]; then
  mkdir -p build/assets
  cp config.properties build/assets/defaults.properties
  ASSET_ARGS=(-A build/assets)
fi

javac -source 8 -target 8 -bootclasspath "$PLATFORM:$BT/core-lambda-stubs.jar" -encoding UTF-8 \
  -d build/classes $(find src -name '*.java')

jar cf build/classes.jar -C build/classes .

java -cp "$BT/lib/d8.jar" com.android.tools.r8.D8 --release --min-api 24 \
  --lib "$PLATFORM" --output build/dex build/classes.jar

"$BT/aapt2" link -o build/unsigned.apk -I "$PLATFORM" "${ASSET_ARGS[@]}" --manifest AndroidManifest.xml
(cd build/dex && zip -q ../unsigned.apk classes.dex)
"$BT/zipalign" -f 4 build/unsigned.apk build/aligned.apk

if [ ! -f "$KS" ]; then
  keytool -genkeypair -keystore "$KS" -alias mediacontrol -keyalg RSA -keysize 2048 \
    -validity 10000 -storepass "$KSPASS" -keypass "$KSPASS" -dname "CN=MediaControl"
fi

"$BT/apksigner" sign --ks "$KS" --ks-pass "pass:$KSPASS" --key-pass "pass:$KSPASS" \
  --out "$APK" build/aligned.apk

"$BT/apksigner" verify --print-certs "$APK" | head -4
ls -lh "$APK"
