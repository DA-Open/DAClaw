Call data.profile for the subscribed path first. It profiles all tables and
returns computed statistics and deterministic quality findings.
`data_ref`, when present, points to the complete JSON result: page through it with
artifacts.read first; never construct a card from a truncated preview. Copy the entire
`profile` object unchanged into the data card, with meaning_source="computed".
Include the producing call's action_id as profile_action_id (returned by the tool).
Every number in profile must come from that call; the verifier replays it.
Only grain, meaning and unit may be inferred. Put them in `inferences`, each as
{"field":"grain|meaning|unit", "value":"...", "meaning_source":"inferred",
"confidence":0.0}. Do not invent statistical claims in inference text.
The JSON card has schema_version="1", subject_ref, data_version (from the trigger),
meaning_source, profile_action_id, profile, and inferences. Save with artifacts.write
as application/json; submit under artifact_refs.data_card. Ask for no external writes.
