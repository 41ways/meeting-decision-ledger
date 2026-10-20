"""결정 원장 MCP 서버 ("ledger") — 백엔드가 stdio 자식 프로세스로 띄워 재사용한다 (결정 D-002와 같은 구조).

Tool / Resource / Prompt 구분 기준은 3과와 같다: "부를 때마다 결과가 달라지느냐".
- Tool     : 회의마다 다른 원문 검색, 계속 쌓이는 원장 검색, 시점에 따라 달라지는 예외 확인, 원장 쓰기
- Resource : 거의 안 바뀌는 정책 조항, 현행 기준 문서 (URI로 식별)
- Prompt   : 결정 추출·충돌 검토의 표준 절차

approve_decision은 사람(승인 화면)만 부르는 경로다. Agent 그래프는 propose까지만 한다.
"""
import hashlib
import json
import sys
from pathlib import Path

from fastmcp import FastMCP

ROOT = Path(__file__).resolve().parent
# 백엔드가 다른 작업 디렉터리에서 자식 프로세스로 띄워도 ledger 패키지를 찾도록 프로젝트 루트를 경로에 추가
sys.path.insert(0, str(ROOT))

from ledger.rag import LedgerRAG          # noqa: E402
from ledger.resolve import Resolver       # noqa: E402
from ledger.store import LedgerStore      # noqa: E402

# 저장소·검색기는 프로세스당 하나만 만들어 모든 도구가 공유
mcp = FastMCP(name="decision-ledger")
store = LedgerStore()
rag = LedgerRAG()
resolver = Resolver(store, rag)
DATA = ROOT / "data" / "project"


def _slim(rows: list[dict], keep: tuple[str, ...]) -> list[dict]:
    """도구 응답에 필요한 키만 남긴다.

    쓰지 않는 메타데이터를 빼서 LLM에 넘기는 응답 크기를 줄인다. 없는 키는 None으로 채우지 않고 생략.
    Args:
        rows: 검색·조회 결과 목록
        keep: 남길 키
    Returns:
        keep에 있는 키만 담은 dict 목록
    """
    return [{k: r.get(k) for k in keep if k in r} for r in rows]


# ---------------- Tool: R1 회의 원문 ----------------
@mcp.tool()
def search_transcript(query: str, meeting_id: str = "", k: int = 5) -> list[dict]:
    """회의·면접 원문(R1)에서 질의와 관련된 발언을 찾는다.

    원문은 권위가 없고 인용 근거로만 쓴다.
    Args:
        query:      찾을 내용
        meeting_id: 특정 회의로 한정. 비우면 전체
        k:          최대 결과 수
    Returns:
        발언 목록 (meeting_id, date, seq, ts, speaker, role, text, _score)
    """
    rows = rag.search_transcript(query, meeting_id=meeting_id or None, k=k)
    return _slim(rows, ("meeting_id", "date", "seq", "ts", "speaker", "role", "text", "_score"))


@mcp.tool()
def get_segments(meeting_id: str, seq_from: int = 1, seq_to: int = 9999) -> list[dict]:
    """회의 원문을 발언 번호 구간으로 그대로 가져온다 (근거 인용 검증용, 검색 아님).

    원문이 보존 기한 경과로 파기됐으면 빈 목록을 돌려준다.
    Args:
        meeting_id: 회의 ID
        seq_from:   시작 발언 번호 (포함)
        seq_to:     끝 발언 번호 (포함). 기본값은 사실상 끝까지
    Returns:
        발언 목록 (원문 필드 + meeting_id, date)
    """
    # 벡터 DB가 아니라 원문 파일을 직접 읽는다 — 인용이 원문 그대로인지 확인하는 용도라서
    f = DATA / "r1_meetings" / f"{meeting_id}.json"
    if not f.exists():
        return []
    m = json.loads(f.read_text())
    return [dict(s, meeting_id=meeting_id, date=m["date"]) for s in m["segments"] if seq_from <= s["seq"] <= seq_to]


