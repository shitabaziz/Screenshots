# MCQ Sheet Maker

A small web app for making JobScholar Bangla GK MCQ sheets. Upload a long
LiveMCQ-style screenshot or PDF, and it returns the `.xlsx` sheet that
`reference/workflow_rules_gk.txt` describes.

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...      # your Claude API key
python app.py
```

Open http://localhost:5000, drop in the file(s), and click **Make sheet**.

## What it does

1. **Loads the upload.** It takes PNG, JPG, WEBP or PDF files. Each PDF page
   is rendered at 150 dpi, and multi-page PDFs are stacked into one tall
   strip. Several files uploaded together become one topic, in upload order.
2. **Slices the image.** The strip is resized to 1000 px wide and cut into
   chunks shaped like phone pages. A 50 000 px strip sent whole would be
   shrunk until the Bengali text can't be read.
3. **Reads the chunks with Claude.** Up to 6 consecutive chunks go in each
   request, and neighbouring requests share one chunk, so a question cut at
   a slice edge is still seen whole. Requests run 4 at a time.
4. **Merges the results.** Overlap copies are removed by source question
   number. When a question appears twice, the complete copy is kept. Genuine
   source duplicates have different numbers, so they stay as separate rows.
5. **Builds the sheet.** It follows the GK workflow rules:
   - Sheet `Questions` with the columns `question | option_a..d | correct_key | explanation`.
   - The green option sets `correct_key` to `a`/`b`/`c`/`d` by its position.
   - Explanations are copied verbatim, with "Live MCQ" sentences removed.
   - Questions the source cancelled are left out.
   - Arial font, wrapped text and the standard column widths.
6. **Validates the sheet.** It runs the usual checks (row count, empty
   fields, bad keys, leftover "Live MCQ" text, duplicate rows) and shows the
   report on the page next to the download link.

### Citation line (তথ্যসূত্র)

- By default each row keeps its own তথ্যসূত্র line from the source. The
  **Citation line** you type is added only to rows that don't have one.
- Tick **Use this line on every row** to replace every row's source line
  with yours. This is the fixed `D` behaviour from the old builder.

### Output name

The sheet is named after the first uploaded file, minus any page suffix:
`jati_shongo_o_jatishongo_bishoyok-1.pdf` → `jati_shongo_o_jatishongo_bishoyok.xlsx`.
You can type a different name in the form.

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Claude API key (required) |
| `CLAUDE_MODEL` | `claude-opus-5-5` | Model used for reading the images |
| `CLAUDE_EFFORT` | `medium` | `low` / `medium` / `high`: higher is more careful, slower and costs more |
| `PARALLEL_REQUESTS` | `4` | How many chunk batches are read at once |
| `PORT` | `5000` | Web server port |

## Files

- `app.py`: the Flask web app (upload, progress polling, download).
- `extractor.py`: image/PDF loading, slicing and Claude extraction, plus merging.
- `sheet_builder.py`: the xlsx builder and validation, ported from `reference/jobscholar_gk_builder.py`.
- `templates/index.html`: the upload page.
- `reference/`: the original builder script and workflow rules.

Jobs are kept in memory, so restarting the server clears finished downloads.
This is fine for a personal tool, but the app has no login. Don't put it on
the public internet as is, because anyone who finds it could spend your API
credits.
