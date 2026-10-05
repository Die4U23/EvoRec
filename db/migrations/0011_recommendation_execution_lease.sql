-- NULL identifies older admissions: their liveness cannot be inferred from a lock.
-- Only a server holding the request's session advisory lock writes this owner.
ALTER TABLE recommendation_requests ADD COLUMN execution_owner uuid;
