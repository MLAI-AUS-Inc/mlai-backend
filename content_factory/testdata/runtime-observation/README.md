# CF process observation fixture

`cf-processes-v1.json` was produced by the actual
`articles_process_observation.record_process_observation` and
`read_process_observations` functions at Content Factory commit
`69d2d69a1fff5397928fbb4496a8676abf0462db` (draft #1090).

Generation used an empty environment apart from a synthetic settings key,
`APP_ENV=test`, dotenv/bytecode disabled and the explicit source SHA. Only the
Redis connection was replaced with an in-memory boundary; sockets were denied.
It contains one actual writer result for the parent-ready event and the actual
responding-process read projection; generation calls the writer directly rather
than running a Celery signal or worker. These are isolated test observations, not a deployed process,
nightly run, goal enrollment or qualification claim.

The backend test freezes its clock relative to the fixture timestamp and runs
the actual streaming HTTP client with an in-memory Requests response. It does
not invent the CF response shape or contact a service.
