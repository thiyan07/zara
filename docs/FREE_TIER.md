# Free-Tier Policy (binding)

Zara is fully free-tier. Nothing may be added that costs money — no paid
APIs, no subscriptions, no mandatory cloud services, no per-use billing.

Allowed:
- Free and open-source software (MIT/Apache/GPL-compatible), self-hosted.
- Free-of-charge proprietary tools ONLY if an open alternative exists or
  the tool is swappable (e.g. system Chrome; Chromium works too).
- Optional one-time hardware the user may or may not buy (e.g. a $15
  voice satellite board). The software side must be free regardless.
- Oracle Always Free: permitted as an *option*, never a requirement.
  SQLite-first default stays until Postgres is self-hostable for free.

Forbidden:
- Any dependency requiring an API key tied to billing.
- Any feature that only works with a paid provider.
- "Free trial" services as architectural dependencies.
- Telematics/usage-metered SDKs.

Verification before adding: license check + cost check + offline check
(does it work with no network and no account?). Record all three in the
commit or docs.