# ---------------- Tool: R3 결정 원장 ----------------
@mcp.tool()
def search_decisions(query: str, include_proposed: bool = False, k: int = 5) -> list[dict]:
    """결정 원장(R3)에서 같은 주제의 결정을 찾는다.

    기본은 유효 결정(승인 + 대체 안 됨)만. 승인 대기는 권위 없음. 다른 프로젝트 결정은 참고용.
    Args:
        query:            찾을 내용
        include_proposed: True면 승인 대기(proposed)도 함께 반환
        k:                최대 결과 수
    Returns:
        결정 목록 (decision_id, project_id, topic, status, active, statement, _score)
    """
    rows = rag.search_decisions(query, k=k, include_proposed=include_proposed)
    return _slim(rows, ("decision_id", "project_id", "topic", "status", "active", "statement", "_score"))


@mcp.tool()
def list_decisions(project_id: str = "PRJ-OPSAI") -> list[dict]:
    """원장 전체 목록 (승인 대기·대체 포함).

    화면의 원장 표와 승인 버튼용.
    Args:
        project_id: 프로젝트로 한정. 비우면 전체 프로젝트
    Returns:
        결정 목록 (decision_id, project_id, topic, status, statement, decided_by, approved_by,
        effective_from, superseded_by, evidence)
    """
    return _slim(store.decisions(project_id=project_id or None),
                 ("decision_id", "project_id", "topic", "status", "statement", "decided_by", "approved_by",
                  "effective_from", "superseded_by", "evidence"))


@mcp.tool()
def get_decision(decision_id: str) -> dict:
    """결정 1건의 전체 기록(이유, 기각한 대안, 근거 인용, 대체 관계)을 원장에서 가져온다.

    Args:
        decision_id: 결정 ID
    Returns:
        결정 기록 전체. 없으면 {"error": "… 없음"}
    """
    return store.get_decision(decision_id) or {"error": f"{decision_id} 없음"}


@mcp.tool()
def propose_decision(statement: str, topic: str, decided_by: str, evidence: list[dict],
                     rationale: str = "", alternatives_rejected: list[str] | None = None,
                     project_id: str = "PRJ-OPSAI") -> dict:
    """추출한 결정을 승인 대기(proposed)로 원장에 올린다.

    승인 전에는 권위가 없다. 같은 근거로 이미 올린 승인 대기 건이 있으면 새로 만들지 않고 그 ID를 돌려준다.
    Args:
        statement:             결정 내용
        topic:                 주제 키
        decided_by:            결정한 사람
        evidence:              [{meeting_id, seq, quote}] — quote는 원문 그대로여야 한다
        rationale:             결정 이유
        alternatives_rejected: 기각한 대안 목록
        project_id:            결정이 속한 프로젝트
    Returns:
        {"proposed": 결정 ID}, 기존 건 재사용이면 "existing": True 추가
    """
    # 중복 판정 키 = 근거 발언 (meeting_id, seq) 집합. quote 문구는 LLM이 조금씩 다르게 뽑을 수 있어 키에서 뺀다
    key = sorted((e["meeting_id"], e.get("seq")) for e in evidence)
    for d in store.decisions(project_id=project_id, topic=topic):     # 같은 근거로 이미 올린 건 재사용 (재실행 중복 방지)
        if d["status"] == "proposed" and sorted((e["meeting_id"], e.get("seq")) for e in d["evidence"]) == key:
            return {"proposed": d["decision_id"], "existing": True}
    did = store.propose({"project_id": project_id, "topic": topic, "statement": statement,
                         "rationale": rationale, "alternatives_rejected": alternatives_rejected or [],
                         "decided_by": decided_by, "evidence": evidence})
    # SQLite가 원천, 벡터 DB에도 바로 반영해 다음 검색에서 승인 대기 건이 보이게 한다
    rag.ingest_decision(store.get_decision(did))
    return {"proposed": did}


@mcp.tool()
def approve_decision(decision_id: str, approved_by: str, effective_from: str = "") -> dict:
    """[사람 전용] 승인 대기 결정을 승인한다.

    같은 주제의 기존 유효 결정은 대체(superseded) 처리된다.
    Args:
        decision_id:    승인할 결정 ID
        approved_by:    승인자
        effective_from: 시행일 (YYYY-MM-DD). 비우면 오늘
    Returns:
        {"approved": 결정 ID, "superseded": 대체된 결정 ID 목록}
    """
    out = store.approve(decision_id, approved_by, effective_from or None)
    # 승인 건과 대체된 건 모두 active 값이 바뀌어 벡터 DB 메타데이터도 다시 적재
    for did in [decision_id, *out["superseded"]]:
        rag.ingest_decision(store.get_decision(did))
    return out


