-- DevFoundry: give the conversations created before sign-in ownership (user_id = 'default_user') to one account,
-- then require every conversation to belong to a real user.
-- Run once, AFTER migration 003 and AFTER that account has signed in to the app at least once
-- (signing in creates its "AppUser" row).

-- >>> Replace the address below with the account's sign-in name (the email shown in the app's top bar).
do $$
declare
    owner_email constant text := 'brijdeep.goyal@maqsoftware.com';
    owner_id text;
    moved integer;
begin
    select user_id into owner_id from public."AppUser"
     where lower(username) = lower(owner_email) or lower(email) = lower(owner_email)
     limit 1;
    if owner_id is null then
        raise exception 'No AppUser row for %. Sign in to the app once with that account, then run this again.', owner_email;
    end if;

    update public."Conversation" set user_id = owner_id where user_id = 'default_user' or user_id is null;
    get diagnostics moved = row_count;
    raise notice 'Assigned % conversation(s) to %', moved, owner_email;
end $$;

-- Every conversation must now belong to a real, known user.
alter table public."Conversation" alter column user_id set not null;

do $$
begin
    if not exists (select 1 from pg_constraint where conname = 'conversation_user_id_fkey_appuser') then
        alter table public."Conversation"
            add constraint conversation_user_id_fkey_appuser
            foreign key (user_id) references public."AppUser"(user_id) on delete restrict;
    end if;
end $$;
