#!/usr/bin/env bash
# Lab-only installer for the Zara privileged companion (T3 tier).
# Emulator/lab images ONLY. Refuses physical phones and user builds.
# Usage: bash scripts/setup_privileged_lab.sh --companion privcompanion.apk [--yes]
set -euo pipefail

APK=""
YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --companion) APK="${2:-}"; shift 2 ;;
    --yes) YES=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[ -n "$APK" ] && [ -f "$APK" ] || { echo "need --companion <apk>" >&2; exit 2; }
command -v adb >/dev/null || { echo "adb not found" >&2; exit 2; }

fail() { echo "REFUSED: $*" >&2; exit 3; }

QEMU="$(adb shell getprop ro.kernel.qemu 2>/dev/null | tr -d '\r')"
[ "$QEMU" = "1" ] || fail "not an emulator (ro.kernel.qemu=$QEMU)"
BTYPE="$(adb shell getprop ro.build.type 2>/dev/null | tr -d '\r')"
[ "$BTYPE" != "user" ] || fail "user build — lab images only"
SERIAL="$(adb get-serial-no 2>/dev/null | tr -d '\r')"
case "$SERIAL" in emulator-*) ;; *) fail "serial $SERIAL is not an emulator" ;; esac

echo "target: $SERIAL (emulator, build type $BTYPE)"
if [ "$YES" != "1" ]; then
  printf 'push %s to /system/priv-app (needs adb root + remount + reboot)? [y/N] ' "$APK"
  read -r ans
  [ "$ans" = "y" ] || { echo "aborted"; exit 0; }
fi

adb root >/dev/null
sleep 1
adb remount || fail "remount failed (needs a writable emulator image)"
adb push "$APK" /system/priv-app/ZaraPrivCompanion/privcompanion.apk
adb shell chmod 644 /system/priv-app/ZaraPrivCompanion/privcompanion.apk
# Priv-app permission allowlist (WITHOUT this, system_server boot-loops:
# "Signature|privileged permissions not in privileged permission
# allowlist"). Pushed in the same remount session, before the reboot.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ALLOW="$SCRIPT_DIR/../android/privileged/privapp-permissions-zara.xml"
[ -f "$ALLOW" ] || fail "allowlist xml missing: $ALLOW"
adb push "$ALLOW" /system/etc/permissions/privapp-permissions-zara.xml
adb shell chmod 644 /system/etc/permissions/privapp-permissions-zara.xml
adb reboot
adb wait-for-device
echo "rebooted; waiting for boot..."
until [ "$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ]; do sleep 2; done

if adb shell pm list packages | grep -q dev.zara.privcompanion; then
  echo "OK: dev.zara.privcompanion installed as a system package"
else
  echo "MISSING: companion package not visible after reboot" >&2
  exit 4
fi
# Verify priv-app placement (the actual prerequisite for
# TETHER_PRIVILEGED to be effective — not just package visibility).
if adb shell dumpsys package dev.zara.privcompanion 2>/dev/null \
    | grep -q "/system/priv-app"; then
  echo "OK: companion lives in /system/priv-app (privileged grant effective)"
else
  echo "WARN: companion visible but NOT under /system/priv-app — privileged"
  echo "      permissions will not apply; reinstall to the priv-app path."
fi
echo "Next: install the main app signed with the SAME lab keystore, then run"
echo "a system.hotspot.set job — expect applied:true only on verified tether."
echo ""
echo "Rollback/uninstall (emulator only):"
echo "  adb root && adb remount && \\"
echo "    adb shell rm -rf /system/priv-app/ZaraPrivCompanion && adb reboot"
echo "Main-app reinstall with its own key restores the stock signature state."
