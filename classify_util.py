"""
유사도/클러스터링 유틸 (numpy 없이 순수 Python)
로컬 분류 시스템에서 파일명/태그 임베딩 비교에 사용.
"""

import math


def cosine_sim(a, b) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na  = math.sqrt(sum(x * x for x in a))
    nb  = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def top_k_similar(query_vec, candidates: dict, k: int = 5, threshold: float = 0.75) -> list:
    """candidates: {key: vec}. threshold 이상인 것만, 유사도 내림차순으로 최대 k개 키 반환."""
    scored = []
    for key, vec in candidates.items():
        sim = cosine_sim(query_vec, vec)
        if sim >= threshold:
            scored.append((sim, key))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [key for _, key in scored[:k]]


def cluster_by_similarity(items: list, max_group: int = 25, threshold: float = 0.6) -> list:
    """
    items: [(path, vec), ...]
    임계값 이상 유사한 항목을 같은 그룹으로 그리디 클러스터링한 뒤,
    그룹이 max_group개를 넘으면 유사도 순으로 정렬해 max_group 단위로 잘라낸다.

    반환: [[path, ...], [path, ...], ...]  (각 그룹 최대 max_group개)
    """
    remaining = list(items)
    groups = []

    while remaining:
        seed_path, seed_vec = remaining.pop(0)
        cluster = [(seed_path, seed_vec)]
        rest = []
        for path, vec in remaining:
            if cosine_sim(seed_vec, vec) >= threshold:
                cluster.append((path, vec))
            else:
                rest.append((path, vec))
        remaining = rest
        groups.append(cluster)

    # max_group 초과 그룹은 유사도(seed 기준) 순으로 정렬 후 분할
    chunks = []
    for cluster in groups:
        if len(cluster) <= max_group:
            chunks.append([p for p, _ in cluster])
            continue
        seed_vec = cluster[0][1]
        cluster_sorted = sorted(cluster, key=lambda pv: cosine_sim(seed_vec, pv[1]), reverse=True)
        for i in range(0, len(cluster_sorted), max_group):
            chunk = cluster_sorted[i:i + max_group]
            chunks.append([p for p, _ in chunk])

    return chunks
