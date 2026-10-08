# Privileged Android Application Model — Research

> Scope: what placing an APK in a system privileged location actually grants,
> what it does NOT grant, and what a normally-installed third-party APK can
> never obtain without a system-image change. No device was modified and no
> emulator test was run for this document; everything below is labeled by
> evidence status.
>
> Label convention used throughout:
> - **[FACT]** — well-established AOSP behavior; mechanism named so it can be
>   verified in source/docs.
> - **[INFERENCE]** — reasoned conclusion from FACTs; plausible but not directly
>   verified in this repo session.
> - **[UNKNOWN / NEEDS EXPERIMENT]** — claim that requires an emulator/device
>   test before it can be asserted for this project.

Repo context (read-only inspection, 2026-10-06):
- `android/android/app/src/main/AndroidManifest.xml` declares only
  `RECORD_AUDIO` (dangerous/runtime), `POST_NOTIFICATIONS` (runtime, API 33+),
  `INTERNET` + `ACCESS_NETWORK_STATE` (normal). No `sharedUserId`, no
  `signature`/`privileged` permission requested, no `platform` signing config —
  `android/android/app/build.gradle.kts` signs release with the debug key.
- The `BIND_VOICE_INTERACTION` references in the manifest are
  `android:permission="..."` *guard attributes on our own services* (the OS
  must hold the permission to bind to us), not `<uses-permission>` requests.
  Declaring the guard grants nothing by itself.

---

## 1. What priv-app placement actually grants vs. what people assume

### FACT

1. **Placement defines a permission ceiling, not new capabilities.**
   The PackageManagerService partition scan treats APKs under privileged
   locations (`/system/priv-app`, and on modern devices also
   `/system_ext/priv-app`, `/product/priv-app`, `/vendor/priv-app` depending
   on the device's partition layout) as `ApplicationInfo.isPrivilegedApp()`.
   Mechanism: `PackageManagerService` / `AppDirInstaller` partition scanning
   (`ParsedPackage.isPrivileged`); privileged flag is set from install location,
   not from anything inside the APK. Being privileged is a *necessary
   condition* for being granted permissions whose `protectionLevel` includes
   `privileged` — it is not itself a capability.
2. **Priv-app ≠ system UID, platform signature, or sandbox exit.** Placement
   alone does not change the app's Linux UID assignment (still a per-app UID
   in the app range), does not change its SELinux domain to a system domain
   (still an `untrusted_app*` domain), does not sign it with the platform key,
   and does not remove the application sandbox (each app still gets its own
   UID, data dir, and process boundary). These are four independent axes:
   location / signature / UID / SELinux context.
3. **Partition matters only insofar as the build's allowlist and SELinux
   policy cover it.** `/system/priv-app` is the historical location;
   Project Treble-era builds (Android 8.0+, API 26) added
   `/vendor`, `/product`, `/system_ext` partitions, each scanned for `priv-app`
   subdirectories. Whether `/product/priv-app` works on a given device depends
   on that device's `config`/`fstab`/SELinux file contexts labeling the path
   as a privileged app location — on AOSP builds it does; on a heavily
   customized OEM image a nonstandard path may not be scanned. Always verify
   per-image.
4. **Common over-assumption, corrected:** people expect priv-app status to
   unlock silent installs, background-start freedom, battery-exemption, or
   direct hardware access. None of these follow from placement alone. Each is
   gated by a *specific permission, role, or allowlist entry* (e.g. background
   start rules still apply; Doze/App Standby exemptions need
   `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS` flow or device-owner/profile-owner
   action; silent package install needs `INSTALL_PACKAGES` held with the right
   signature/role, not merely priv-app status).

### INFERENCE

- For this project's goals (assistant entry points, reliable mic/background
  behavior), priv-app placement is best understood as "permission to ask for
  stronger permissions," and every concrete benefit must be traced to one named
  permission + its allowlist entry, not to placement in the abstract.

### UNKNOWN / NEEDS EXPERIMENT

- Which of `/system/priv-app` vs `/product/priv-app` vs `/system_ext/priv-app`
  the target image actually scans, and the SELinux file contexts applied to
  each. Confirm on the exact emulator image (`adb shell ls -ldZ <dir>`,
  `dumpsys package <pkg> | grep -i priv`, `getprop` partition properties).

