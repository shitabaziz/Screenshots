"""
Tiny web app: upload long LiveMCQ screenshots / PDFs, get the MCQ xlsx sheet.

Run:  ANTHROPIC_API_KEY=... python app.py   then open http://localhost:5000
"""
import io
import os
import threading
import traceback
import uuid

from flask import Flask, abort, jsonify, render_template, request, send_file

import extractor
from sheet_builder import build_bytes, validate_bytes

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB per upload

ALLOWED = (".png", ".jpg", ".jpeg", ".webp", ".pdf")
JOBS = {}  # job_id -> dict (in memory; fine for a single-user tool)


def run_job(job_id, files, citation, replace_citation):
    job = JOBS[job_id]

    def progress(done, total):
        job.update(done=done, total=total)

    try:
        result = extractor.process(files, citation, replace_citation, progress)
        if not result["rows"]:
            raise RuntimeError("No questions were found in the upload.")
        data = build_bytes(result["rows"])
        job.update(
            status="done",
            xlsx=data,
            report=validate_bytes(data),
            notes=result["notes"],
            range=result["range"],
            count=len(result["rows"]),
        )
    except Exception as e:  # report any failure to the page
        traceback.print_exc()
        job.update(status="error", error=str(e))


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/upload")
def upload():
    uploads = [f for f in request.files.getlist("files") if f.filename]
    if not uploads:
        return jsonify(error="Choose at least one screenshot or PDF."), 400
    bad = [f.filename for f in uploads if not f.filename.lower().endswith(ALLOWED)]
    if bad:
        return jsonify(error=f"Unsupported file type: {', '.join(bad)}"), 400

    files = [(f.filename, f.read()) for f in uploads]
    name = request.form.get("name", "").strip() or uploads[0].filename
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"status": "running", "done": 0, "total": 0,
                    "filename": extractor.output_name(name) + ".xlsx"}
    threading.Thread(
        target=run_job,
        args=(job_id, files, request.form.get("citation", "").strip(),
              request.form.get("replace_citation") == "on"),
        daemon=True,
    ).start()
    return jsonify(job=job_id)


@app.get("/status/<job_id>")
def status(job_id):
    job = JOBS.get(job_id) or abort(404)
    return jsonify({k: v for k, v in job.items() if k != "xlsx"})


@app.get("/download/<job_id>")
def download(job_id):
    job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        abort(404)
    return send_file(io.BytesIO(job["xlsx"]), as_attachment=True,
                     download_name=job["filename"],
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