@mcp.tool()
def reject_decision(decision_id: str, rejected_by: str, reason: str = "") -> dict:
    """[사람 전용] 승인 대기 결정을 반려한다.

    원장에는 rejected로 남는다.
    Args:
        decision_id: 반려할 결정 ID
        rejected_by: 반려한 사람
        reason:      반려 사유
    Returns:
        {"rejected": 결정 ID}
    """
    out = store.reject(decision_id, rejected_by, reason)
    # status가 바뀌어 검색 필터(proposed 포함 검색)에서 빠지도록 재적재
    rag.ingest_decision(store.get_decision(decision_id))
    return out


@mcp.tool()
def list_meetings() -> list[dict]:
    """회의·면접 목록 (캘린더용). 원문이 보존 중인 것만.

    Returns:
        회의 목록 (meeting_id, project_id, date, title, attendees, domain, segments=발언 수)
    """
    out = []
    # 파기된 원문은 파일이 없으므로 파일 목록이 곧 보존 중인 회의 목록
    for f in sorted((DATA / "r1_meetings").glob("*.json")):
        m = json.loads(f.read_text())
        # domain 필드가 없는 예전 파일은 회의(meeting)로 본다
        out.append({k: m.get(k) for k in ("meeting_id", "project_id", "date", "title", "attendees")}
                   | {"domain": m.get("domain", "meeting"), "segments": len(m["segments"])})
    return out


@mcp.tool()
def rag_inspect(collection: str, query: str, filtered: bool = True, k: int = 6) -> list[dict]:
    """RAG 저장소 들여다보기 (설정 화면).

    Args:
        collection: r1|r2|r3|r4 (그 밖의 값은 r4로 처리)
        query:      찾을 내용
        filtered:   False면 대체·구버전까지 보여 준다 (r1은 필터 없음)
        k:          최대 결과 수
    Returns:
        컬렉션 공통 주요 키만 남긴 검색 결과 목록
    """
    if collection == "r1":
        rows = rag.search_transcript(query, k=k)
    elif collection == "r2":
        rows = rag.search_baseline(query, k=k, filtered=filtered)
    elif collection == "r3":
        # 들여다보기 화면이라 승인 대기도 함께 보여 준다
        rows = rag.search_decisions(query, k=k, filtered=filtered, include_proposed=True)
    else:
        rows = rag.search_rules(query, k=k, filtered=filtered)
    # 네 컬렉션의 키를 합친 목록 — _slim이 없는 키는 생략하므로 한 목록으로 공용
    keep = ("_score", "meeting_id", "seq", "speaker", "doc_id", "doc_version", "is_current", "section", "decision_id",
            "status", "active", "project_id", "rule_id", "policy_version", "text", "statement", "topic")
    return _slim(rows, keep)


@mcp.tool()
def ledger_fingerprint() -> str:
    """판단에 쓰이는 입력 상태의 지문 (답변 재사용 판단용).

    결정 상태·대체, 현행 정책 버전, 예외, 현행 문서 버전, 회의 원문 파일이 하나라도 바뀌면 값이 달라진다.
    Returns:
        SHA-1 해시 앞 16자리
    """
    # 판정 결과를 바꿀 수 있는 입력만 모은다. 원문은 내용 대신 파일명·수정 시각으로 변경 감지
    parts = [[(d["decision_id"], d["status"], d.get("superseded_by")) for d in store.decisions()],
             store.current_policy_version(),
             [(w["waiver_id"], w["expires_at"]) for w in store.all_waivers()],
             [(d["doc_id"], d["doc_version"], d["updated_at"]) for d in store.documents()],
             sorted((f.name, f.stat().st_mtime) for f in (DATA / "r1_meetings").glob("*.json"))]
    # 캐시 키 용도라 앞 16자리만 사용. default=str은 JSON 기본 직렬화가 안 되는 값 대비
    return hashlib.sha1(json.dumps(parts, ensure_ascii=False, default=str).encode()).hexdigest()[:16]


@mcp.tool()
def rag_counts() -> dict:
    """RAG 저장소별 적재 건수.

    Returns:
        {컬렉션 이름: 건수}
    """
    return rag.counts()


