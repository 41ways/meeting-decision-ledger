"""검색 임계값 — 해석기(resolve)와 그래프가 같은 값을 쓰도록 한곳에 둔다.

eval/retrieval_eval.py로 정답·함정 문장의 점수 분포를 재서 정했다 (결과 eval/retrieval_results.md).
데이터가 바뀌면 다시 재서 고친다.
"""

# 주제 1위 점수: 정답 최소 0.553, 목록에 없는 새 주제(함정) 최대 0.463
MIN_TOPIC_SCORE = 0.50
# 회의 원문: 관련 발언 최소 0.626, 논의한 적 없는 주제의 1위 최대 0.527
# 에이전트는 원문을 단어 검색(term_index.TranscriptIndex)으로 찾아 이 값을 쓰지 않는다. 벡터 R1 측정(retrieval_eval) 기록용
MIN_DISCUSSION_SCORE = 0.58
# 다른 주제의 유효 결정이 이 이상 가까우면 같은 사안일 수 있다 (같은 주제 결정 최소 0.553)
CROSS_TOPIC_SCORE = 0.55

# 결정·미결 후보를 LLM이 한 번에 읽는 상한 — 색인으로 좁힌 후보가 이보다 많으면 별칭 충돌로 보고 전체 읽기 대비책으로 간다
# 목록이 길수록 가운데를 놓치고 느려진다 (eval/list_scaling.md)
CANDIDATE_LIMIT = 50
# 전체 읽기에서 한 번에 보내는 후보 목록의 토큰 상한 — 넘으면 나눠 읽는다 (NUM_CTX 16k에서 지시문·질문·출력 몫을 뺀 값)
LIST_TOKEN_BUDGET = 6000
