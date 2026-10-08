# Zara as First-Class AOSP Component — System Architecture Research

Status: research-only. No code changed. No device modified.
Scope: minimum viable architecture for "Zara" as part of an AOSP-based system image.
Current baseline: Zara Android body is a **normal user app** (Flutter + Kotlin `DeviceBridge.kt` over `zara/device` MethodChannel, runtime permissions, Keystore-backed key, allowlisted jobs `{system.battery, system.network}`, no `exec`, no local reasoning/authorization). See `docs/ANDROID_ARCHITECTURE.md`, `docs/SECURITY_MODEL.md`.

> ## Hard prohibitions (normative, non-negotiable)
>
> 1. **NEVER unlock, root, flash, wipe, or otherwise modify the user's existing physical Vivo phone** (or any physical user device). No bootloader unlock, no flashing, no exploit chain, no bootloader tampering, no data wipe on user hardware.
> 2. **No exploit chains** to escalate privilege on any device the project does not own and build itself.
> 3. **Experiments, if any, target the Android emulator (or Cuttlefish) only**, using the already-available emulator workflow (cf. `docs/STAGE6_EMULATOR_TEST_REPORT.md`). No new physical-device procedures are authorized by this document.
> 4. **No large downloads** (full AOSP tree / vendor blobs / factory images) are authorized as part of this task. AOSP build validation is explicitly out of scope until separately approved.

## 1. What "first-class component" means in AOSP terms

A normal app lives in `/data/app`, gets a per-app UID, and can only use public SDK APIs plus user-granted runtime permissions. A first-class system component means one or more of:

- (a) app preinstalled in a read-only partition (`system/priv-app`, `system_ext/priv-app`, `product/...`) with access to `signature|privileged` permissions;
- (b) code running as system UID (`android.uid.system`) or inside `system_server`;
- (c) a new Binder service registered with `ServiceManager` and callable system-wide;
- (d) new SELinux domains/contexts and new platform-defined permissions enforced by the OS.

Each step up buys capability but multiplies build, signing, and update burden. The recommendation in §5 is to take the **fewest** such steps that still beat a normal app.

## 2. Full privileged architecture — piece by piece

### 2.1 Custom privileged app (priv-app)

- **What it is:** APK preinstalled under `/system/priv-app/`, `/system_ext/priv-app/`, or `/product/priv-app/` instead of `/data/app`.
- **What it buys over a normal app [FACT]:** eligibility for permissions with `protectionLevel="signature|privileged"` (or `privileged`), which PackageManager will never grant to a `/data/app` APK. Also survives factory reset, can be granted default runtime permissions at image build time, can use some `@SystemApi` / hidden APIs only if also platform-signed (see §2.7).
- **What it costs:** must be part of the system image build (`PRODUCT_PACKAGES`), must ship a `privapp-permissions-*.xml` allowlist entry (since Android 8, privileged permissions are deny-by-default without an allowlist entry), must be signed consistently with the image keys.
- **Partition note [FACT]:** `system` = core framework; `system_ext` = OEM/system extensions that depend on system APIs; `product` = product-specific apps/experience; `vendor` = SoC/board HALs (Treble boundary — app code does not belong in `vendor`). Zara app logic belongs in `system_ext` or `product`, **not** `vendor`, and definitely not as a HAL.

### 2.2 Custom system service + ServiceManager registration

- **Mechanism [FACT]:** long-lived Java (or native) services run inside `system_server` (or standalone `servicemanager`-registered native daemons) and publish a Binder token via `ServiceManager.addService("zara", binder)`. Clients call `ServiceManager.getService("zara")` / `ServiceManager.checkService()` and transact via a generated AIDL interface.
- **Integration points [FACT]:** for a Java service: (1) add service class under `frameworks/base/services/core/java/com/android/server/zara/`; (2) declare interface in `*.aidl`; (3) start it from `SystemServer.startOtherServices()` or via the `SystemServiceRegistry` / `SystemService` subclass pattern; (4) expose a `Context.getSystemService()` / `SystemServiceRegistry.registerService` accessor and (optionally) an SDK `@SystemApi` wrapper so the Zara app can call it without raw `ServiceManager` reflection.
- **Cost:** this is framework surgery. Every Android version rebase must carry the patch; API surface must be versioned; a crash in `system_server` reboots the device. This is the single most expensive piece and should be deferred unless §5's minimum proves insufficient.

