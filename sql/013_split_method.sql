-- How a row's section token counts (system/tool/history/user_tokens) were made.
--   estimate    each section's share of serialized characters, scaled to the billed total
--   calibrated  the same, with per-section weights from scripts/calibrate_anthropic_split.py
--   tokenized   each section run through the model's tokenizer, scaled to the billed total
-- NULL when the row has no sections, and on rows from before this column (all estimates).
-- Every method sums exactly to the provider's billed input total.
--
-- Applied by the gateway at startup (services/db.py). Keep in sync with that list.
ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS split_method TEXT;
