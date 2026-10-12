-- DevFoundry: signed-in users, conversation ownership, Conversation -> Project link, database hardening.
-- Run once in the Supabase SQL editor AFTER the backend uses the service-role key (see README / ISSUES.md).
-- Safe to run more than once.

-- 1. Users (Microsoft Entra ID). user_id = immutable Entra object id (oid) from the sign-in token.
create table if not exists public."AppUser" (
    user_id       text primary key,
    tenant_id     text,
    username      text,          -- preferred_username (display only)
    display_name  text,
    email         text,
    created_at    timestamptz not null default now(),
    last_seen_at  timestamptz not null default now()
);

-- 2. Conversation.conv_id must be unique to be referenced (skip if it already is the primary key / unique).
do $$
begin
    if not exists (
        select 1
        from pg_index i
        join pg_attribute a on a.attrelid = i.indrelid and a.attnum = i.indkey[0]
        where i.indrelid = 'public."Conversation"'::regclass and i.indisunique and i.indnatts = 1 and a.attname = 'conv_id'
    ) then
        alter table public."Conversation" add constraint conversation_conv_id_key unique (conv_id);
    end if;
end $$;

-- 3. Project.conv_id must have the same type as Conversation.conv_id (text vs uuid).
do $$
declare
    conv_type text;
    proj_type text;
begin
    select format_type(atttypid, atttypmod) into conv_type from pg_attribute
     where attrelid = 'public."Conversation"'::regclass and attname = 'conv_id';
    select format_type(atttypid, atttypmod) into proj_type from pg_attribute
     where attrelid = 'public."Project"'::regclass and attname = 'conv_id';
    if conv_type <> proj_type then
        execute format('alter table public."Project" alter column conv_id type %s using conv_id::%s', conv_type, conv_type);
    end if;
end $$;

-- 4. Link Project -> Conversation. Deleting a conversation deletes its Project (and, by cascade, Repository) rows.
--    Nothing is deleted in Azure DevOps.
delete from public."Project" p
 where not exists (select 1 from public."Conversation" c where c.conv_id = p.conv_id);

do $$
begin
    if not exists (select 1 from pg_constraint where conname = 'project_conv_id_fkey_conversation') then
        alter table public."Project"
            add constraint project_conv_id_fkey_conversation
            foreign key (conv_id) references public."Conversation"(conv_id) on delete cascade;
    end if;
end $$;

-- 5. The owner is stored once, on the conversation.
alter table public."Project" drop column if exists user_id;

create index if not exists conversation_user_created_idx on public."Conversation"(user_id, created_at desc);

-- 6. Hardening: only the backend (service-role key) can read/write. The service role bypasses row-level security;
--    the anon/authenticated keys get no policies and no privileges on these tables.
alter table public."Conversation" enable row level security;
alter table public."Project"      enable row level security;
alter table public."Repository"   enable row level security;
alter table public."AppUser"      enable row level security;

do $$
declare
    p record;
begin
    for p in
        select schemaname, tablename, policyname from pg_policies
         where schemaname = 'public' and tablename in ('Conversation', 'Project', 'Repository', 'AppUser')
    loop
        execute format('drop policy %I on %I.%I', p.policyname, p.schemaname, p.tablename);
    end loop;
end $$;

revoke all on public."Conversation", public."Project", public."Repository", public."AppUser" from anon, authenticated;
grant select, insert, update, delete on public."Conversation", public."Project", public."Repository", public."AppUser" to service_role;
