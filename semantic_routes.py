from dataclasses import dataclass
from hashlib import file_digest, sha256
from itertools import zip_longest
import logging
from pathlib import Path
import re
from tempfile import NamedTemporaryFile, gettempdir

import numpy as np


MODEL_REPO = "Xenova/multilingual-e5-small"
MODEL_REVISION = "761b726dd34fb83930e26aab4e9ac3899aa1fa78"
MODEL_FILES = {
    "tokenizer.json": "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39",
    "onnx/model_quantized.onnx": "f80102d3f2a1229f387d3c81909990d8945513e347b0eab049f7de3c6f98c193",
}
ROUTE_DESCRIPTIONS = {
    "lakeside-walk": (
        "잔잔한 호수와 물결을 바라보며 고요한 물가를 걷고 여유를 느끼는 이야기.",
        "지친 사람이 잠시 멈춰 앉아 숨을 돌리고 조용히 쉬며 마음의 평온과 위로를 되찾는다.",
        "바닷가와 강변의 바람, 저녁 노을과 물 위에 비친 빛을 천천히 감상한다.",
    ),
    "fortress-walk": (
        "옛 왕국과 백제의 역사, 성벽과 토성에 남은 사람들의 자취를 찾아간다.",
        "유적을 답사하고 토기와 비석처럼 오래된 유물을 살펴 과거를 알아본다.",
        "옛 마을의 전통과 풍습, 역사적 사건과 선조들의 삶을 들여다보는 이야기.",
    ),
    "memory-walk": (
        "어린 시절과 고향을 떠올리고 사진이나 일기를 보며 오래된 추억을 되짚는다.",
        "계절이 바뀌는 정원과 숲을 걷고 야생화와 나뭇잎처럼 자연의 작은 변화를 관찰한다.",
        "책 속 장면과 문장을 마음에 담아 일상과 지나온 시간을 돌아보는 이야기.",
    ),
    "lovers-walk": (
        "사랑하는 사람을 만나 설레고 함께 걷거나 헤어지며 관계를 돌아보는 이야기.",
        "가족과 친구, 이웃들이 서로 마음을 나누고 돌보며 함께 사는 공동체의 이야기.",
        "소중한 사람과 손을 잡고 미술관이나 조각공원을 거닐며 감상을 나눈다.",
    ),
    "youth-walk": (
        "주인공이 낯선 곳으로 떠나 험한 산을 오르고 어려움을 헤쳐 나가는 모험이 시작된다.",
        "달리기, 자전거, 경기와 훈련으로 한계에 도전하고 목표를 향해 나아간다.",
        "실패를 딛고 새로운 일에 도전하며 자신감과 용기를 얻는 성장 이야기.",
    ),
}

SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+|(?<=[。！？])\s*|\n+")
MAX_SENTENCES = 28
MIN_MARGIN = 0.012


class SemanticUnavailable(Exception):
    """The optional local model is unavailable."""


@dataclass(frozen=True)
class Sentence:
    source: str
    text: str


@dataclass(frozen=True)
class RouteScore:
    route: str
    score: float
    evidence: tuple[Sentence, ...]


def collect_sentences(documents):
    by_source = []
    seen = set()
    for source, text in documents:
        sentences = []
        for piece in SENTENCE_BREAK.split(text):
            sentence = " ".join(piece.split())
            key = re.sub(r"[\W_]+", "", sentence).casefold()
            if len(key) < 6 or key in seen:
                continue
            sentences.append(Sentence(source, sentence[:420]))
            seen.add(key)
            if len(sentences) == 7:
                break
        by_source.append(sentences)
    ordered = [sentence for row in zip_longest(*by_source) for sentence in row if sentence is not None]
    return tuple(ordered[:MAX_SENTENCES])


class SemanticRouteModel:
    def __init__(self, tokenizer_path, model_path):
        from onnxruntime import InferenceSession, SessionOptions
        from tokenizers import Tokenizer

        self.tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self.tokenizer.enable_truncation(max_length=192)
        self.tokenizer.enable_padding(pad_id=self.tokenizer.token_to_id("<pad>"), pad_token="<pad>")
        options = SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        self.session = InferenceSession(str(model_path), sess_options=options, providers=["CPUExecutionProvider"])
        self.input_names = {item.name for item in self.session.get_inputs()}
        self.route_vectors = {
            route: self.embed(descriptions) for route, descriptions in ROUTE_DESCRIPTIONS.items()
        }

    def embed(self, texts):
        result = []
        for start in range(0, len(texts), 8):
            batch = self.tokenizer.encode_batch([f"query: {text}" for text in texts[start:start + 8]])
            mask = np.array([item.attention_mask for item in batch], dtype=np.int64)
            inputs = {
                "input_ids": np.array([item.ids for item in batch], dtype=np.int64),
                "attention_mask": mask,
                "token_type_ids": np.array([item.type_ids for item in batch], dtype=np.int64),
            }
            hidden = self.session.run(None, {key: value for key, value in inputs.items() if key in self.input_names})[0]
            weights = mask.astype(hidden.dtype)
            pooled = (hidden * weights[:, :, None]).sum(axis=1) / weights.sum(axis=1)[:, None]
            result.append(pooled / np.maximum(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-9))
        return np.concatenate(result, axis=0)

    def rank(self, sentences, route_ids):
        routes = [route for route in route_ids if route in self.route_vectors]
        vectors = self.embed([sentence.text for sentence in sentences])
        similarity = np.column_stack([(vectors @ self.route_vectors[route].T).max(axis=1) for route in routes])
        # Compare each sentence across the same routes before aggregating its strongest evidence.
        relative = similarity - similarity.mean(axis=1, keepdims=True)
        results = []
        for index, route in enumerate(routes):
            evidence = np.argsort(-relative[:, index], kind="stable")[:3]
            score = float(relative[evidence, index].mean())
            results.append(RouteScore(route, score, tuple(sentences[i] for i in evidence)))
        return sorted(results, key=lambda result: result.score, reverse=True)


def model_file(cache_dir, name, expected_hash):
    import httpx

    destination = cache_dir / Path(name).name
    if destination.is_file():
        with destination.open("rb") as file:
            if file_digest(file, "sha256").hexdigest() == expected_hash:
                return destination
    url = f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}/{name}"
    with NamedTemporaryFile(dir=cache_dir, delete=False) as temporary:
        download = Path(temporary.name)
    try:
        digest = sha256()
        with httpx.stream("GET", url, follow_redirects=True, timeout=15) as response, download.open("wb") as file:
            response.raise_for_status()
            for block in response.iter_bytes(chunk_size=1 << 20):
                digest.update(block)
                file.write(block)
        if digest.hexdigest() != expected_hash:
            raise ValueError("Model file checksum did not match.")
        download.replace(destination)
        return destination
    finally:
        download.unlink(missing_ok=True)


def load_semantic_model():
    try:
        cache_dir = Path(gettempdir()) / f"olympic-walk-e5-{MODEL_REVISION}"
        cache_dir.mkdir(exist_ok=True)
        paths = [model_file(cache_dir, name, digest) for name, digest in MODEL_FILES.items()]
        return SemanticRouteModel(*paths)
    except Exception as error:
        logging.getLogger(__name__).warning("Unable to load semantic model: %s", type(error).__name__)
        raise SemanticUnavailable from error