### 2.3 Binder IPC with explicit permission checks

- **Mechanism [FACT]:** Binder is the kernel-mediated IPC; AIDL generates the proxy/stub. Security is **not** automatic — each entry point must enforce.
- **Required pattern [FACT]:** every public method in the service must call `enforceCallingPermission(Manifest.permission.X, ...)` or `enforceCallingOrSelfPermission`, and/or check UID/PID via `Binder.getCallingUid()` / `getCallingPid()` against an allowlist (e.g., only Zara priv-app UID or `SYSTEM_UID`). For sensitive ops, also gate on `UserHandle`/foreground-user and audit the call.
- **Failure mode if skipped [FACT]:** an unenforced Binder service is a system-wide confused deputy — any app with the Binder descriptor can call it. Review must treat missing `enforceCalling*` as a P0 defect.

### 2.4 SystemServer integration points (summary)

| Hook | File / class (AOSP `frameworks/base`) | Purpose |
|---|---|---|
| Service startup | `services/java/com/android/server/SystemServer.java` → `startOtherServices()` | instantiate + publish `zara` service |
| Service base | `services/core/java/com/android/server/SystemService.java` | lifecycle (`onStart`, `onBootPhase`) |
| Client accessor | `core/java/android/app/SystemServiceRegistry.java` | `ZARA_SERVICE` string + creator |
| Permission def | `core/res/AndroidManifest.xml` | `<permission>` declarations |
| Priv allowlist | `data/etc/privapp-permissions-platform.xml` (or product/system_ext overlay) | grant `privileged` perms to Zara priv-app package name |

### 2.5 Custom permission definition (protection levels)

- **Mechanism [FACT]:** new permissions are declared in the platform manifest (`frameworks/base/core/res/AndroidManifest.xml`) as `<permission android:name="com.zara.permission.X" android:protectionLevel="..."/>`.
- **Relevant levels [FACT]:** `normal` (auto-grant), `dangerous` (runtime grant), `signature` (only same-signer as declarer), `signature|privileged` (same-signer **or** priv-app + allowlist), plus flags/modifiers such as `privileged`, `internal`, `role`, `knownSigner`. Exact taxonomy shifts between releases; the build-time source of truth is `frameworks/base/core/res/AndroidManifest.xml` + `PermissionInfo` docs for the target API level.
- **Zara guidance:** define **one** narrow custom permission first (e.g., `com.zara.permission.ACCESS_ZARA_SERVICE`, `signature|privileged`), enforced in every Binder entry point. Do not invent a family of permissions up front. Dangerous-level custom permissions add settings/UX surface for no benefit at this stage.

### 2.6 SELinux policy additions

- **Why needed [FACT]:** AOSP ships SELinux in enforcing mode. A new Binder service name, a new app domain, or new file locations are denied by default.
- **Minimum additions [FACT-pattern, exact labels version-dependent]:**
  1. `service_contexts`: map Binder name → context, e.g. `zara  u:object_r:zara_service:s0`.
  2. `.te` for the service domain: allow `add_service`/`find_service` transitions between `system_server` (or the hosting domain) and clients; `binder_call`/`binder_transfer` rules.
  3. App domain: if Zara stays a normal priv-app, it typically reuses `priv_app`/`system_app` domains via `seapp_contexts` mapping by package/signature — a brand-new app domain (custom `.te`) is only needed if Zara needs capabilities no stock domain has.
  4. File contexts for any new files on the read-only partitions.
- **Cost:** each rule is version- and device-policy-sensitive (`system/sepolicy` + vendor policy must compose under Treble/CIL). Overly broad `allow` rules (`untrusted_app`, `unrestricted`) are a security defect. Keep the policy diff to a handful of lines or defer the custom service entirely.

### 2.7 Platform signing requirements

