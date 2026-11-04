# User 8 history snapshot (time-masked)

This directory contains user 8's interaction history as of a specific
test moment. Every event has `source_timestamp < T_test`; future events are
absent. Scoring fields (`update_history`, `confidence_*`, `stereotype_mark`,
`hidden_persona_labels`) are stripped — you are seeing the same view a
recommender would have at inference time. Newer backends no longer carry
`split` / `over_personalization_irrelevant`; the eval harness now picks test
moments dynamically from the full timeline.

## Files (use `Read` to open any of them)

You see only the per-app event timelines (plus the calendar). The user's profile
(demographics, app personas, hidden persona summary, full preference list) is
intentionally NOT included — you must infer who this user is from their actual
interactions across the apps below.

- `instagram.json` — 250 events, time-sorted ascending.
- `facebook.json` — 166 events, time-sorted ascending.
- `threads.json` — 298 events, time-sorted ascending.
- `chatbot.json` — 42 events, time-sorted ascending.
- `ai_studio.json` — 29 events, time-sorted ascending.
- `calendar.json` — a `{"modifications": [...]}` CRUD stream on the user's
  calendar entries (`added` / `updated` / `removed`, each with a `timestamp`).
  Fold the modifications with `timestamp < T_test` to get the current schedule.

Chat-style apps carry a full `conversation` turn list +
`interaction_format.user_message` per event; feed-style apps carry `content`.

## Event schema

Each event is a JSON object with:
- `source_timestamp` (unix epoch), `formatted_timestamp`
- `source_hashtags` — array of `#tags` on the content
- `source_interaction_type` — one of:
  `explicit_positive`, `implicit_positive`, `explicit_negative`,
  `implicit_negative`
- `interaction_format.action_label` — human-readable action (e.g. "Liked",
  "Viewed more than 75% of the reel", "Asked the assistant not to personalize
  recommendations around a preference")
- `content.title`, `content.caption`, `content.overall_description`
- `preferences[]` — inferred `persona_item` + `category` for this engagement
  (may be empty for greyscale negative stubs)

## Reading tip

Events within each app JSON are sorted oldest → newest. Read the full file
once and scan; do not try to enumerate files by `ls` or `find` — those tools
are not available to you. Only the filenames listed above (and this README)
exist in this directory.
