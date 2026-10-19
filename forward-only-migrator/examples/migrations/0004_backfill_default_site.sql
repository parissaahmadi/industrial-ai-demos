-- Data fix as its own migration: small, reviewable, recorded.
UPDATE assets SET site = 'unassigned' WHERE site IS NULL;
