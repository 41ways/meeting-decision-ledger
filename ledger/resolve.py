"""우선순위 해석 — "이 주제에 대해 지금 유효한 기준이 무엇인가" (docs/design.md §3).

    R4 현행 정책  (제약: 아래 어떤 결정도 어기면 무효, 유효한 예외가 있으면 표시와 함께 허용)
      > R3 유효 결정 (같은 프로젝트, approved, 대체 안 됨)
      > R2 현행 기준 문서
      > R1 원문 (권위 없음 — "논의는 있었지만 미결정" 안내용)

다른 프로젝트의 결정은 advisory(참고)로만 돌려준다. 승인 대기(proposed)는 pending으로 돌려주고 권위가 없다.
여기서는 LLM을 쓰지 않는다 — 검증 가능한 판단은 코드가 내린다(권위 게이트와 같은 원칙).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .rag import LedgerRAG
from .store import LedgerStore

# 역할 → 결정 권한 매핑 (R4 권한표의 approver_role 문자열과 대조)
# 권한표 표기("보안 담당", "PM / 본부장")가 화자 역할 표기와 달라 별칭으로 맞춘다
ROLE_ALIASES = {
    "PM": ["PM"], "PL": ["PL"], "보안": ["보안 담당"], "본부장": ["본부장", "PM / 본부장"],
}


@dataclass
class Resolution:
    """
    한 주제에 대한 우선순위 해석 결과

    Attributes:
        topic:      해석한 주제 키
        authority:  현재 기준의 출처 (policy | decision | baseline | discussion | none)
        current:    권위 있는 현재 기준 (결정 또는 기준 문서 조항)
        rules:      관련 현행 정책
        pending:    승인 대기 결정 (권위 없음)
        advisory:   다른 프로젝트 결정 (참고)
        baseline:   현행 기준 문서 조항
        discussion: 원문 논의 (결정 없음일 때)
        notes:      사용자에게 보여 줄 안내 문구
    """
    topic: str
    authority: str                       # policy | decision | baseline | discussion | none
    current: dict | None = None          # 권위 있는 현재 기준 (결정 또는 기준 문서 조항)
    rules: list[dict] = field(default_factory=list)       # 관련 현행 정책
    pending: list[dict] = field(default_factory=list)     # 승인 대기 결정
    advisory: list[dict] = field(default_factory=list)    # 다른 프로젝트 결정 (참고)
    baseline: list[dict] = field(default_factory=list)    # 현행 기준 문서 조항
    discussion: list[dict] = field(default_factory=list)  # 원문 논의 (결정 없음일 때)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """
        MCP 응답용 dict 변환
        Returns:
            필드 이름을 키로 하는 얕은 복사본
        """
        return self.__dict__.copy()


class Resolver:
    """
    R1~R4를 우선순위대로 조합해 "지금 유효한 기준"을 판정하는 해석기

    후보 찾기는 벡터 검색(LedgerRAG), 유효성 판정은 SQLite 원장(LedgerStore) 기준.
    """

    def __init__(self, store: LedgerStore | None = None, rag: LedgerRAG | None = None,
                 project_id: str = "PRJ-OPSAI"):
        """
        해석기 생성
        Args:
            store:      SQLite 원장. None이면 기본 경로로 생성
            rag:        벡터 검색기. None이면 기본 경로로 생성
            project_id: 호출 측이 지정하지 않을 때 쓰는 기준 프로젝트
        """
        self.store = store or LedgerStore()
        self.rag = rag or LedgerRAG()
        self.project_id = project_id

    def topic_candidates(self, text: str, k: int = 3) -> list[dict]:
        """
        주제 후보를 유사도 순으로 반환

        임베딩 점수만으로는 오분류가 생겨서
        (예: "Redis 캐시" 질문이 rationale에 Redis가 있는 session.store로 붙음)
        최종 선택은 그래프의 LLM 노드가 후보 + '새 주제' 중에서 고른다.
        Args:
            text: 주제를 찾을 발언·질문
            k:    최대 후보 수
        Returns:
            [{"topic": 주제 키, "score": 최고 유사도}] 유사도 내림차순
        """
        # 결정은 filtered=False — 대체된 결정의 주제도 같은 주제 키라 후보로 살린다
        cands = self.rag.search_decisions(text, k=4, filtered=False) + self.rag.search_baseline(text, k=4)
        best: dict[str, float] = {}
        for c in cands:
            t = c.get("topic")
            # topic 없는 조항과 용어집 행(glossary)은 실제 주제가 아니라 제외
            if t and t != "glossary":
                # 같은 주제가 여러 번 걸리면 가장 높은 점수로 대표
                best[t] = max(best.get(t, 0.0), c["_score"])
        return [{"topic": t, "score": s} for t, s in sorted(best.items(), key=lambda x: -x[1])[:k]]

    def topic_of(self, text: str) -> str:
        """
        LLM 없이 쓸 때의 기본 주제: 가장 유사한 후보 (평가의 'RAG만' 기준선)
        Args:
            text: 주제를 찾을 발언·질문
        Returns:
            주제 키. 후보가 없으면 빈 문자열
        """
        cands = self.topic_candidates(text, k=1)
        return cands[0]["topic"] if cands else ""

    def resolve(self, text: str, topic: str | None = None, project_id: str | None = None) -> Resolution:
        """
        주제에 대해 지금 유효한 기준을 우선순위(R4 > R3 > R2 > R1)로 해석

        다른 프로젝트 결정은 참고(advisory)로만 담는다.
        Args:
            text:       질문·발언 원문 (검색 질의로 사용)
            topic:      주제 키. None이면 topic_of로 추정
            project_id: 판단 기준 프로젝트. None이면 기본 프로젝트
        Returns:
            Resolution (authority, current, notes 등)
        """
        pid = project_id or self.project_id
        topic = topic or self.topic_of(text)
        res = Resolution(topic=topic, authority="none")

        # R4: 현행 정책만 (구 정책은 판단에 쓰지 않음)
        # 0.45는 무관한 규칙을 걸러 내는 경험적 임계값 (주제 일치 대신 유사도로만 거른다)
        res.rules = [r for r in self.rag.search_rules(text, k=3) if r["_score"] >= 0.45]

        # R3: 같은 topic의 결정 — 원천은 SQLite (벡터 DB는 후보 찾기용)
        # 주제를 못 정하면 결정을 붙이지 않는다 (엉뚱한 결정을 권위로 내세우지 않기 위해)
        same = self.store.decisions(project_id=pid, topic=topic) if topic else []
        active = [d for d in same if d["is_active"]]
        res.pending = [d for d in same if d["status"] == "proposed"]
        # 다른 프로젝트의 유효 결정은 권위 없이 참고로만
        others = [d for d in self.store.decisions(topic=topic)
                  if d["project_id"] != pid and d["is_active"]] if topic else []
        res.advisory = others

        # R2: 현행 버전 조항
        # 같은 주제 조항은 점수와 상관없이 포함, 주제가 달라도 0.6 이상이면 관련 조항으로 포함 (경험적 임계값)
        res.baseline = [b for b in self.rag.search_baseline(text, project_id=pid, k=3)
                        if b.get("topic") == topic or b["_score"] >= 0.6]

        # 우선순위 판정: 유효 결정 > 현행 기준 문서 > 원문 논의
        if active:
            # 같은 topic에 유효 결정이 여럿이면 가장 최근 시행분
            res.current = max(active, key=lambda d: d.get("effective_from") or "")
            res.authority = "decision"
            # 결정이 문서보다 나중이면 문서가 아직 반영 못한 것 — 충돌이 아니라 문서 갱신 안내
            # (날짜는 ISO 문자열이라 문자열 비교로 선후 판단)
            updated = {(d["doc_id"], d["doc_version"]): d["updated_at"] for d in self.store.documents()}
            eff = res.current.get("effective_from") or ""
            for b in res.baseline:
                if b.get("topic") == topic and updated.get((b["doc_id"], b["doc_version"]), "") < eff:
                    res.notes.append(f"기준 문서 {b['doc_id']} {b['doc_version']} '{b['section']}'가 결정 "
                                     f"{res.current['decision_id']}({eff}) 이전 개정본 — 문서 갱신 필요 (충돌 아님)")
        elif res.baseline:
            res.current = res.baseline[0]
            res.authority = "baseline"
        else:
            # 권위 있는 기준이 없을 때만 원문을 찾는다 — 원문은 "미결정" 안내용
            res.discussion = self.rag.search_transcript(text, project_id=pid, k=3)
            res.authority = "discussion" if res.discussion else "none"
            res.notes.append("유효한 결정·기준 문서 없음 — 원문 논의만 있음 (미결정)")
        if res.pending:
            res.notes.append("승인 대기 결정 있음: " + ", ".join(p["decision_id"] for p in res.pending)
                             + " (권위 없음)")
        if others:
            res.notes.append("다른 프로젝트 결정은 참고용: " + ", ".join(o["decision_id"] for o in others))
        return res

    # ---------- 결정적 검사 (권위 게이트) ----------
    def check_authority(self, topic: str, decided_by_role: str, attendee_roles: list[str]) -> dict:
        """
        결정 권한 확인

        권한표(R4 AUTH-*)에서 이 주제의 승인자를 찾아, 결정한 사람이나 참석자가 권한을 가졌는지 본다.
        Args:
            topic:           주제 키
            decided_by_role: 결정한 사람의 역할
            attendee_roles:  참석자 역할 목록
        Returns:
            {"ok", "rule", "approver", "threshold", "reason"}. 권한 규칙이 없으면 ok=True
        """
        # 판단은 SQLite 현행 정책 기준 (벡터 검색 결과에 기대지 않음)
        rules = [r for r in self.store.rules() if r["domain"] == "authority" and r["topic"] == topic]
        if not rules:
            return {"ok": True, "rule": None, "reason": "해당 주제의 권한 규칙 없음"}
        r = rules[0]
        # "PM / 본부장"처럼 '/'로 나열된 승인자 중 한 명이면 된다
        approvers = [a.strip() for a in r["approver_role"].split("/")]
        # 결정자 본인이 아니어도 승인 권한자가 회의에 참석했으면 권한 있는 결정으로 본다
        holders = {decided_by_role, *attendee_roles}
        # 별칭이 승인자 문자열에 포함되는지(부분 일치)로 대조 — 등록 안 된 역할은 자기 이름 그대로 대조
        ok = any(any(alias in a for alias in ROLE_ALIASES.get(h, [h])) for a in approvers for h in holders)
        return {"ok": ok, "rule": f"{r['rule_id']}@{r['policy_version']}", "approver": r["approver_role"],
                "threshold": r["threshold"], "reason": r["text"]}

    def check_waiver(self, rule_id: str, scope_text: str, on: str,  # pylint: disable=unused-argument
                     project_id: str | None = None) -> dict:
        """
        특정 날짜에 유효한 정책 예외(waiver) 확인

        예외는 정책을 바꾸지 않는다. 범위가 맞는지는 별도 판단.
        Args:
            rule_id:    정책 규칙 ID
            scope_text: 예외 범위와 대조할 문구 (현재 코드에서는 쓰지 않음)
            on:         기준 날짜 (YYYY-MM-DD)
            project_id: 대상 프로젝트. None이면 기본 프로젝트
        Returns:
            {"rule_id", "on", "active_waivers", "note"}
        """
        ws = self.store.active_waivers(rule_id, project_id or self.project_id, on)
        return {"rule_id": rule_id, "on": on, "active_waivers": ws,
                "note": "예외는 정책을 바꾸지 않는다. 범위가 맞는지는 별도 판단 필요" if ws else "유효한 예외 없음"}
