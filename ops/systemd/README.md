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

TianTianJieXi upstream parsing is independently limited to one real request by
the shared Redis lease in the application. Increasing web request slots does
not increase that Provider's upstream concurrency.
