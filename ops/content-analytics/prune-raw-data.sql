-- Raw-event retention for the pinned Umami v3.2 PostgreSQL schema.
--
-- Run only through the retention service, which first verifies a dump less than
-- 48 hours old. This transaction obtains an advisory lock, validates the schema
-- it is about to mutate, and fails closed when an expected table/column is absent.

\if :{?retention_days}
\else
  \echo 'retention_days is required'
  \quit 64
\endif

SELECT set_config('mlai.raw_retention_days', :'retention_days', false);

DO $$
DECLARE
  retention_days integer := current_setting('mlai.raw_retention_days')::integer;
  cutoff timestamptz;
  missing_columns text;
  affected bigint;
BEGIN
  IF retention_days < 30 OR retention_days > 3650 THEN
    RAISE EXCEPTION 'retention_days must be between 30 and 3650; got %', retention_days;
  END IF;

  cutoff := clock_timestamp() - make_interval(days => retention_days);
  PERFORM pg_advisory_xact_lock(hashtext('mlai.umami.raw-retention.v3'));

  SELECT string_agg(required.table_name || '.' || required.column_name, ', ' ORDER BY 1)
    INTO missing_columns
  FROM (
    VALUES
      ('website_event', 'event_id'),
      ('website_event', 'session_id'),
      ('website_event', 'visit_id'),
      ('website_event', 'created_at'),
      ('event_data', 'website_event_id'),
      ('event_data', 'created_at'),
      ('session_data', 'session_id'),
      ('session_data', 'created_at'),
      ('revenue', 'event_id'),
      ('revenue', 'session_id'),
      ('revenue', 'created_at'),
      ('session_replay', 'session_id'),
      ('session_replay', 'created_at'),
      ('session_replay_saved', 'created_at'),
      ('heatmap_event', 'session_id'),
      ('heatmap_event', 'created_at'),
      ('session', 'session_id'),
      ('session', 'created_at')
  ) AS required(table_name, column_name)
  WHERE NOT EXISTS (
    SELECT 1
    FROM information_schema.columns actual
    WHERE actual.table_schema = 'public'
      AND actual.table_name = required.table_name
      AND actual.column_name = required.column_name
  );

  IF missing_columns IS NOT NULL THEN
    RAISE EXCEPTION
      'Unsupported Umami schema; refusing retention. Missing: %',
      missing_columns;
  END IF;

  -- Delete children and event-adjacent rows before raw events. Umami v3 uses
  -- Prisma-managed relations, so the order is explicit rather than relying on
  -- database cascades.
  DELETE FROM event_data data
  USING website_event event
  WHERE data.website_event_id = event.event_id
    AND event.created_at < cutoff;
  GET DIAGNOSTICS affected = ROW_COUNT;
  RAISE NOTICE 'deleted % event_data rows attached to expired events', affected;

  DELETE FROM event_data data
  WHERE data.created_at < cutoff
    AND NOT EXISTS (
      SELECT 1 FROM website_event event
      WHERE event.event_id = data.website_event_id
    );
  GET DIAGNOSTICS affected = ROW_COUNT;
  RAISE NOTICE 'deleted % expired orphan event_data rows', affected;

  DELETE FROM revenue revenue_row
  USING website_event event
  WHERE revenue_row.event_id = event.event_id
    AND event.created_at < cutoff;
  GET DIAGNOSTICS affected = ROW_COUNT;
  RAISE NOTICE 'deleted % revenue rows attached to expired events', affected;

  DELETE FROM revenue WHERE created_at < cutoff;
  GET DIAGNOSTICS affected = ROW_COUNT;
  RAISE NOTICE 'deleted % additional expired revenue rows', affected;

  DELETE FROM session_data WHERE created_at < cutoff;
  GET DIAGNOSTICS affected = ROW_COUNT;
  RAISE NOTICE 'deleted % expired session_data rows', affected;

  DELETE FROM session_replay WHERE created_at < cutoff;
  GET DIAGNOSTICS affected = ROW_COUNT;
  RAISE NOTICE 'deleted % expired session_replay rows', affected;

  DELETE FROM session_replay_saved WHERE created_at < cutoff;
  GET DIAGNOSTICS affected = ROW_COUNT;
  RAISE NOTICE 'deleted % expired session_replay_saved rows', affected;

  DELETE FROM heatmap_event WHERE created_at < cutoff;
  GET DIAGNOSTICS affected = ROW_COUNT;
  RAISE NOTICE 'deleted % expired heatmap_event rows', affected;

  DELETE FROM website_event WHERE created_at < cutoff;
  GET DIAGNOSTICS affected = ROW_COUNT;
  RAISE NOTICE 'deleted % expired website_event rows', affected;

  -- A session may be old while a related row is still inside the window. Keep
  -- it until every known v3 raw-data reference is gone.
  DELETE FROM "session" session_row
  WHERE session_row.created_at < cutoff
    AND NOT EXISTS (
      SELECT 1 FROM website_event event
      WHERE event.session_id = session_row.session_id
    )
    AND NOT EXISTS (
      SELECT 1 FROM session_data data
      WHERE data.session_id = session_row.session_id
    )
    AND NOT EXISTS (
      SELECT 1 FROM revenue revenue_row
      WHERE revenue_row.session_id = session_row.session_id
    )
    AND NOT EXISTS (
      SELECT 1 FROM session_replay replay
      WHERE replay.session_id = session_row.session_id
    )
    AND NOT EXISTS (
      SELECT 1 FROM heatmap_event heatmap
      WHERE heatmap.session_id = session_row.session_id
    );
  GET DIAGNOSTICS affected = ROW_COUNT;
  RAISE NOTICE 'deleted % unreferenced expired session rows', affected;
END
$$;

ANALYZE website_event;
ANALYZE event_data;
ANALYZE session_data;
ANALYZE revenue;
ANALYZE session_replay;
ANALYZE heatmap_event;
ANALYZE "session";
