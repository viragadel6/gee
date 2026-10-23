ALTER TABLE public.chat_jobs
  ADD COLUMN IF NOT EXISTS attempts integer NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS next_attempt_at timestamptz,
  ADD COLUMN IF NOT EXISTS last_error text,
  ADD COLUMN IF NOT EXISTS progress_at timestamptz,
  ADD COLUMN IF NOT EXISTS checkpoint jsonb;

CREATE INDEX IF NOT EXISTS chat_jobs_recovery_idx
  ON public.chat_jobs (status, next_attempt_at, created_at)
  WHERE status IN ('queued', 'running');

CREATE INDEX IF NOT EXISTS chat_jobs_lease_idx
  ON public.chat_jobs (lease_expires_at)
  WHERE status = 'running';

CREATE TABLE IF NOT EXISTS public.agent_supervisor_leases (
  name text PRIMARY KEY,
  owner_id text NOT NULL,
  acquired_at timestamptz NOT NULL DEFAULT now(),
  renewed_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  sweep_count bigint NOT NULL DEFAULT 0,
  healed_count bigint NOT NULL DEFAULT 0,
  state jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS public.agent_recovery_log (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  job_id uuid,
  kind text NOT NULL,
  owner_id text NOT NULL DEFAULT '',
  detail jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS agent_recovery_log_created_idx
  ON public.agent_recovery_log (created_at DESC);

CREATE INDEX IF NOT EXISTS agent_recovery_log_job_idx
  ON public.agent_recovery_log (job_id, id DESC);

ALTER TABLE public.agent_supervisor_leases ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agent_recovery_log ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE public.agent_supervisor_leases FROM PUBLIC, anon, authenticated;
REVOKE ALL ON TABLE public.agent_recovery_log FROM PUBLIC, anon, authenticated;

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.agent_supervisor_leases TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.agent_recovery_log TO service_role;

CREATE OR REPLACE FUNCTION public.claim_supervisor_lease(
  _name text,
  _owner_id text,
  _lease_ms integer,
  _state jsonb
)
RETURNS TABLE(owner_id text, acquired boolean)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, extensions
AS $$
DECLARE
  current_row public.agent_supervisor_leases%ROWTYPE;
  now_ts timestamptz := now();
  expires_ts timestamptz := now_ts + make_interval(msecs => greatest(_lease_ms, 1000));
BEGIN
  SELECT * INTO current_row
  FROM public.agent_supervisor_leases
  WHERE name = _name
  FOR UPDATE;

  IF NOT FOUND THEN
    INSERT INTO public.agent_supervisor_leases (name, owner_id, acquired_at, renewed_at, expires_at, state)
    VALUES (_name, _owner_id, now_ts, now_ts, expires_ts, coalesce(_state, '{}'::jsonb));
    RETURN QUERY SELECT _owner_id AS owner_id, true AS acquired;
    RETURN;
  END IF;

  IF current_row.owner_id = _owner_id OR current_row.expires_at <= now_ts THEN
    UPDATE public.agent_supervisor_leases
    SET owner_id = _owner_id,
        acquired_at = CASE WHEN current_row.owner_id = _owner_id THEN current_row.acquired_at ELSE now_ts END,
        renewed_at = now_ts,
        expires_at = expires_ts,
        state = coalesce(_state, state)
    WHERE name = _name;
    RETURN QUERY SELECT _owner_id AS owner_id, true AS acquired;
    RETURN;
  END IF;

  RETURN QUERY SELECT current_row.owner_id AS owner_id, false AS acquired;
END;
$$;

CREATE OR REPLACE FUNCTION public.release_supervisor_lease(_name text, _owner_id text)
RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, extensions
AS $$
  WITH released AS (
    DELETE FROM public.agent_supervisor_leases
    WHERE name = _name AND owner_id = _owner_id
    RETURNING 1
  )
  SELECT exists(SELECT 1 FROM released);
$$;

CREATE OR REPLACE FUNCTION public.claim_chat_job_retry(
  _job_id uuid,
  _max_attempts integer,
  _retry_base_ms integer,
  _retry_max_ms integer,
  _error text
)
RETURNS TABLE(status text, attempts integer, retry boolean)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, extensions
AS $$
DECLARE
  job_row public.chat_jobs%ROWTYPE;
  next_attempts integer;
  delay_ms integer;
  now_ts timestamptz := now();
BEGIN
  SELECT * INTO job_row
  FROM public.chat_jobs
  WHERE id = _job_id
  FOR UPDATE;

  IF NOT FOUND THEN
    RETURN QUERY SELECT 'missing'::text, 0, false;
    RETURN;
  END IF;

  IF job_row.status IN ('completed', 'cancelled', 'failed') THEN
    RETURN QUERY SELECT job_row.status, coalesce(job_row.attempts, 0), false;
    RETURN;
  END IF;

  next_attempts := coalesce(job_row.attempts, 0) + 1;

  IF job_row.stop_requested OR next_attempts > greatest(_max_attempts, 1) THEN
    UPDATE public.chat_jobs
    SET status = CASE WHEN job_row.stop_requested THEN 'cancelled' ELSE 'failed' END,
        attempts = next_attempts,
        last_error = coalesce(_error, job_row.last_error),
        lease_expires_at = NULL,
        finished_at = now_ts,
        updated_at = now_ts
    WHERE id = _job_id;
    RETURN QUERY
      SELECT (CASE WHEN job_row.stop_requested THEN 'cancelled' ELSE 'failed' END)::text,
             next_attempts,
             false;
    RETURN;
  END IF;

  delay_ms := least(
    greatest(_retry_max_ms, _retry_base_ms),
    _retry_base_ms * power(2::numeric, least(next_attempts - 1, 16))
  );

  UPDATE public.chat_jobs
  SET status = 'queued',
      attempts = next_attempts,
      last_error = coalesce(_error, job_row.last_error),
      lease_expires_at = NULL,
      next_attempt_at = now_ts + make_interval(msecs => delay_ms),
      updated_at = now_ts
  WHERE id = _job_id;

  RETURN QUERY SELECT 'queued'::text, next_attempts, true;
END;
$$;

REVOKE ALL ON FUNCTION public.claim_supervisor_lease(text, text, integer, jsonb) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.release_supervisor_lease(text, text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.claim_chat_job_retry(uuid, integer, integer, integer, text) FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION public.claim_supervisor_lease(text, text, integer, jsonb) TO service_role;
GRANT EXECUTE ON FUNCTION public.release_supervisor_lease(text, text) TO service_role;
GRANT EXECUTE ON FUNCTION public.claim_chat_job_retry(uuid, integer, integer, integer, text) TO service_role;