# ---------------- Tool: R2·R4 검색과 해석 ----------------
@mcp.tool()
def search_baseline(query: str, k: int = 4) -> list[dict]:
    """프로젝트 기준 문서(R2) 현행 버전에서 관련 조항을 찾는다. 옛 버전은 검색하지 않는다.

    Args:
        query: 찾을 내용
        k:     최대 결과 수
    Returns:
        조항 목록 (doc_id, doc_version, section, topic, text, _score)
    """
    rows = rag.search_baseline(query, k=k)
    return _slim(rows, ("doc_id", "doc_version", "section", "topic", "text", "_score"))


@mcp.tool()
def search_policies(query: str, k: int = 4) -> list[dict]:
    """사내 정책(R4) 현행 버전에서 해당 행위에 적용되는 규칙을 찾는다.

    구 정책은 판단 근거가 될 수 없다.
    Args:
        query: 찾을 내용 (행위·상황)
        k:     최대 결과 수
    Returns:
        규칙 목록 (rule_id, policy_version, rule_domain, topic, approver_role, text, _score)
    """
    rows = rag.search_rules(query, k=k)
    return _slim(rows, ("rule_id", "policy_version", "rule_domain", "topic", "approver_role", "text", "_score"))


@mcp.tool()
def topic_candidates(text: str, k: int = 3) -> list[dict]:
    """발언·의견이 어느 주제(topic)에 해당하는지 후보를 유사도 순으로 돌려준다.

    최종 선택은 호출 측이 한다.
    Args:
        text: 주제를 찾을 발언·의견
        k:    최대 후보 수
    Returns:
        [{"topic", "score"}] 유사도 내림차순
    """
    return resolver.topic_candidates(text, k)


@mcp.tool()
def resolve_current(text: str, topic: str = "", project_id: str = "") -> dict:
    """주제에 대해 지금 유효한 기준을 우선순위(R4 > R3 유효 결정 > R2 현행 > R1 논의)로 해석해 돌려준다.

    project_id를 주면 그 프로젝트가 판단 기준이 되고 다른 프로젝트 결정은 참고(advisory)로 돌아간다.
    Args:
        text:       질문·발언 원문
        topic:      주제 키. 비우면 유사도로 추정
        project_id: 판단 기준 프로젝트. 비우면 기본 프로젝트
    Returns:
        해석 결과 (topic, authority, current, rules, pending, advisory, baseline, discussion, notes)
    """
    return resolver.resolve(text, topic or None, project_id or None).to_dict()


@mcp.tool()
def check_authority(topic: str, decided_by_role: str, attendee_roles: list[str]) -> dict:
    """결정 권한표(R4)로 이 주제를 이 사람·참석자가 결정할 수 있는지 확인한다.

    Args:
        topic:           주제 키
        decided_by_role: 결정한 사람의 역할
        attendee_roles:  참석자 역할 목록
    Returns:
        {"ok", "rule", "approver", "threshold", "reason"}. 해당 주제 권한 규칙이 없으면 ok=True
    """
    return resolver.check_authority(topic, decided_by_role, attendee_roles)


@mcp.tool()
def check_waiver(rule_id: str, on_date: str, project_id: str = "") -> dict:
    """해당 날짜에 유효한 정책 예외(waiver)를 확인한다.

    예외는 정책을 바꾸지 않고, 범위 판단은 별도로 필요하다.
    Args:
        rule_id:    정책 규칙 ID
        on_date:    기준 날짜 (YYYY-MM-DD)
        project_id: 대상 프로젝트. 비우면 기본 프로젝트
    Returns:
        {"rule_id", "on", "active_waivers", "note"}
    """
    # 범위 판단은 호출 측 몫이라 scope_text는 빈 문자열로 넘긴다
    return resolver.check_waiver(rule_id, "", on_date, project_id or None)


# ---------------- Resource: R4 정책 조항, R2 현행 문서 ----------------
@mcp.resource("policy://{policy_version}/{rule_id}")
def get_policy_rule(policy_version: str, rule_id: str) -> str:
    """정책 조항 원문.

    Args:
        policy_version: 정책 버전. 'current'를 주면 현행 버전
        rule_id:        규칙 ID
    Returns:
        규칙 JSON 문자열. 없으면 "… 없음"
    """
    ver = store.current_policy_version() if policy_version == "current" else policy_version
    r = store.rule(rule_id, ver)
    return json.dumps(r, ensure_ascii=False) if r else f"{rule_id}@{ver} 없음"


