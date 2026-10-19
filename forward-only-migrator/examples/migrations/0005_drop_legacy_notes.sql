-- migrate: allow-destructive
-- Nothing reads legacy_notes any more (fictional). The directive above is what lets DROP run.
DROP TABLE legacy_notes;
