#!/usr/bin/env bash
set -euo pipefail
# 需要 Android SDK 的 build-tools + platform（android-34）。用法：SDK=$HOME/android-sdk ./build.sh
SDK=${SDK:-$HOME/android-sdk-tools}
BT=$SDK/android-14
PLATFORM=$SDK/android-34/android.jar
cd "$(dirname "$0")"

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

if [ ! -f keystore.jks ]; then
  keytool -genkeypair -keystore keystore.jks -alias mediacontrol -keyalg RSA -keysize 2048 \
    -validity 10000 -storepass mediacontrol -keypass mediacontrol -dname "CN=MediaControl"
fi

"$BT/apksigner" sign --ks keystore.jks --ks-pass pass:mediacontrol --key-pass pass:mediacontrol \
  --out "$APK" build/aligned.apk

"$BT/apksigner" verify --print-certs "$APK" | head -4
ls -lh "$APK"
