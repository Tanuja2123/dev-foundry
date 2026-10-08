-- DevFoundry: Project + Repository tables
-- Run once in the Supabase SQL editor (the app's anon key cannot create tables).
-- Naming follows the existing "Conversation" table: PascalCase table names, snake_case columns.

create extension if not exists pgcrypto;

-- One project per conversation: requirements (PRD/TRD), the ADO project proposal and the created ADO project.
create table if not exists public."Project" (
    project_id          uuid primary key default gen_random_uuid(),
    conv_id             text not null unique,          -- "Conversation".conv_id
    user_id             text,
    project_name        text,
    project_description text,
    prd                 jsonb,                         -- Requirements_Agent structured PRD
    trd                 jsonb,                         -- Requirements_Agent structured TRD
    workflow_stage      text not null default 'REQUIREMENTS',
    ado_project_id      text,                          -- real Azure DevOps project id (not the operation id)
    ado_project_url     text,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now()
);

-- Repositories of a project, including the reviewed folder structure that gets pushed.
create table if not exists public."Repository" (
    repo_id             uuid primary key default gen_random_uuid(),
    project_id          uuid not null references public."Project"(project_id) on delete cascade,
    repo_name           text not null,
    status              text not null default 'PROPOSED',   -- PROPOSED | CREATED | STRUCTURE_REVIEW | PUSHED
    structure           jsonb,                              -- [{"path": "/a/b.txt", "content": "..."}], source of truth
    structure_version   integer not null default 0,
    ado_repo_id         text,
    ado_repo_url        text,
    branch              text,
    commit_id           text,
    commit_message      text,
    pushed_at           timestamptz,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),
    unique (project_id, repo_name)
);

create index if not exists repository_project_id_idx on public."Repository"(project_id);

-- The backend uses the anon key (same as for "Conversation"), so allow it full access to these tables.
-- Tighten this together with "Conversation" once the backend uses a service-role key / authenticated users.
alter table public."Project" enable row level security;
alter table public."Repository" enable row level security;

drop policy if exists "backend anon full access" on public."Project";
create policy "backend anon full access" on public."Project"
    for all to anon, authenticated using (true) with check (true);

drop policy if exists "backend anon full access" on public."Repository";
create policy "backend anon full access" on public."Repository"
    for all to anon, authenticated using (true) with check (true);

grant select, insert, update, delete on public."Project" to anon, authenticated;
grant select, insert, update, delete on public."Repository" to anon, authenticated;
