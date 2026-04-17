-- Migration: add pgvector embedding column to messaging_messages
-- Pass 7 of adversarial improvement loop — 2026-04-16
--
-- Uses vector(384) to match sentence-transformers/all-MiniLM-L6-v2 dimensions
-- and mirror the shape of core.discord_messages.embedding.
--
-- NULLABLE so existing queries are unaffected.
-- ADD COLUMN IF NOT EXISTS is non-blocking on Postgres (no table rewrite for NULLABLE cols).

ALTER TABLE public.messaging_messages
    ADD COLUMN IF NOT EXISTS embedding vector(384);

-- Verify
SELECT column_name, data_type, character_maximum_length
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name   = 'messaging_messages'
  AND column_name  = 'embedding';
