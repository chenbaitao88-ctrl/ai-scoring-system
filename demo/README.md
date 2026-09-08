# Synthetic demo dataset

This directory contains a deterministic, fully fictional dataset created only for product demonstrations and regression checks.

## Dataset layers

- `cases.json` defines the 12 stable `DEMO-P-*` and `DEMO-M-*` records, scores, flags, and review states.
- `enrichment.json` adds realistic fictional work titles, five AI process comments, analysis summaries, and defense questions without changing the stable case identifiers.
- `sample-works/` contains three from-scratch fictional work folders used to demonstrate readable source evidence:
  - `DEMO-P-001`: 校园节水小管家
  - `DEMO-P-002`: 古诗词节奏训练营
  - `DEMO-M-001`: 城市无障碍出行助手

Each sample work includes a README, readable Python source, a fictional AI-use record, and a static SVG preview. The initializer packages these sources but never executes them.

## Runtime output

`scripts/init_demo.py` writes or verifies the isolated dataset under `runtime/demo/data/`. The three source folders are deterministically packaged as ZIP files under:

```text
runtime/demo/data/sample-works/
```

Re-running the initializer safely applies the same enrichment to an existing synthetic runtime, verifies the dataset, and leaves an already-correct runtime unchanged.

## Privacy boundary

- All names, schools, work concepts, scores, comments, code, media previews, and AI-use records are synthetic.
- No content is derived from a real student, teacher, school, registration sheet, submitted work, cloud-drive link, private model response, or event dataset.
- The sample Python files are evidence for reading only. They must not be executed by the initializer or acceptance flow.
- Do not replace these fixtures with real project data. Real data requires a separate approved transfer and must stay outside Git.