- **[FACT]:** `signature`-level permissions and `android:sharedUserId="android.uid.system"` require the APK's signing key to match the key that signed the package declaring the permission (usually the `platform` key). `privileged`-level grants additionally require priv-app location + allowlist entry, but the allowlisted APK does **not** always need the platform key — `signature|privileged` grants via *either* path.
- **Consequence:** if Zara only needs `signature|privileged` *via the priv-app path*, it can be signed with its own product key plus allowlist. If it needs true `signature` platform permissions, `SystemApi`, or `sharedUserId=system`, it **must** be signed with the image's `platform` key. Running as system UID is strongly discouraged for new code (shared-UID deprecation direction; massive blast radius).
- **Keys involved [FACT]:** AOSP key set is `platform / shared / media / networkstack / releasekey / testkey`, generated by `development/tools/make_key` and applied at `sign_target_files_apks` time. Losing the private keys = cannot ship updates that the device will accept as the same OS.

### 2.8 Build-system placement (`Android.bp` / `PRODUCT_PACKAGES` / partitions)

- **[FACT]:** modern AOSP uses Soong (`Android.bp`) rather than `Android.mk` for new modules. A prebuilt or source-built APK is wrapped in an `android_app_import` / `android_app` module and pulled into the image by adding its module name to `PRODUCT_PACKAGES` in the product/device makefile (`device/<vendor>/<product>/...mk`).
- **Placement rule:** Zara app → `system_ext` or `product` partition (e.g., `system_ext_priv-app` / `product_priv-app` via `LOCAL_PRIVILEGED_MODULE` / Soong `privileged: true`). Framework service + permission XML + `service_contexts` → `system` / `system_ext`. Nothing Zara-specific belongs in `vendor` (Treble/VNDK boundary; vendor must keep booting with a generic system image).
- **Priv allowlist placement [FACT]:** `etc/permissions/privapp-permissions-<device>.xml` packaged with the same partition as the app.

### 2.9 Verified Boot / AVB implications

- **[FACT]:** production devices use Android Verified Boot (AVB/`vbmeta` + `dm-verity`/`dm-verity`-protected `system`/`vendor`/`product` partitions). Any byte change to a verified partition invalidates the hash tree unless the image is re-signed with keys the bootloader trusts.
- **Consequence [FACT]:** there is no "just push one APK into `/system/priv-app` on a locked production phone." Doing this on user hardware requires an unlocked bootloader + a custom root of trust (own AVB keys) or `userdebug`/`eng` builds with verity disabled — all of which are **explicitly forbidden on the user's Vivo** by this document's prohibitions. On emulator/Cuttlefish images (which already run `userdebug`/`test-keys` with verity commonly disabled), iteration is possible without touching user hardware.
- **Key management:** release images must be signed with private, access-controlled AVB + APK keys; `test-keys` (public AOSP default keys) must never ship to anything treated as secure.

### 2.10 OTA / update consequences

- **[FACT]:** A/B (seamless) OTAs are block-level payloads signed with the release keys; the updater verifies them against the on-device trust anchors.
- **Consequence [FACT]:** forking the system image forks the update stream. Zara images need their **own OTA generation + hosting** (`ota_from_target_files`), their own key rotation story, and a rebase process for each upstream Android security bulletin. Users of a Zara image can no longer take the OEM's stock OTAs; skipping rebases leaves devices on stale SPLs. This is the dominant long-term cost of the full-OS route and the main reason §5 minimizes new framework surface.

## 3. Lighter-weight alternative: Device Owner (no OS rebuild)

