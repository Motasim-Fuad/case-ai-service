"""Run the WHOLE pipeline on one file with no Docker / Redis / Qdrant (all in memory).

    python -m scripts.demo_local samples/sample_insurance_letter.pdf
    python -m scripts.demo_local samples/sample_insurance_letter.pdf --ask "When are the photos due?"
    python -m scripts.demo_local my_file.pdf --live      # real OpenAI (+ Textract for scans) from your .env
    python -m scripts.demo_local my_file.pdf --json      # print the complete result JSON

Without --live it uses MOCK_AI (offline regex heuristics), so scanned files need --live + AWS keys for OCR.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import uuid
from pathlib import Path

from app.core.config import Settings
from app.core.logging import configure_logging
from app.dev_harness import build_inmemory_deps
from app.models.api import ProcessRequest
from app.services.pipeline import run_pipeline


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--live", action="store_true", help="use real OpenAI / Textract (needs keys in .env)")
    ap.add_argument("--ask", help="ask a RAG question after processing")
    ap.add_argument("--json", action="store_true", help="print full result JSON")
    args = ap.parse_args()

    src = Path(args.file)
    if not src.is_file():
        print(f"File not found: {src}")
        return 2
    settings = Settings().model_copy(update={
        "storage_backend": "local", "qdrant_url": ":memory:", "mock_ai": not args.live,
        "log_json": False, "log_level": "WARNING", "environment": "development",
    })
    configure_logging("WARNING", json_logs=False)
    deps, rag = build_inmemory_deps(settings)

    key = f"demo/{src.name}"
    dest = Path(settings.local_storage_dir) / key
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)

    req = ProcessRequest(document_id="demo-doc", case_id="demo-case", s3_key=key)
    job_id = uuid.uuid4().hex
    deps.jobs.create(job_id, req)
    print(f"Processing {src.name}  (mode: {'LIVE OpenAI' if args.live else 'MOCK AI'}) ...")
    try:
        run_pipeline(job_id, deps)
    except Exception as exc:  # noqa: BLE001
        print(f"\nFAILED: {getattr(exc, 'code', type(exc).__name__)}: {exc}")
        return 1

    result = deps.jobs.get_result(job_id)
    assert result is not None
    if args.json:
        print(result.model_dump_json(indent=2))
    else:
        print(f"\nstatus        : {result.processing_status}")
        print(f"document type : {result.document_type}")
        print(f"summary       : {result.summary}")
        cd = result.claim_details
        print(f"claim / policy: {cd.claim_number} / {cd.policy_number}   insurer: {cd.insurer}   date of loss: {cd.date_of_loss}")
        print("\nTIMELINE EVENTS")
        for e in result.events:
            flag = "DEADLINE" if e.is_deadline else e.event_type
            print(f"  {e.event_date or e.date_text:<12} [{flag}] {e.title}   (page {e.evidence.page}, conf {e.confidence:.2f})")
        print("\nAMOUNTS")
        for a in result.amounts:
            print(f"  {a.amount:>10,.2f} {a.currency or '':<4} {a.label}   [{a.amount_type}] (page {a.evidence.page})")
        print("\nPARTIES")
        for p in result.parties:
            print(f"  {p.role:<12} {p.name}")
        print("\nREQUESTS")
        for r in result.requests:
            print(f"  by {r.due_date or '-':<10} {r.description[:90]}")
        print("\nPAGES", [(p.page, p.method) for p in result.pages], "| chunks indexed:", deps.jobs.get(job_id).chunks_indexed)
        for w in result.warnings:
            print("WARNING:", w)
    if args.ask:
        ans = rag.answer("demo-case", args.ask)
        print(f"\nQ: {args.ask}\nA: {ans.answer}")
        for s in ans.sources:
            print(f"   source: doc {s.document_id} page {s.page} - {s.chunk_text_snippet[:100]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
