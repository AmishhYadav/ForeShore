CREATE EXTENSION IF NOT EXISTS postgis;
CREATE TABLE IF NOT EXISTS features (
    id           BIGSERIAL PRIMARY KEY,
    layer_id     TEXT NOT NULL,
    feature_key  TEXT,
    properties   JSONB NOT NULL DEFAULT '{}'::jsonb,
    geom         geometry(Geometry, 4326) NOT NULL,
    source_id    TEXT NOT NULL,
    acquired_at  TIMESTAMPTZ NOT NULL,
    UNIQUE (layer_id, feature_key)
);
CREATE INDEX IF NOT EXISTS features_geom_idx  ON features USING GIST (geom);
CREATE INDEX IF NOT EXISTS features_layer_idx ON features (layer_id);

CREATE TABLE IF NOT EXISTS traces (
    step_id    TEXT PRIMARY KEY,
    query_id   TEXT NOT NULL,
    parent_id  TEXT,
    agent      TEXT NOT NULL,
    kind       TEXT NOT NULL,
    tool       TEXT,
    payload    JSONB NOT NULL,
    ts         TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS traces_query_idx ON traces (query_id, ts);