- **What it is [FACT]:** a Device Owner (DO) is a managed-device admin app provisioned as the device's policy controller via `DevicePolicyManager`. It is still a normal APK (no platform key, no priv-app, no SELinux change), but once provisioned it can exercise `DevicePolicyManager`/`DevicePolicyController` APIs ordinary apps cannot: silent install/uninstall, lock-task/kiosk, provisioning, restrictions, managed configurations, certificate management, some delegation scopes.
- **Provisioning [FACT]:** QR-code / NFC / cloud-enrollment during out-of-box setup, or (test-only, pre-provisioned devices) `adb shell dpm set-device-owner <pkg>/.<AdminReceiver>`. It **cannot** be granted retroactively on an already-provisioned daily-driver account without wiping — which is why this document authorizes DO experiments **on emulator images only**, never by wiping the user's phone.
- **What DO can host for Zara [FACT]:** fleet-style control of Zara test devices (silent APK updates, kiosk/lock-task demo mode, policy restrictions, managed key/cert install, delegated scopes like `DELEGATION_PACKAGE_INSTALLATION`). Useful for emulator labs and dedicated Zara demo devices built from scratch.
- **What DO cannot do [FACT]:** grant `signature`/`privileged` permissions, add a system service, change SELinux policy, call `@SystemApi`/hidden APIs beyond the DevicePolicyManager surface, impersonate system UID, bypass runtime-permission or Verified-Boot guarantees. It does not make Zara "part of the OS"; it makes Zara the *administrator of* a stock OS.
- **Relation to other modes [FACT]:** legacy `DeviceAdmin` is deprecated; Profile Owner manages only a work profile; Device Owner manages the whole device but only one can exist and provisioning is setup-time. None is a substitute for a custom Binder service if Zara genuinely needs new OS-enforced primitives.

## 4. Minimum viable architecture (fewest privileged pieces that still beat a normal app)

Recommendation: two rungs. Do Rung 0 first; only climb to Rung 1 when a concrete capability forces it.

### Rung 0 — Device Owner + normal app (no image fork) — DEFAULT

- Pieces: zero new privileged OS pieces. Zara APK stays a normal (or emulator `dpm`-provisioned Device Owner) app; all brains/policy stay in existing Zara Core server; device keeps enforcing stock policy.
- Real gain over today: silent install/update + policy/kiosk control of lab/demo devices, managed configs, cert management — without forking AOSP, without keys, without OTA infrastructure.
- Presupposes: nothing beyond the current repo + emulator. No `platform` key, no `SystemServer` patch, no SELinux change, no AVB/OTA burden.
- Choose this unless a requirement explicitly needs a permission or primitive the SDK + DPM surface cannot provide.

### Rung 1 — One priv-app + one custom permission on OWN emulator-built image (no custom system service yet)

- New privileged pieces (minimal set): (1) Zara APK as priv-app in `system_ext`/`product` on an **own-built emulator image**; (2) **one** `signature|privileged` custom permission + `privapp-permissions` allowlist entry, enforced server-side/SEP-side; (3) platform/build signing with **own** keys. Explicitly **no** new `system_server` service, **no** new SELinux domain, **no** `sharedUserId=system` at this rung.
- Real gain over Rung 0 / normal app: access to `signature|privileged` platform capabilities the SDK denies normal apps (exact set chosen per API level, e.g., background/privileged operations needed for always-on voice body), pre-granted defaults, factory-reset persistence.
- Still deferred: custom Binder service + `service_contexts` + custom `.te` domain + `SystemServer` patch (add only when Zara needs an OS-enforced primitive — e.g., mediating a new hardware path — that neither SDK nor DPM exposes). Each deferred piece avoids a permanent rebase/OTA cost.
- Presupposes (§5): own AOSP build for emulator (`lunch` target for `sdk_phone`/`gsi`/Cuttlefish), own key set, own AVB signing for test images, acceptance that these images fail Play Integrity and are lab-only.

### What is deliberately NOT minimal (do not build now)

Custom `system_server` service + broad SELinux domain + `sharedUserId=system` + vendor-partition changes. This maximizes rebase, security-review, and OTA cost while the Rung 0/1 gains are still unexploited.

## 5. Signing / build infrastructure presupposed by any image work

- **Own source + own keys [FACT]:** any shippable image needs a pinned AOSP manifest/branch, reproducible build host, and privately held key material (`platform`, `releasekey`, AVB keys via `make_key`; `sign_target_files_apks`; `avbtool`). Key loss = update stream death; key leak = attacker can sign malicious "updates."
- **`test-keys` vs release keys [FACT]:** AOSP default `test-keys` are public and well-known. `userdebug`/`eng` emulator images with `test-keys` and disabled verity exist for development **only**. Any image presented as trustworthy must use private release keys, `user` variant, enforcing SELinux, locked-verified-boot semantics on lab hardware that supports it.
- **SafetyNet / Play Integrity [FACT]:** custom images, unlocked bootloaders, `test-keys`, or disabled verity fail `MEETS_DEVICE_INTEGRITY` and cannot pass `STRONG`; many banking/GPay/DRM-dependent apps refuse to run. Lab Zara images must be treated as integrity-failing by design; this is another reason lab images and the user's daily-driver phone must never be the same device.
- **OTA/hosting [FACT]:** forking the image means running OTA payload generation, signing, hosting, rollback protection, and per-bulletin rebases (§2.10). Budget this before promising any "Zara OS" deliverable.

