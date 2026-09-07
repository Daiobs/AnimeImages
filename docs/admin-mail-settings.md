# Admin-Managed Mail Pilot

AnimeImages is the first mail capability pilot. Human clarified on 2026-09-07
that TopicEye/Rardar has mail code and an Admin entry, but no configured real
mail service. This is source-capability absorption, not account migration.
The previous PR #84 access failures remain historical; Windows access, old
credentials and production SSH are not development prerequisites. Recommend
closing PR #84 without merging its now-superseded preparation instructions;
this feature PR does not modify or close it.

## Source And License

Reference: Brilliant666/TopicEye at
`72bd85b60fd274a3c8b0eeca973f5751384c3b38`, upstream fxbin/TopicEye.
TopicEye credits Copyright 2026 fxbin under Apache-2.0; the license is retained
in [TopicEye-Apache-2.0.txt](licenses/TopicEye-Apache-2.0.txt).

Reviewed source paths:

- `backend/app/services/email/base.py`, `factory.py`, `smtp_provider.py`,
  `brevo_provider.py`;
- `backend/app/services/secret_store.py`;
- `backend/app/api/v1/settings.py`;
- `frontend/src/app/admin/settings/page.tsx`;
- `LICENSE` and README attribution.

The configuration/source selection, conditional form and blank-secret retention
designs were adapted, not claimed as clean-room work. SMTP stays Django's
implementation. The Brevo adapter is rewritten for Django EmailMessage rather
than copying the verification-code method or templates. Fernet uses an
independent explicit AnimeImages key, not TopicEye's hash-derived app secret.
No secret-prefix previews, FastAPI/SQLAlchemy layers or online TopicEye calls
were imported.

## Configure

Open `/admin/mail-settings/` from the Admin operations navigation or Provider
Health page. Active Staff needs `change_mailchannelsetting`; sending a test
also requires `test_mailchannelsetting`. Superusers have both. Ordinary Staff
has neither by default. Configuration and test actions are CSRF-protected POSTs.

Choose the source explicitly:

- Environment (default, including before migration): existing `DJANGO_EMAIL_*`
  values and `PROVIDER_VALIDATION_EMAIL_RECIPIENTS` remain authoritative.
- Database: the saved SMTP/Brevo account and the Asset 3 `email_recipients`
  list are authoritative. No fieldwise fallback to environment credentials.

Saving or changing provider sends nothing. Drafts may be incomplete. SMTP uses
STARTTLS or direct TLS; both preserve certificate verification. Host/port use
mail protocol configuration, not the media URL admission policy. Brevo needs
only its selected fields, not SMTP credentials. Blank secret inputs retain the
old encrypted value; explicit checkboxes clear it. A wrong/missing key cannot
overwrite old ciphertext. Environment credentials are never copied into DB.

Alert recipients belong to ProviderHealthTestSetting and are only consumed in
database mode. The channel does not change the business-notification switch,
daily selection, samples, thresholds, cooldown, recovery or scheduling. Health
reports expose recipient count, not the list or channel secrets.

## Key And Deployment Prerequisites

This PR is not deployed and does not activate production email. At a separately
reviewed deployment, install the declared cryptography dependency (locally
verified with 47.0.0), run migration 0026, and configure one Fernet key through a
restricted environment file as `MAIL_CONFIG_ENCRYPTION_KEY`. Generate it once
with `Fernet.generate_key()` in an approved environment and transfer it without
logs or shell arguments. Back it up separately from the database. Do not
regenerate on startup; losing it makes saved credentials unreadable. No public
default or Django SECRET_KEY fallback exists. Missing encryption setup does
not prevent the unused DB path from remaining off while env mode operates.

All Web and management-command processes must receive the same master key.
They read the saved channel on each use; no process-global mutation or config
cache is needed. One sending operation uses one in-memory snapshot. Configuration
revision/fingerprint changes invalidate the displayed prior test result.

## Test And Delivery Semantics

Enter exactly one test recipient, acknowledge the real-send warning and submit.
Save pending configuration changes first. Tests use the same saved channel and
connection factory as Asset 3, but do not add the test recipient to alert lists
or create Provider Health runs. They do not enable business notifications.

`PROVIDER_VALIDATION_EMAIL_DELIVERY_ENABLED` remains deployment-controlled;
the UI cannot override it, even for a superuser test. Business delivery also
requires the existing database notification switch and a nonempty valid alert
list. Only complete channel configuration can send.

The test action uses POST/redirect/GET, disables repeated clicks, rejects stale
configuration fingerprints and has a database-backed channel-wide 60-second
limit. It saves only timestamp, configuration fingerprint and a fixed safe
result code. Unknown acceptance is not retried automatically.

States distinguish incomplete configuration, environment gate closed, local
simulation, service accepted, failed and unknown. Local-memory/console/dummy/file
backends are simulations, not real delivery. A service-accepted result is not
inbox confirmation. A stalled `SENDING` record is not success.

The [Brevo API](https://developers.brevo.com/reference/send-transac-email) adapter
uses a fixed HTTPS endpoint, no environment proxy inheritance, no redirects,
bounded request/response sizes and timeouts, and a 201 response containing a
nonempty messageId. It supports current plain-text messages only; attachments,
HTML alternatives, CC/BCC, reply-to and extra headers are rejected explicitly.
No request/response body is logged. No automatic HTTP retry occurs, especially
after ambiguous timeouts. SMTP and API exceptions become fixed safe UI statuses.

Asset 3 retains failure/reminder/recovery and history semantics; a failed send
does not roll back stored test results. Its ordinary report no longer contains
recipient addresses. Tests and alerting share transport, not business state.

## Verification And Rollback

Offline tests cover encrypted storage, missing/wrong keys, blank/clear semantics,
source isolation, permissions, CSRF, throttling, stale forms, safe errors, SMTP
parameters, Brevo responses, common test/business transport and migration
upgrade/reverse/reapply. Existing Asset 3 state-machine tests remain intact.
Chrome acceptance uses an isolated SQLite database, temporary key, fake
credentials and a local-memory backend; no provider account or real mail is used.

Switch source back to Environment and save to restore the old env-only path.
This does not migrate secrets or toggle either notification gate. Preserve key
and encrypted backups before removing DB configuration. No source runtime,
production access, deployment, account creation or domain verification is part
of this pilot. `REAL_DELIVERY = NOT_CONFIGURED`.
