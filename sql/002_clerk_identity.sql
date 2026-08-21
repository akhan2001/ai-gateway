-- Link a Clerk sign-in to the workspace it owns.
--
-- Clerk authenticates *humans* into the dashboard. The `txk-` key continues to
-- authenticate *machines* — gateway traffic and the analytics API — and nothing
-- about that changes here. This table's job is only to answer "which workspace
-- does this signed-in person own", so the dashboard can provision one on first
-- visit and then find it again.
--
-- Deliberately NOT storing the key: api_keys keeps a SHA-256 hash and a 12-char
-- prefix, and the plaintext exists only in the response that mints it. Adding a
-- decryptable copy here so the key could be re-displayed later would hand an
-- attacker every live customer credential from one table.

ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS clerk_user_id TEXT;
ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS email TEXT;

-- One workspace per Clerk user. A partial index rather than a plain UNIQUE:
-- every workspace created before Clerk (and any provisioned by an admin) has a
-- NULL clerk_user_id, and a UNIQUE constraint over many NULLs is fine in
-- Postgres but the partial index states the intent and stays cheap.
CREATE UNIQUE INDEX IF NOT EXISTS workspaces_clerk_user_idx
    ON workspaces (clerk_user_id) WHERE clerk_user_id IS NOT NULL;
