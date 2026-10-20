"""검색 임계값 — 해석기(resolve)와 그래프가 같은 값을 쓰도록 한곳에 둔다.

eval/retrieval_eval.py로 정답·함정 문장의 점수 분포를 재서 정했다 (결과 eval/retrieval_results.md).
데이터가 바뀌면 다시 재서 고친다.
"""

# 주제 1위 점수: 정답 최소 0.553, 목록에 없는 새 주제(함정) 최대 0.463
MIN_TOPIC_SCORE = 0.50
# 회의 원문: 관련 발언 최소 0.626, 논의한 적 없는 주제의 1위 최대 0.527
MIN_DISCUSSION_SCORE = 0.58
# 다른 주제의 유효 결정이 이 이상 가까우면 같은 사안일 수 있다 (같은 주제 결정 최소 0.553)
CROSS_TOPIC_SCORE = 0.55
