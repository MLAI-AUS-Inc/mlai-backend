# Staging account federation

Work in progress; disabled by default. No production deployment is established.

## Cookie cutover

Deploy with `COMMUNITY_CHAT_HOST_ONLY_COOKIES=false`. Before enabling identity
federation, approve a cutover window and inspect real browser cookie behavior.
At the approved UTC cutover, enable host-only cookies for every web process and
run `python manage.py retire_legacy_chat_sessions --before <UTC-cutoff>` to
preview the count, then the same command with `--apply` after checking it.
The command revokes only pre-cutover web sessions; it does not remove accounts,
native sessions or relay membership. Readers reject the old cookie names after
cutover; sign-in and logout expire their old `.mlai.au` domain/path cookies.
Users may need to sign in again. Do not restore legacy cookie acceptance during
rollback. Staging uses independent names and its API's host-only cookies.

No remote CI is used for this work. Tests and rehearsals use local disposable
data only. Production migration and cookie changes require a separate approval.
