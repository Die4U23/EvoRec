-- Legacy requests remain NULL; only trusted server admission writes model input.
ALTER TABLE recommendation_requests ADD COLUMN model_snapshot jsonb;
ALTER TABLE recommendation_requests ADD CONSTRAINT recommendation_model_snapshot_object
    CHECK (model_snapshot IS NULL OR jsonb_typeof(model_snapshot) = 'object');
