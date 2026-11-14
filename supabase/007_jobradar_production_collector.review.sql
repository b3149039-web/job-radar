-- Review first. Apply manually after production migrations 003, 005 and 006.
-- The role starts with NOLOGIN and no password.
begin;

create role jobradar_prod_collector nologin noinherit nosuperuser nocreatedb nocreaterole
  noreplication nobypassrls connection limit 2;
grant connect on database postgres to jobradar_prod_collector;
grant usage on schema public to jobradar_prod_collector;
grant select on public.jobs to jobradar_prod_collector;
grant insert (id, company, title, city, source, url, summary, cohort, recruitment_type,
              employer_type, check_status, qualifications, first_seen),
      update (check_status, qualifications) on public.jobs to jobradar_prod_collector;

create policy jobs_prod_collector_read on public.jobs for select to jobradar_prod_collector using (true);
create policy jobs_prod_collector_insert on public.jobs for insert to jobradar_prod_collector
  with check (url like 'https://%' and first_seen <> '' and summary = ''
    and check_status in ('在招待确认', '官网显示可投递', '已过期', '已结束', '无法访问'));
create policy jobs_prod_collector_update on public.jobs for update to jobradar_prod_collector
  using (true) with check (check_status in ('在招待确认', '官网显示可投递', '已过期', '已结束', '无法访问'));

create function public.jobradar_keep_prod_terminal_status() returns trigger
language plpgsql set search_path = ''
as $$
begin
  if current_user = 'jobradar_prod_collector'
     and old.check_status in ('已过期', '已结束', '无法访问')
     and new.check_status is distinct from old.check_status then
    raise insufficient_privilege using message = 'Collector cannot replace a terminal status';
  end if;
  return new;
end;
$$;
revoke execute on function public.jobradar_keep_prod_terminal_status() from public, anon, authenticated;
create trigger jobs_keep_prod_terminal_status before update of check_status on public.jobs
  for each row execute function public.jobradar_keep_prod_terminal_status();

-- Abort the entire migration if a pre-existing grant (including PUBLIC) would
-- expose private data or permit operations beyond those granted above.
do $$
declare
  table_id regclass;
  collector constant name := 'jobradar_prod_collector';
begin
  foreach table_id in array array['public.jobs'::regclass, 'public.profiles'::regclass,
    'public.job_activity'::regclass, 'public.interviews'::regclass, 'public.manual_jobs'::regclass] loop
    if not (select relrowsecurity from pg_class where oid = table_id) then
      raise exception 'Expected RLS on %', table_id;
    end if;
    if table_id <> 'public.jobs'::regclass and
       (has_any_column_privilege(collector, table_id, 'SELECT,INSERT,UPDATE,REFERENCES')
        or has_table_privilege(collector, table_id, 'DELETE,TRUNCATE,TRIGGER')) then
      raise exception 'Unexpected private table grant on %', table_id;
    end if;
  end loop;
  if has_schema_privilege(collector, 'public', 'CREATE')
     or has_table_privilege(collector, 'public.jobs', 'UPDATE,DELETE,TRUNCATE,TRIGGER,REFERENCES')
     or exists (select from pg_attribute
       where attrelid = 'public.jobs'::regclass and attnum > 0 and not attisdropped
       and attname not in ('check_status', 'qualifications')
       and has_column_privilege(collector, attrelid, attnum, 'UPDATE'))
     or exists (select from pg_proc
       where pronamespace = 'public'::regnamespace and prosecdef
       and has_function_privilege(collector, oid, 'EXECUTE')) then
    raise exception 'Unexpected inherited collector privileges';
  end if;
end;
$$;

commit;