## 6. Emulator-only validation plan (no device modification, no large downloads now)

1. Keep current Stage-6 emulator flow (normal APK install, `dpm` Device-Owner check on a fresh emulator snapshot) — no AOSP checkout needed; this validates Rung 0.
2. Rung-1 build validation is **deferred** until explicitly approved, because it requires a full AOSP sync + build (tens of GB, hours) — called out here only so the cost is visible, not to authorize it.
3. When authorized, build only an emulator target (`sdk_gphone`/`Cuttlefish`), with own keys, and assert: priv-app location, `dumpsys package` shows `signature|privileged` grant via allowlist, Binder permission denial for unprivileged callers (negative test), SELinux `avc: denied` audit clean, `vbmeta`/verity state documented, OTA payload signs+verifies with own keys.

## 7. Claim ledger — FACT vs INFERENCE vs UNKNOWN/NEEDS_EXPERIMENT

Conventions: **FACT** = verifiable in AOSP source/docs for the pinned target version or by on-device `dumpsys`/audit on an emulator the team already runs. **INFERENCE** = reasoned extension, plausible but not yet demonstrated for Zara. **UNKNOWN** = must be measured on an emulator before it constrains design.

- **FACT:** normal apps cannot obtain `signature|privileged` permissions; priv-app + allowlist can (via either signer-or-allowlist path for `signature|privileged`).
- **FACT:** new Binder services require `ServiceManager` registration, per-method `enforceCallingPermission`, `service_contexts`, and SELinux allows; unenforced endpoints are world-callable.
- **FACT:** `SystemServer.startOtherServices` / `SystemServiceRegistry` / platform `AndroidManifest.xml` / `privapp-permissions-*.xml` / `Android.bp`+`PRODUCT_PACKAGES` / `system` vs `system_ext` vs `product` vs `vendor` / AVB+dm-verity / signed A/B OTA / `test-keys`-vs-release / Play Integrity failure on custom images / Device-Owner capabilities and setup-time-only provisioning are as described in §§2–3, 5.
- **INFERENCE:** Rung 0 (DO) + Rung 1 (single priv-app + single custom permission, no new system service) is sufficient for the next demonstrable Zara capability jump (persistent privileged body + managed lab devices), because the current bottleneck is distribution/policy control, not missing OS primitives. To confirm, map each requested "OS-level" Zara capability to SDK-vs-DPM-vs-privileged API before adding framework code.
- **INFERENCE:** deferring the custom `system_server` service keeps rebase/OTA/SELinux cost near zero while preserving the option to add it later behind the single custom permission.
- **UNKNOWN / NEEDS_EXPERIMENT (emulator only):** exact permission taxonomy and partition placement for the target API level (re-verify against the pinned `frameworks/base` + `system/sepolicy` tree, since names shift yearly); which specific privileged permission(s) Rung 1 actually needs for always-on voice/background operation on that API level; DO provisioning UX that fits Zara's enrollment flow; OTA pipeline shape if Rung 1 ever leaves the lab.

## 8. Recommendation

1. **Stay on Rung 0 now:** normal app + (emulator-only) Device-Owner experiments. No image fork, no keys, no OTA burden.
2. **Gate Rung 1** on a written capability gap ("SDK+DPM cannot provide X, privileged perm Y on own emulator image can") plus approval for the AOSP sync/build cost and own-key custody.
3. **Never** touch the user's Vivo or any daily-driver device; never pursue root/exploit/unlock paths; keep all image work on emulator targets with the expectation of Play Integrity failure.
4. Revisit the custom system service only after Rung 1 ships on an emulator and a concrete Binder-mediated primitive is proven necessary.