---

## 2. The privapp-permissions allowlist mechanism (Android 8+ enforcement)

### FACT

1. **Since Android 8.0 (API 26, Treble), privileged apps must be explicitly
   allowlisted for each `signature|privileged` permission, or the grant is
   denied (and the boot may warn/fail).** Mechanism: XML files under
   `/etc/permissions/` (system image), conventionally
   `privapp-permissions-<oem>.xml` / `privapp-permissions-platform.xml`,
   parsed by `DefaultPermissionGrantPolicy` / `PrivAppPermissionsXmlParser`.
   Each entry maps `package="com.example.x"` → named permissions granted as
   privileged. AOSP enforces at boot/install: a priv-app holding an unlisted
   privileged permission logs `Privileged permission ... for package ... not
   in privapp-permissions allowlist` and the permission is **not granted**.
   On userdebug/eng builds this is a warning; on user builds with strict
   enforcement (`ro.control_privapp_permissions=enforce`) an unlisted grant
   can prevent boot / fail verification — behavior tightened across
   Android 8→9 (API 28 made enforcement the default expectation).
2. **The allowlist lives in the read-only system image.** Adding or editing an
   entry requires rebuilding/resigning the system image (or otherwise
   modifying `/system`/`/product` `/etc/permissions`), i.e. platform-key /
   image-build access. It cannot be done by the app itself, by `adb install`,
   or by any runtime API. The file also records the expected signature
   (`system` vs explicit cert) depending on schema.
3. **What happens without the entry (the failure mode people hit):**
   the app installs/runs fine, but `checkSelfPermission()` returns
   `PERMISSION_DENIED` / `PackageManager.checkPermission` denies, and
   guarded APIs throw `SecurityException`. Logcat shows the
   `not in privapp-permissions allowlist` denial. There is no fallback grant —
   the permission silently stays denied. **[FACT for AOSP 8+; exact
   warn-vs-enforce per build type is image-dependent — see version note.]**
4. **Allowlist ≠ auto-grant of dangerous permissions.** Runtime (`dangerous`)
   permissions still need user grant (or default-grant entries where the
   policy supports them, e.g. `DefaultPermissionGrantPolicy` carrier/default
   handlers — narrow, role-specific). The privapp allowlist covers the
   `privileged` protection flag, not user consent.

### INFERENCE

- Any plan of the form "drop our APK in priv-app and declare
  `CAPTURE_AUDIO_HOTWORD` / `MANAGE_VOICE_KEYPHRASES` / `BIND_*` /
  `PACKAGE_USAGE_STATS`-adjacent privileged permissions" fails at step two
  unless the image builder also ships our package name in the permissions XML.
  The APK change and the image change must be coordinated.

### UNKNOWN / NEEDS EXPERIMENT

- The target image's `ro.control_privapp_permissions` value and whether
  missing entries produce log-only vs enforce behavior there.
- The exact allowlist filenames present on the target image
  (`adb shell ls /etc/permissions/privapp*` and
  `/product/etc/permissions/privapp*`).

---

## 3. `signature` vs `signature|privileged` protection levels

### FACT

1. **`signature`**: granted only to apps signed with the *same certificate*
   as the permission's declaring package (usually `android`, i.e. the platform
   key). No placement helps: a differently-signed APK in priv-app is still
   denied. Mechanism: `PackageManager` signature comparison at grant time.
2. **`signature|privileged`** (historically written `signatureOrSystem`,
   renamed `signature|privileged` around API 23+): granted if **either**
   the signature matches **or** the app is a privileged (priv-app) app
   **and** allowlisted per §2. This is the level priv-app placement unlocks —
   and only in combination with the allowlist.
3. **`privileged` alone** (rare): grantable to priv-apps without signature
   match (still needs allowlist on 8+). Seen on a small number of permissions;
   do not assume any given permission uses it — check the permission's
   declared `android:protectionLevel` in `frameworks/base/core/res/AndroidManifest.xml`
   (platform) for the authoritative value.
4. **Normal / dangerous / others are unaffected by priv-app status.**
   `normal` auto-grants to everyone; `dangerous` needs user grant regardless
   of priv-app; `appop`, `role`, `internal|system`, `signature`-gated
   device-admin/device-owner flows each have their own gate that placement
   does not satisfy.

