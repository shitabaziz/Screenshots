"""
Turn tall LiveMCQ-style screenshots / PDFs into MCQ rows with Claude vision.

Pipeline:
  1. Load each upload as one or more tall images (PDF pages are rendered).
  2. Resize to a readable width and cut into chunks with a page-like aspect ratio
     (a 1080 x 50000 px strip sent whole would be shrunk until the text is unreadable).
  3. Send consecutive chunks in batches (neighbouring batches share one chunk, so a
     question cut at a batch edge is seen whole by the next batch).
  4. Merge batch results by source question number, preferring complete copies.
"""
import base64
import io
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor

import anthropic
import pymupdf
from PIL import Image

Image.MAX_IMAGE_PIXELS = None

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("CLAUDE_EFFORT", "medium")
TARGET_WIDTH = 1000        # px after resizing
CHUNK_RATIO = 1.3          # chunk height = width * ratio
CHUNKS_PER_BATCH = 6
PARALLEL_REQUESTS = int(os.environ.get("PARALLEL_REQUESTS", "4"))
PDF_DPI = 150

SYSTEM_PROMPT = """You transcribe Bengali multiple-choice questions (MCQs) from screenshots of a LiveMCQ-style exam-prep app into structured data.

The images you receive are consecutive vertical slices of ONE long screenshot, top to bottom. Read them as one continuous page: a question may start in one slice and continue in the next.

Each question block looks like:
- A question number (e.g. "2133.") followed by the question text.
- Four options labelled ক/খ/গ/ঘ (sometimes a/b/c/d). The correct option is highlighted in GREEN.
- A grey "Explanation" box. It often ends with a source line starting with "তথ্যসূত্র" (or "Source").

Rules:
- Copy all text EXACTLY as written (verbatim). Do not translate, paraphrase, correct or summarise anything.
- `number`: the question number as printed, digits only (e.g. "2133"). Empty string if none is visible.
- `question`: the question text WITHOUT the number.
- `options`: the four option texts in display order, WITHOUT the ক/খ/গ/ঘ labels.
- `correct_index`: 0-3 = position of the green-highlighted option (1st=0 ... 4th=3), regardless of labels. -1 if no option is highlighted.
- `explanation`: the explanation box text verbatim, keeping its paragraph breaks, but WITHOUT the trailing তথ্যসূত্র/source line. Empty string if there is no explanation.
- `source_line`: the তথ্যসূত্র/source line exactly as printed (e.g. "তথ্যসূত্র: বাংলাদেশ অর্থনৈতিক সমীক্ষা ২০২৪।"), or empty string.
- `cancelled`: true only if the page explicitly says the question was cancelled (e.g. "অপশনে সঠিক উত্তর না থাকায় প্রশ্নটি বাতিল হয়েছে").
- `complete`: false if any part of the block (question, options or explanation) is cut off by the top edge of the FIRST image or the bottom edge of the LAST image, otherwise true.
- Include every question that is at least partly visible, in top-to-bottom order. Never invent or pad questions.
- Ignore app chrome, ads, buttons and other non-question content."""

SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "number": {"type": "string"},
                    "question": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                    "correct_index": {"type": "integer"},
                    "explanation": {"type": "string"},
                    "source_line": {"type": "string"},
                    "cancelled": {"type": "boolean"},
                    "complete": {"type": "boolean"},
                },
                "required": ["number", "question", "options", "correct_index",
                             "explanation", "source_line", "cancelled", "complete"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["questions"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------- images

def load_images(filename, data):
    """Return the upload as a list of PIL images (one per PDF page)."""
    if filename.lower().endswith(".pdf") or data[:5] == b"%PDF-":
        images = []
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            for page in doc:
                pix = page.get_pixmap(dpi=PDF_DPI)
                images.append(Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB"))
        return images
    return [Image.open(io.BytesIO(data)).convert("RGB")]


def stack(images):
    """Join PDF pages into one tall strip so questions that cross pages stay whole."""
    if len(images) == 1:
        return images[0]
    w = max(im.width for im in images)
    images = [im if im.width == w else im.resize((w, round(im.height * w / im.width))) for im in images]
    out = Image.new("RGB", (w, sum(im.height for im in images)), "white")
    y = 0
    for im in images:
        out.paste(im, (0, y))
        y += im.height
    return out


def make_chunks(img):
    """Resize to TARGET_WIDTH and slice into JPEG chunks (bytes)."""
    if img.width > TARGET_WIDTH:
        img = img.resize((TARGET_WIDTH, round(img.height * TARGET_WIDTH / img.width)), Image.LANCZOS)
    chunk_h = int(img.width * CHUNK_RATIO)
    chunks = []
    for y in range(0, img.height, chunk_h):
        buf = io.BytesIO()
        img.crop((0, y, img.width, min(y + chunk_h, img.height))).save(buf, "JPEG", quality=90)
        chunks.append(buf.getvalue())
    return chunks


def make_batches(chunks):
    """Consecutive groups of chunks; neighbouring groups share one chunk."""
    if len(chunks) <= CHUNKS_PER_BATCH:
        return [chunks]
    step = CHUNKS_PER_BATCH - 1
    batches = []
    for start in range(0, len(chunks) - 1, step):
        batches.append(chunks[start:start + CHUNKS_PER_BATCH])
        if start + CHUNKS_PER_BATCH >= len(chunks):
            break
    return batches


# ---------------------------------------------------------------- Claude

def extract_batch(client, chunks):
    content = []
    for i, jpg in enumerate(chunks, 1):
        content.append({"type": "text", "text": f"Slice {i} of {len(chunks)}:"})
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg",
            "data": base64.standard_b64encode(jpg).decode()}})
    content.append({"type": "text", "text": "Extract every MCQ visible in these slices."})

    with client.beta.messages.stream(
        model=MODEL,
        max_tokens=64000,
        system=SYSTEM_PROMPT,
        thinking={"type": "adaptive"},
        output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": SCHEMA}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": content}],
    ) as stream:
        msg = stream.get_final_message()

    if msg.stop_reason == "refusal":
        raise RuntimeError("Claude declined to process part of this file.")
    if msg.stop_reason == "max_tokens":
        raise RuntimeError("Response was cut off (too much text in one batch).")
    text = "".join(b.text for b in msg.content if b.type == "text")
    return json.loads(text)["questions"]


