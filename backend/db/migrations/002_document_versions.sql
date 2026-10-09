-- DevFoundry: document versions, so PRD/TRD updates at any stage can flag what is now outdated.
-- Run once in the Supabase SQL editor after 001_create_project_and_repository.sql.

alter table public."Project"
    add column if not exists prd_version          integer not null default 0,  -- +1 on every PRD save
    add column if not exists trd_version          integer not null default 0,  -- +1 on every TRD save
    add column if not exists trd_prd_version      integer not null default 0,  -- PRD version the current TRD reflects
    add column if not exists proposal_trd_version integer not null default 0;  -- TRD version the project name/description reflects

alter table public."Repository"
    add column if not exists structure_trd_version integer not null default 0, -- TRD version the structure reflects
    add column if not exists pushed_structure      jsonb,                      -- files as last pushed (baseline for follow-up commits)
    add column if not exists pushed_structure_version integer;

-- Existing rows: treat what is stored today as version 1 and consistent with each other.
update public."Project" set prd_version = 1 where prd is not null and prd_version = 0;
update public."Project" set trd_version = 1, trd_prd_version = prd_version where trd is not null and trd_version = 0;
update public."Project" set proposal_trd_version = trd_version where project_name is not null and proposal_trd_version = 0;
update public."Repository" r set structure_trd_version = p.trd_version
    from public."Project" p where r.project_id = p.project_id and r.structure is not null and r.structure_trd_version = 0;
update public."Repository" set pushed_structure = structure, pushed_structure_version = structure_version
    where status = 'PUSHED' and pushed_structure is null;
