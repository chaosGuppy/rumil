-- Model building (theoretical): a new synthesis page type plus a link type
-- tying a model back to the question it models.
--
-- Design: a MODEL page captures variables, relationships, parameters,
-- predictions, assumptions, and sensitivities as structured Markdown in
-- the page's `content`. Predictions derived from a model are emitted as
-- separate CLAIM pages (or VIEW_ITEM proposals) so that scouts can attack
-- them via the existing scout_c_how_false / scout_c_stress_test_cases paths.
--
-- `page_type` has no CHECK constraint (it's free text), so adding the new
-- value requires no DDL beyond this comment. `link_type` similarly has no
-- CHECK constraint, so MODEL_OF ("model_of") needs no DDL either.
--
-- This migration is intentionally a no-op in the schema; it exists to
-- document the new values in the migration history and to keep the
-- application-layer enum extensions in lockstep with the migration
-- timeline. A placeholder comment on the pages table is added so that the
-- migration produces observable DDL (helps smoke tests catch cases where
-- migrations silently fail to run).
--
-- Forward-compatibility note: the executable flavor of model building is
-- deferred pending a separate sandboxing design. When it lands, any new
-- storage (e.g. run output blobs) will need its own migration; this one
-- deliberately does not anticipate those fields.

COMMENT ON COLUMN pages.page_type IS
  'Page kind (claim, question, judgement, source, wiki, summary, view, view_item, view_meta, model).';