### INFERENCE

- Audit every permission this project might want by reading its declared
  protection level in the platform manifest for the target API level, not by
  blog folklore. The level string is the entire decision procedure.

### UNKNOWN / NEEDS EXPERIMENT

- Per-permission level on the *exact* target API level (levels are
  occasionally reclassified between releases, e.g. permissions moved to
  `signature` or gated behind roles in newer APIs). Diff
  `platform/AndroidManifest.xml` between the build's API level and the dev
  machine's assumption.

---

## 4. Platform signing vs. same-signature requirements

### FACT

1. **Platform signing = signed with the device image's platform key**
   (`build/target/product/security/platform.{pk8,x509.pem}` in AOSP builds).
   It makes the app signature-match permissions declared by `android` at
   `signature` level — the strongest app-accessible tier short of system UID.
2. **Same-signature ≠ platform signature in general.** What matters is
   matching the *declaring* package's cert. For most coveted permissions the
   declarer is `android` (platform), so in practice they coincide — but a
   permission declared by another app (e.g. a GMS or OEM package) requires
   *that* package's cert, which the image builder may not even possess.
3. **Signing with the platform key does not by itself confer UID, SELinux
   domain, or sandbox escape** (see §5–§7). It widens the grantable
   permission set; each permission is still individually granted/checked.
4. **Key custody is the crux:** public AOSP test-keys (`external/seandroid`
   / `build/make/target/product/security/testkey`) are not the shipping
   platform key of any real device. An APK signed with test-keys is
   signature-mismatched on production images. There is no legitimate way to
   "derive" platform signature without the private key. **[FACT, standard
   PKI.]**
5. **Repo status:** this repo signs with the debug key
   (`android/android/app/build.gradle.kts`: release `signingConfig = debug`),
   so today the app satisfies **no** `signature`-level check on any image
   except a local emulator image explicitly built with the debug cert as the
   platform key — which is not the default. **[FACT about this repo; verified
   by file read above.]**

### INFERENCE

- Without custody of (or coordination with the custodian of) the target
  image's platform key, all `signature`-level permissions are off the table,
  regardless of priv-app placement.

### UNKNOWN / NEEDS EXPERIMENT

- Which key the chosen emulator image uses as platform key, and whether a
  locally-built AOSP image (`make` with custom keys) is an acceptable test
  vehicle for this project vs. a production-signed device (answer determines
  whether any signature-level result generalizes).

---

## 5. UID / system-UID access

### FACT

1. **Normal apps (including priv-apps) get a per-app Linux UID**
   (AID_APP range, `u0_aNNN`), giving each its own `/data/data/<pkg>`
   directory and process isolation. Priv-app placement does not change UID
   assignment. Mechanism: `installd` / `PackageManager` UID allocation at
   install.
2. **`android:sharedUserId="android.uid.system"` (requesting the system UID,
   AID_SYSTEM=1000) requires platform signature.** The package manager
   rejects a system-UID request from a differently-signed APK at install
   (`PackageManager.INSTALL_FAILED_SHARED_USER_INCOMPATIBLE`). Even when
   granted, system-UID apps remain subject to SELinux (`system_app` domain,
   not kernel/unrestricted) and to permission checks.
3. **`android.uid.shared` / media / bluetooth / etc. shared UIDs** follow the
   same rule: signature match with the defining package is mandatory; the
   mechanism is identical (shared-user cert check at install/scan).
4. **Running as system UID widens *which* `signature`-level (platform)
   permissions can be held and which system-only Binder calls succeed, but it
   is not root (UID 0) and not a sandbox exit.** System services still enforce
   permission + identity checks per call; SELinux still confines the process.

### INFERENCE

- This project should treat `sharedUserId` system as a heavier, more coupled
  alternative to (not a prerequisite for) priv-app + allowlist, and only
  pursue it if a specific API documents a UID-1000 requirement.

### UNKNOWN / NEEDS EXPERIMENT

- Whether the target image even permits additional system-UID packages
  (some hardened/OEM policies and all API 29+ deprecation pressure around
  `sharedUserId` complicate this; `sharedUserId` is deprecated from API 29
  with behavioral friction increasing afterwards — verify install outcome on
  the exact image rather than assuming).

