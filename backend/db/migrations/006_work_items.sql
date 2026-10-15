-- DevFoundry: Azure Boards work items from the PRD (Epic > Features > one story per functional requirement).
-- Run once in the Supabase SQL editor after 005_scaffold_commands.sql.
--
-- work_items_proposal             : the reviewed backlog draft ({"epic", "features": [{"key", "title", "stories": [...]}]}).
-- work_items_proposal_prd_version : PRD version the draft was written from.
-- work_items                      : the created work items, by key ({"EPIC" | "F1" | "FR-001": {"id", "url", "type", "hash"}}).
--                                   Creating again after a PRD change only adds new items and updates changed ones.
-- work_items_prd_version          : PRD version the created work items reflect (older = "work items outdated").

alter table public."Project"
    add column if not exists work_items_proposal             jsonb,
    add column if not exists work_items_proposal_prd_version integer not null default 0,
    add column if not exists work_items                      jsonb,
    add column if not exists work_items_prd_version          integer not null default 0;

-- Make PostgREST pick up the new columns immediately.
notify pgrst, 'reload schema';
