-- DevFoundry: security reviews of pull requests, requested from the chat (FEATURES.md SEC-A8).
-- Run once in the Supabase SQL editor after 006_work_items.sql.
--
-- pr_reviews : the latest review per pull request of this repository, by PR id:
--              {"12": {"pr_id", "title", "iteration", "source_commit", "reviewed_at", "findings": [...],
--                      "counts": {...}, "status": "REVIEWED" | "POSTED", "posted": {"<fingerprint>": <thread id>}}}
--              Posting the findings again never duplicates a comment (keyed by each finding's fingerprint).

alter table public."Repository"
    add column if not exists pr_reviews jsonb;

-- Make PostgREST pick up the new column immediately.
notify pgrst, 'reload schema';