# ---------------------------------------------------------------- merge

def _norm(s):
    return re.sub(r"\s+", " ", s).strip()


def merge(batch_results):
    """Dedupe overlap copies by question number (or text), keeping the best copy.

    Genuine source duplicates carry different numbers, so they survive."""
    order, best = [], {}
    for questions in batch_results:
        for q in questions:
            key = q["number"] or "text:" + _norm(q["question"])
            if key not in best:
                order.append(key)
                best[key] = q
                continue
            old = best[key]
            if (q["complete"], len(q["explanation"])) > (old["complete"], len(old["explanation"])):
                best[key] = q
    return [best[k] for k in order]


def to_rows(questions, citation="", replace_citation=False):
    """Map extracted questions to builder rows. Returns (rows, notes)."""
    rows, notes = [], []
    for q in questions:
        label = f"Q{q['number']}" if q["number"] else f"“{q['question'][:40]}…”"
        if q["cancelled"]:
            notes.append(f"{label}: cancelled in source — excluded.")
            continue
        if not q["complete"]:
            notes.append(f"{label}: was cut off at the edge of the file — check it.")
        opts = (q["options"] + [""] * 4)[:4]
        if len(q["options"]) != 4:
            notes.append(f"{label}: found {len(q['options'])} options instead of 4.")
        idx = q["correct_index"]
        key = "abcd"[idx] if 0 <= idx <= 3 else ""
        if not key:
            notes.append(f"{label}: no green (correct) option detected.")

        source = citation if (replace_citation or not q["source_line"]) else q["source_line"]
        exp = q["explanation"].strip()
        if source:
            exp = f"{exp}\n\n{source}" if exp else source
        rows.append([q["question"], *opts, key, exp])
    return rows, notes


def number_range(questions):
    nums = [q["number"] for q in questions if q["number"]]
    return f"Q{nums[0]} → Q{nums[-1]}" if nums else ""


def process(files, citation="", replace_citation=False, progress=lambda done, total: None):
    """files: [(filename, bytes)] in upload order. Returns dict with rows/notes/range."""
    client = anthropic.Anthropic()
    jobs = []
    for name, data in files:
        jobs.extend(make_batches(make_chunks(stack(load_images(name, data)))))

    done, lock = 0, threading.Lock()
    progress(done, len(jobs))

    def run(batch):
        nonlocal done
        result = extract_batch(client, batch)
        with lock:
            done += 1
            progress(done, len(jobs))
        return result

    with ThreadPoolExecutor(PARALLEL_REQUESTS) as pool:
        results = list(pool.map(run, jobs))

    questions = merge(results)
    rows, notes = to_rows(questions, citation, replace_citation)
    return {"rows": rows, "notes": notes, "range": number_range(questions)}


def output_name(filename):
    """'jati_shongo_bishoyok-1.pdf' -> 'jati_shongo_bishoyok'."""
    base = os.path.splitext(os.path.basename(filename))[0]
    base = re.sub(r"[-_ ]\d+$", "", base)
    base = re.sub(r"[^\wঀ-৿]+", "_", base).strip("_")
    return base or "questions"