---

## 6. SELinux per-app domains

### FACT

1. **Every app process runs in a per-app SELinux domain**, typically
   `untrusted_app` / `untrusted_app_27`+ / `untrusted_app_all` variants
   selected by targetSdkVersion (`untrusted_app_<sdk>` per-level domains
   exist so policy can tighten newer apps), plus MLS categories isolating
   apps from each other (`c512,c768` per-app categories). Mechanism:
   `zygote` + `selinux_android_setcontext` / `mac_permissions.xml` seinfo
   mapping at process spawn; `untrusted_app*.te` policy files define allowed
   operations.
2. **Priv-app placement does not move an app out of `untrusted_app*.`**
   Only platform-signed system components run as `system_app` /
   `platform_app` (and even `platform_app` is still a confined app domain;
   only true system services run in their own daemons' domains like
   `system_server`, `audioserver`, `mediaserver`). Domain is derived from
   signature/seinfo + UID class, not from priv-app location.
3. **SELinux denials are independent of the Android permission grant.**
   An app can hold the Java-level permission and still be denied the
   underlying kernel/driver operation by neverallow policy (visible as
   `avc: denied` in dmesg/logcat). Debugging requires both layers:
   `checkSelfPermission` *and* `adb shell dmesg | grep avc`.
4. **`mac_permissions.xml`** (also in the image's `/etc/selinux/`) maps
   signing certs to seinfo labels — another image-side file a third party
   cannot modify, and another reason platform signature (not placement)
   determines domain upgrades.

### INFERENCE

- Direct hardware/driver access ambitions (raw ALSA, `/dev/*`, low-level
  sensors) will hit SELinux policy before they hit any Android API, and
  priv-app status changes nothing at this layer.

### UNKNOWN / NEEDS EXPERIMENT

- The exact domain assigned to a test APK on the target image
  (`adb shell ps -Z | grep <pkg>`) in each of: normal install, priv-app
  placement, platform-signed variants.

---

## 7. Whether a priv-app can escape the sandbox

### FACT (short answer: no, by design)

1. **The application sandbox = UID isolation + SELinux confinement +
   permission-checked Binder to system services.** Priv-app status alters none
   of the three outside the widened grantable-permission set (§3). A priv-app
   is still a sandboxed app process.
2. **System services (`system_server`, `audioserver`, etc.) re-check every
   Binder call** for the caller's UID, package, and held permissions
   (`checkPermission`, `enforceCallingPermission`, AppOps checks). Holding a
   process-local reference to a service handle (even via a hidden API) does
   not bypass the in-service check — the check runs server-side on the
   caller's identity.
3. **No sanctioned path exists from priv-app to root/UID 0, to another app's
   private data, to raw Binder syscalls outside the service interface, or to
   loading code into `system_server`.** Such transitions require a kernel or
   system-service vulnerability (i.e. an exploit),SELinux policy modification
   in the image, or a custom system service added to the image — all outside
   the app model.
4. **Debuggable/eng-only escapes** (`adb shell` = `shell` UID AID_SHELL,
   `adb root`/`adbd` as root on userdebug/eng builds) are properties of the
   *build type*, not of the app. They vanish on production (`user`) builds and
   must never be designed around.

### INFERENCE

- Architecture for this project must assume the sandbox holds: cross-app data
  access, ambient capture without a holding permission + user-visible
  indicator, and silent actuation outside the app's own UID are not available
  to design around obtaining; the design must route privileged operations
  through Core/backend or device-owner flows, as the existing
  `docs/ANDROID_ARCHITECTURE.md` already does (Core dispatches, phone executes
  only allowlisted reads).

### UNKNOWN / NEEDS EXPERIMENT

- None on the principle (settled); per-API unknowns belong in §9 (which
  specific Binder calls succeed with which identity + permission on the
  target image).

---

## 8. Non-SDK (hidden API) greylist/blacklist relevance

### FACT

1. **Since Android 9 (API 28), non-SDK interfaces are restricted**
   (`hidden API blacklist/greylist`, now documented as blocked vs.
   unsupported lists). Mechanism: `hidden_api_policy` enforced by the runtime
   (`art` + `PlatformDex` checks); access to blocked (`blacklist`) APIs
   throws `NoSuchMethodError`/`NoSuchFieldError` or logs and denies
   depending on version and enforcement level. The lists ship inside the
   platform (`hiddenapi-*.txt` / `hiddenapi-flags.csv` in AOSP source,
   baked into the boot image).
2. **Priv-app placement does not exempt an app.** Exemptions that exist are
   narrow: platform-signed apps and (on some versions) apps targeting older
   SDKs get partial greylist access; the blacklist applies broadly. The
   exemption criterion is *signature/targetSdk*, not priv-app location.
   Relevant knobs: `hidden_api_policy` enforcement, `targetSdkVersion`
   (higher target = stricter), and on userdebug builds the policy can be
   relaxed for testing — production `user` builds enforce.
3. **Even successful hidden-API invocation still faces permission + UID +
   SELinux checks (§5–§7).** Hidden APIs are mostly just unexported wrappers
   around the same permission-checked service calls; reaching the method is
   not reaching the capability.
4. **Google has steadily moved interfaces from greylist to blocked across
   API 28→33+.** Any hidden-API dependency must be re-validated per target
   API level; `veridex`/`hiddenapi` list diffs are the verification tool.

### INFERENCE

- Hidden APIs are a compatibility liability, not a privilege escalation path,
  for this project. Prefer public APIs + documented permissions/roles; treat
  any hidden-API use as tech debt requiring per-release re-validation.

### UNKNOWN / NEEDS EXPERIMENT

- For each candidate hidden API: its list status (blocked/unsupported/max-
  target-sdk) on the exact target API level, and runtime behavior on the
  chosen image (user vs userdebug enforcement differs).

---

## 9. What a normally-installed third-party APK can NEVER obtain without a system-image change

**[FACT — each item names the missing image-side artifact]**

| Capability | Why it is impossible from a normal install | Image-side artifact required |
|---|---|---|
| Any `signature`-level permission declared by `android` (e.g. platform-only telephony/media/system knobs) | Signature mismatch; no placement or runtime action changes the signing cert | Re-sign APK with the platform private key (key custody) |
| Any `signature\|privileged` permission (e.g. hotword-adjacent, voice-keyphrase, privileged device-policy/network ops — exact set per API level) as a *granted* permission | Privileged flag comes only from priv-app location; grant additionally needs allowlist entry | Move APK to `priv-app` location **and** add package+permission to `privapp-permissions-*.xml` in the image |
| System UID (`android.uid.system`) or any signature-gated `sharedUserId` | Install-time shared-user cert check fails for mismatched signatures (`INSTALL_FAILED_SHARED_USER_INCOMPATIBLE`) | Platform (or defining-package) signature on the APK |
| `system_app`/`platform_app` (or any non-`untrusted_app`) SELinux domain | Domain derives from signature/seinfo mapping + UID class, not from anything the app declares | Platform signature + matching `mac_permissions.xml` seinfo entry in the image (plus policy defining the domain) |
| Silent grant of privileged permissions without user flow | Grant path is install/boot-time policy evaluation over the image's XML, not a runtime API | Same two artifacts as row 2 |
| Becoming default Assistant / VoiceInteractionService with system privileges | Declaring the service/intent is allowed, but *selection* as assistant is a user/role decision (`RoleManager` `ASSISTANT` role / Settings default assistant) plus, for hotword/unlock-adjacent powers, the privileged permissions above | User selection (not obtainable programmatically by a third party) + image artifacts for any privileged permission the role path requires |
| Background-start / battery-exemption / notification-listener-class powers by fiat | Each has its own gate: role + user consent (`NotificationListenerService` binding requires explicit user enablement in Settings), device-owner/profile-owner (needs provisioning or `dpm` via an authorized provisioner), or explicit user opt-out flows — none granted by install location | User action or authorized provisioner; no pure-APK path |
| Raw hardware/driver access outside the permission model (other UIDs' data, `/dev` nodes, kernel interfaces) | UID + SELinux + service-side checks all deny; no app-declared attribute overrides them | New SELinux policy + possibly new HAL/service in the image (i.e. building a different OS), or an exploit (out of scope for legitimate design) |
| Relaxing hidden-API blacklist for production devices | Enforcement is baked into the boot image; `hidden_api_policy` relaxation is a userdebug/test affordance | Custom/rebuilt image (test only) — no effect shippable to production |

Note on assistant specifics (relevant to this repo's Stage-10 spike):
declaring `VoiceInteractionService` + `ACTION_ASSIST` (as this repo's
manifest does) is something **any** third-party APK may declare **[FACT]**;
being *chosen and empowered* as the assistant (hotword detection while
screen-off, keyphrase enrollment via `MANAGE_VOICE_KEYPHRASES`, always-on
capture via `CAPTURE_AUDIO_HOTWORD`, unlock-adjacent behaviors) requires the
user's default-assistant selection **plus** the privileged/signature
permissions above, i.e. image cooperation **[FACT on the mechanism; exact
permission set is API-level-dependent — verify against the target API's
platform manifest, UNKNOWN until then]**.

### INFERENCE

- The project's honest posture (already present in the manifest comments and
  `ANDROID_ARCHITECTURE.md`: "presence claims nothing — the OS/user decide")
  is the only sustainable one for a normally-installed APK. Privileged powers
  are a distribution/integration negotiation with whoever builds/signs the
  system image, not an implementation task.

### UNKNOWN / NEEDS EXPERIMENT (consolidated test backlog)

1. Target image identity: API level, `user` vs `userdebug`, platform-key
   custody, scanned priv-app partitions + their SELinux contexts.
2. `ro.control_privapp_permissions` value; allowlist filenames present.
3. Per-candidate-permission protection levels on that API level (read from
   that level's platform manifest, not from memory).
4. Domain actually assigned (`ps -Z`) per install variant.
5. Assistant-role end-to-end on that image: user-selectable? which
   privileged permissions actually requested/denied (`dumpsys package`,
   logcat allowlist lines, `avc: denied` audit)?

---

## Version notes (only where confident)

- **API 23 (6.0):** runtime permissions; `signatureOrSystem` terminology
  giving way to `signature|privileged`. **[FACT]**
- **API 26 (8.0, Treble):** privapp-permissions allowlist introduced;
  `/vendor`/`/product` partition awareness begins. **[FACT]**
- **API 28 (9):** hidden-API (greylist/blacklist) enforcement begins;
  privapp enforcement tightened (deny-by-default expectation on compliant
  builds). **[FACT]**
- **API 29 (10):** `sharedUserId` deprecated; scoped storage rollout begins;
  background-start restrictions tighten (continuing in later versions).
  **[FACT on deprecation; details of later tightening are version-fine and
  should be read per-API, not from this summary.]**
- **API 30–34:** background-start, exact-alarm, microphone/camera indicators,
  and role-based (RoleManager) gating expand; several formerly
  permission-only flows gain role + user-consent layers. Precise per-API
  deltas are **UNKNOWN for this project's target until the target API level
  is pinned and its platform manifest + behavior-change docs are read.**

---

*No code changed, no device modified, no downloads performed. Repo files read:
`android/android/app/src/main/AndroidManifest.xml`,
`android/android/app/build.gradle.kts`, `docs/ANDROID_ARCHITECTURE.md`,
plus grep over `android/` for permission/signature/platform markers.*

## 9. Primary-agent verification appendix (2026-10-06, on-device API 36)

- **§6 correction:** priv-app placement DOES change the SELinux domain on
  the measured image — our test APK runs as `u:r:priv_app:s0` (observed
  `ps -Z`), not `untrusted_app*`. Domain assignment comes from
  `seapp_contexts` mapping, so the exact domain remains per-build, but
  the blanket "stays untrusted_app" claim is refuted for this image.
- **§2 enforcement correction:** on the measured `userdebug` API 36
  build, a missing allowlist entry is NOT warning-only — it boot-crashes
  `system_server` (`IllegalStateException … not in privileged permission
  allowlist`, observed in crash buffer, boot loop until fixed). Treat the
  allowlist as load-bearing configuration on any target image.
- **§9 table stands**, with one addition: `GRANT_RUNTIME_PERMISSIONS`
  row should read `signature|installer|verifier` (measured) — priv-app
  cannot hold it on this build.
- Full adjudication + POC evidence:
  `PRIVILEGED_ANDROID_FINAL_REPORT.md`.
