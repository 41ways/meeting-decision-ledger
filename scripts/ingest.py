"""가상 프로젝트 데이터를 SQLite(원장·정책)와 Chroma(RAG 4종)에 적재한다.

사용: .venv/bin/python scripts/ingest.py [--domain meeting] [--data data/project]
옛 문서 버전·구 정책·대체된 결정도 일부러 함께 넣는다 — 검색 필터가 이것들을 걸러내는지
확인하고, 필터를 껐을 때 생기는 '옛 결과 간섭'을 평가에서 재현하기 위해서다.
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ledger.rag import LedgerRAG          # noqa: E402
from ledger.records import MeetingRecords  # noqa: E402
from ledger.store import LedgerStore      # noqa: E402


def main() -> None:
    """
    시드 데이터를 SQLite 원장·정책에 넣고 Chroma RAG 4종(R1 회의 원문, R2 문서, R3 결정, R4 정책)을 새로 적재

    Chroma는 reset 후 전부 다시 넣는다. 문서·정책은 current_only=False로 구버전까지 포함한다.
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", default="meeting")
    ap.add_argument("--data", default=str(ROOT / "data" / "project"))
    args = ap.parse_args()
    data = Path(args.data)
    t0 = time.time()

    store = LedgerStore()
    print("SQLite 시드:", store.load_seed(data))
    records = MeetingRecords()
    records.reset()          # 시드 재적재는 처음부터 — 이전 정리 결과(항목·사안·이력)도 비운다
    print("회의 목록:", records.sync_meetings(data), "건")

    rag = LedgerRAG()
    rag.reset()
    n1 = 0
    for f in sorted((data / "r1_meetings").glob("*.json")):    # 회의(M*)·면접(I*) 전사문 — domain으로 구분
        t = json.loads(f.read_text())
        n1 += rag.ingest_transcript(t, t.get("domain", args.domain))
    project_id = "PRJ-OPSAI"         # manifest에 project_id가 없는 문서의 기본 프로젝트
    manifest = json.loads((data / "r2_docs" / "manifest.json").read_text())["documents"]
    n2 = 0
    for d in store.documents(current_only=False):
        # 같은 doc_id라도 버전마다 manifest 항목이 따로 있어 (doc_id, doc_version)으로 찾는다
        m = next((x for x in manifest if x["doc_id"] == d["doc_id"] and x["doc_version"] == d["doc_version"]), {})
        n2 += rag.ingest_baseline_doc((data / "r2_docs" / d["file"]).read_text(), d,
                                      m.get("project_id", project_id), m.get("domain", args.domain))
    n3 = sum(rag.ingest_decision(d, args.domain) for d in store.decisions())
    n4 = sum(rag.ingest_rule(r, args.domain) for r in store.rules(current_only=False))
    print(f"Chroma 적재: R1 {n1} · R2 {n2} · R3 {n3} · R4 {n4} ({time.time() - t0:.1f}s)")
    print("컬렉션 크기:", rag.counts())


if __name__ == "__main__":
    main()
