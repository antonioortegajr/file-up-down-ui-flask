# People count report (LM Studio)

- **Generated (UTC):** 2026-04-12T05:12:28.991639+00:00
- **LM Studio base:** `http://127.0.0.1:1234/v1`
- **Model:** `YOUR_MODEL_ID`
- **Uploads folder:** `/Users/antonioortega/GitHub/file-up-down-ui-flask/uploads`

## Summary

- Images processed: **6** (with count: **6**, failed: **0**)
- Same people-count in every successful image: **no** (counts seen: [0, 1, 2]).

## Per image

- **IDX2019Holiday_12.jpg:** **1** people
- **Screenshot_2026-03-15_at_9.17.11_PM.png:** **1** people
- **Screenshot_2026-04-07_at_4.14.14_PM.png:** **0** people
- **Screenshot_2026-04-08_at_9.36.22_PM.png:** **1** people
- **Screenshot_2026-04-08_at_9.52.56_PM.png:** **2** people
- **Screenshot_2026-04-08_at_9.54.50_PM.png:** **2** people

## Photos with no people (count = 0)

- `Screenshot_2026-04-07_at_4.14.14_PM.png`

## Count breakdown (successful images only)

- **0 people:** `Screenshot_2026-04-07_at_4.14.14_PM.png`
- **1 people:** `IDX2019Holiday_12.jpg`, `Screenshot_2026-03-15_at_9.17.11_PM.png`, `Screenshot_2026-04-08_at_9.36.22_PM.png`
- **2 people:** `Screenshot_2026-04-08_at_9.52.56_PM.png`, `Screenshot_2026-04-08_at_9.54.50_PM.png`

## Could not get a count (API or parse errors)

- *(none)*

## What you cannot rely on (limitations)

- Counts are model estimates, not ground truth (no human verification).
- Crowds, occlusion, mirrors, and distant figures often produce wrong totals.
- Screenshots may show people in UI thumbnails, video frames, or photos-within-photos — the model may count or skip those inconsistently.
- If the API or parsing failed for a file, that file has no reliable count in this report.
