-- Run this in your Supabase SQL Editor:
-- (Supabase Dashboard -> SQL Editor -> New Query)
--
-- This grants access for the anon and authenticated roles to the public tables,
-- fixing: 42501 "permission denied for table Conversation / Project / Repository"

-- 1. Grant schema usage
GRANT USAGE ON SCHEMA public TO anon, authenticated;

-- 2. Grant privileges on Conversation, Project, and Repository tables
GRANT ALL ON TABLE public."Conversation" TO anon, authenticated;
GRANT ALL ON TABLE public."Project" TO anon, authenticated;
GRANT ALL ON TABLE public."Repository" TO anon, authenticated;

-- 3. Grant privileges on all existing & future tables and sequences in public schema
GRANT ALL ON ALL TABLES IN SCHEMA public TO anon, authenticated;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO anon, authenticated;

-- 4. Enable RLS and add open policies for backend anon access
ALTER TABLE IF EXISTS public."Conversation" ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon full access" ON public."Conversation";
CREATE POLICY "anon full access" ON public."Conversation"
    FOR ALL TO anon, authenticated USING (true) WITH CHECK (true);

ALTER TABLE IF EXISTS public."Project" ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "backend anon full access" ON public."Project";
CREATE POLICY "backend anon full access" ON public."Project"
    FOR ALL TO anon, authenticated USING (true) WITH CHECK (true);

ALTER TABLE IF EXISTS public."Repository" ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "backend anon full access" ON public."Repository";
CREATE POLICY "backend anon full access" ON public."Repository"
    FOR ALL TO anon, authenticated USING (true) WITH CHECK (true);
