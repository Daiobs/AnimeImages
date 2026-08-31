# AnimeImages Web Service

`animeimages.service` is the repository-controlled candidate for the production
web service. It keeps two Gunicorn worker processes and the existing 60-second
request timeout, while changing the worker class from `sync` to `gthread` with
four threads per worker.

The tracked unit is not installed automatically. A production rollout must
compare it with `/etc/systemd/system/animeimages.service`, validate the staged
unit with `systemd-analyze verify`, and use the normal reviewed maintenance
procedure. Repository changes do not imply that the production unit was
updated.

TianTianJieXi upstream parsing has a separate Redis-backed concurrency counter
with a production intent of two. Increasing web request slots does not alter
that Provider limit; configuration changes must be reviewed independently.

## Daily Provider validation candidates

`animeimages-provider-validation.service` and
`animeimages-provider-validation.timer` are inactive candidate units. Their
presence in the repository does not copy them to `/etc/systemd/system`, run
`daemon-reload`, enable a timer, or activate production scheduling.

The timer proposes one run per day at 03:30 in the server's systemd calendar
timezone, with up to 15 minutes of randomized delay and `Persistent=true`.
The separately authorized production activation must verify the server
timezone before installation. The oneshot service uses the production virtual
environment and environment file, runs only
`manage.py run_daily_provider_validation`, and allows 1100 seconds so the
configured 30-900 second suite budget plus cleanup grace remains bounded.

Before any activation, follow the production checklist in
`docs/admin-provider-health-tests.md`: back up the database, migrate, configure
approved public samples while daily execution remains disabled, validate SMTP
without committing secrets, exercise dry-run and no-email canaries, inspect the
Admin report, run `systemd-analyze verify`, then review and install the units.
