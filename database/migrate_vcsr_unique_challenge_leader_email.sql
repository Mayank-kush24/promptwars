-- Virtual challenge submissions: identity is (challenge_id, leader_email), not team name.
-- Removes duplicate rows for the same leader on the same challenge (keeps highest id),
-- then replaces the unique index.

DELETE FROM virtual_challenge_submission_rows v
WHERE v.id IN (
  SELECT id
  FROM (
    SELECT id,
           ROW_NUMBER() OVER (
             PARTITION BY challenge_id, leader_email_normalized
             ORDER BY id DESC
           ) AS rn
    FROM virtual_challenge_submission_rows
  ) x
  WHERE x.rn > 1
);

DROP INDEX IF EXISTS uq_vcsr_challenge_team;

CREATE UNIQUE INDEX IF NOT EXISTS uq_vcsr_challenge_leader_email
  ON virtual_challenge_submission_rows (challenge_id, leader_email_normalized);