@mcp.resource("ledger://topics")
def get_topics() -> str:
    """주제 목록. 결정·기준 문서·정책을 같은 키로 묶는다 (LLM이 이 목록에서 고른다).

    Returns:
        "주제 키: 설명" 줄 목록
    """
    t = json.loads((DATA / "topics.json").read_text())["topics"]
    return "\n".join(f"{k}: {v}" for k, v in t.items())


@mcp.resource("project-doc://{doc_id}/current")
def get_current_doc(doc_id: str) -> str:
    """기준 문서의 현행 버전 전문.

    Args:
        doc_id: 문서 ID
    Returns:
        현행 버전 마크다운 전문. 없으면 "… 현행 문서 없음"
    """
    # documents()는 현행 버전만 돌려주므로 doc_id가 맞는 첫 건이 곧 현행본
    for d in store.documents():
        if d["doc_id"] == doc_id:
            return (DATA / "r2_docs" / d["file"]).read_text()
    return f"{doc_id} 현행 문서 없음"


# ---------------- Prompt: 표준 절차 ----------------
@mcp.prompt()
def extract_decisions_prompt(meeting_id: str) -> str:
    """회의 원문에서 결정·할 일을 근거와 함께 뽑는 표준 절차.

    Args:
        meeting_id: 대상 회의 ID
    Returns:
        LLM에 줄 절차 지시문
    """
    return (
        f"회의 {meeting_id}의 발언을 읽고 다음 절차를 따르라.\n"
        "1) '하겠습니다/하는 걸로/확정' 등 확정 표현이 있는 것만 결정으로 뽑는다. 제안·보류·'다음에 보자'는 결정이 아니다.\n"
        "2) 결정마다 근거 발언의 seq와 원문 그대로의 인용(quote)을 붙인다. 인용을 바꾸거나 요약하지 않는다.\n"
        "3) 할 일은 담당자와 기한을 원문에서 찾는다. 없으면 null로 두고 지어내지 않는다.\n"
        "4) STT 오인식으로 보이는 용어는 용어집 기준 표기로 statement에만 고치고, quote는 원문 그대로 둔다.\n"
        "5) 결정한 사람(decided_by)과 역할을 원문 화자 정보로 적는다."
    )


@mcp.prompt()
def summarize_interview_prompt(meeting_id: str) -> str:
    """면접 전사문 요약의 표준 절차.

    Args:
        meeting_id: 대상 면접 ID
    Returns:
        LLM에 줄 절차 지시문
    """
    return (
        f"면접 {meeting_id}의 발언을 읽고 다음 절차를 따르라.\n"
        "1) 면접관 질문마다 질문 요약과 지원자 답변 요약을 쌍으로 만든다.\n"
        "2) 질문 발언 번호와 답변 발언 번호를 붙인다. 면접관 발언은 답변 근거가 아니다.\n"
        "3) 평가·점수·합격 여부는 쓰지 않는다.\n"
        "4) 지원자가 답변을 거부한 질문은 '답변 거부'로만 적는다."
    )


@mcp.prompt()
def conflict_review_prompt(statement: str) -> str:
    """결정·의견 1건의 충돌 여부를 검토하는 표준 절차.

    Args:
        statement: 검토할 결정·의견 문장
    Returns:
        LLM에 줄 절차 지시문
    """
    return (
        f"검토 대상: {statement}\n"
        "다음 순서로 판단하라.\n"
        "1) 현행 정책(R4)에 걸리는가? 걸리면 유효한 예외(waiver)가 있고 범위가 맞는지 본다.\n"
        "2) 같은 주제의 유효 결정(R3)과 모순되는가? 대체된 결정·승인 대기 결정은 비교 대상이 아니다.\n"
        "3) 현행 기준 문서(R2)와 다른가? 정당한 승인이 있으면 위반이 아니라 '기준 변경(문서 개정 필요)'이다.\n"
        "4) 결정한 사람이 권한표상 그 주제를 결정할 수 있는가?\n"
        "5) 다른 프로젝트의 결정은 참고로만 언급하고 충돌 근거로 삼지 않는다.\n"
        "판단마다 근거(규칙 ID, 결정 ID, 문서 조항)를 붙인다."
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
