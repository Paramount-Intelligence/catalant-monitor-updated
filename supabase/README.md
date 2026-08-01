# Apply the project-monitor schema to a linked Supabase project:
#
#   supabase login
#   supabase link --project-ref <SUPABASE_PROJECT_REF>
#   supabase db push
#
# Or with a direct DB URL (never commit the URL):
#
#   psql "$SUPABASE_DB_URL" -f supabase/migrations/20260331120000_create_project_monitor_schema.sql
#
# Verify:
#
#   python monitor.py --test-supabase
