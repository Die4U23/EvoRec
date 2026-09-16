"""Train-only text features, content towers and exact time-filtered retrieval."""
import hashlib
import json
from pathlib import Path

import joblib
import numpy as np
import torch
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.preprocessing import normalize
from threadpoolctl import threadpool_limits
from torch import nn

from evorec.research.baselines import Ranking
from evorec.research.protocol import AvailableAt, Protocol
from evorec.research.runner import file_sha


class ContentProtocol(Protocol):
    def __init__(self, config):
        super().__init__(config)
        path = Path(config["metadata_path"])
        self.metadata_manifest = json.loads(path.with_suffix(".manifest.json").read_text())
        if file_sha(path) != self.metadata_manifest["metadata_sha256"]:
            raise ValueError("metadata integrity failure")
        if self.metadata_manifest["catalog_sha256"] != self.manifest["catalog_sha256"]:
            raise ValueError("metadata/catalog mismatch")
        if self.metadata_manifest["fields"] != ["title", "categories"] or config["content"]["fields"] != ["title", "categories"]:
            raise ValueError("only title and categories are allowed")
        if self.metadata_manifest["access_assumption"] != config["content"]["access_assumption"]:
            raise ValueError("metadata access protocol mismatch")
        self.metadata = json.loads(path.read_text(encoding="utf-8"))
        self.fingerprint = {**self.fingerprint, "protocol": "r03-static-content-user-bucket-v1",
                            "metadata_sha256": self.metadata_manifest["metadata_sha256"],
                            "content": config["content"]}
        self.protocol_id = hashlib.sha256(json.dumps(self.fingerprint, sort_keys=True).encode()).hexdigest()[:16]


class ContentFeatures:
    def __init__(self, items, vectors):
        self.items = tuple(items)
        self.mapping = {item: i for i, item in enumerate(self.items)}
        self.vectors = np.asarray(vectors, dtype=np.float32)
        if self.vectors.ndim != 2 or len(self.vectors) != len(items) or not np.isfinite(self.vectors).all():
            raise ValueError("invalid content vectors")
        self.present = np.linalg.norm(self.vectors, axis=1) > 1e-8
        digest = hashlib.sha256(self.vectors.tobytes(order="C"))
        digest.update(json.dumps(self.items).encode())
        self.fingerprint = digest.hexdigest()

    def histories(self, histories, decay=.8):
        values = np.zeros((len(histories), self.vectors.shape[1]), dtype=np.float32)
        for row, history in enumerate(histories):
            for distance, item in enumerate(reversed(history)):
                index = self.mapping.get(item)
                if index is not None and self.present[index]:
                    values[row] += self.vectors[index] * decay**distance
        return normalize(values).astype(np.float32)

    @classmethod
    def fit(cls, protocol, directory):
        config = protocol.config["content"]
        items = tuple(sorted(protocol.catalog))
        fit_items = tuple(item for item in protocol.vocabulary if protocol.metadata.get(item, "").strip())
        if len(fit_items) <= config["dimensions"]:
            raise ValueError("too few training documents for requested SVD")
        texts = [protocol.metadata.get(item, "") for item in items]
        vectorizer = TfidfVectorizer(max_features=config["max_features"], min_df=config["min_df"],
                                    ngram_range=(1, 2), sublinear_tf=True, dtype=np.float32)
        with threadpool_limits(limits=4):
            train_matrix = vectorizer.fit_transform([protocol.metadata[item] for item in fit_items])
            svd = TruncatedSVD(n_components=config["dimensions"], n_iter=7, random_state=config["svd_seed"])
            svd.fit(train_matrix)
            matrix = vectorizer.transform(texts)
            vectors = normalize(svd.transform(matrix)).astype(np.float32)
        feature = cls(items, vectors)
        directory.mkdir(parents=True, exist_ok=False)
        joblib.dump({"vectorizer": vectorizer, "svd": svd}, directory / "encoder.joblib")
        np.save(directory / "vectors.npy", vectors)
        (directory / "items.json").write_text(json.dumps(items) + "\n")
        # Round-trip on in-vocabulary and catalog-held-out texts, without refitting.
        restored = joblib.load(directory / "encoder.joblib")
        indices = sorted({0, len(items)//2, len(items)-1})
        repeated = normalize(restored["svd"].transform(restored["vectorizer"].transform([texts[i] for i in indices])))
        if not np.allclose(repeated, vectors[indices], atol=1e-6):
            raise ValueError("reloaded text encoder differs")
        manifest = {
            "protocol_id": protocol.protocol_id, "fit_document_count": len(fit_items),
            "fit_item_set_sha256": hashlib.sha256("\n".join(fit_items).encode()).hexdigest(),
            "vocabulary_terms": len(vectorizer.vocabulary_), "dimensions": vectors.shape[1],
            "catalog_items": len(items), "represented_items": int(feature.present.sum()),
            "missing_or_oov_items": int((~feature.present).sum()), "encoder_reload_verified": True,
            "files": {p.name: file_sha(p) for p in sorted(directory.iterdir()) if p.is_file()},
            "fit_scope": "selected users' train-positive item metadata only",
        }
        (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        return feature, manifest


class ContentTower(nn.Module):
    """Two residual MLP towers; no item-ID embedding or pretrained language model."""
    def __init__(self, dimensions, hidden=128):
        super().__init__()
        self.user = nn.Sequential(nn.Linear(dimensions, hidden), nn.GELU(), nn.Linear(hidden, dimensions))
        self.item = nn.Sequential(nn.Linear(dimensions, hidden), nn.GELU(), nn.Linear(hidden, dimensions))
        for tower in (self.user, self.item):
            nn.init.zeros_(tower[-1].weight)
            nn.init.zeros_(tower[-1].bias)

    def user_vectors(self, values):
        return nn.functional.normalize(values + self.user(values), dim=-1)

    def item_vectors(self, values):
        return nn.functional.normalize(values + self.item(values), dim=-1)

    def forward(self, histories, candidates, temperature=.1):
        return self.user_vectors(histories) @ self.item_vectors(candidates).T / temperature


class ContentPredictor:
    def __init__(self, features, catalog, fallback, model=None, device="cuda", batch_size=128, decay=.8):
        self.features, self.catalog, self.fallback = features, catalog, fallback
        self.model, self.device, self.batch_size, self.decay = model, device, batch_size, decay
        self.raw_items = torch.from_numpy(features.vectors).to(device)
        self.present = torch.from_numpy(features.present).to(device)
        self.first_seen = torch.tensor([catalog[item] for item in features.items], device=device)

    @torch.inference_mode()
    def rank_many(self, queries, k=200):
        if self.model is not None:
            self.model.eval()
            item_vectors = self.model.item_vectors(self.raw_items)
        else:
            item_vectors = self.raw_items
        histories = self.features.histories([q.history for q in queries], self.decay)
        results = []
        for offset in range(0, len(queries), self.batch_size):
            batch = queries[offset:offset + self.batch_size]
            values = torch.from_numpy(histories[offset:offset + self.batch_size]).to(self.device)
            effective = values.norm(dim=1) > 1e-8
            users = self.model.user_vectors(values) if self.model is not None else values
            scores = users @ item_vectors.T
            if not torch.isfinite(scores).all():
                raise ValueError("non-finite content scores")
            timestamps = torch.tensor([q.timestamp_ms for q in batch], device=self.device)
            scores.masked_fill_(self.first_seen[None, :] >= timestamps[:, None], -torch.inf)
            scores.masked_fill_(~self.present[None, :], -torch.inf)
            rows, columns = [], []
            for row, query in enumerate(batch):
                for item in query.seen:
                    if item in self.features.mapping:
                        rows.append(row)
                        columns.append(self.features.mapping[item])
            if rows:
                scores[rows, columns] = -torch.inf
            # Stable full sorting makes ID tie-breaking exact and reproducible.
            indices = torch.argsort(scores, dim=1, descending=True, stable=True)[:, :min(k, len(self.features.items))]
            finite = torch.gather(scores, 1, indices).isfinite().cpu().tolist()
            for row, indices_row in enumerate(indices.cpu().tolist()):
                query = batch[row]
                fallback = self.fallback.rank((), query.seen, AvailableAt(self.catalog, query.timestamp_ms), k)
                if not bool(effective[row]):
                    results.append(Ranking(fallback.items, 0, len(fallback.items)))
                    continue
                items = [self.features.items[i] for i, valid in zip(indices_row, finite[row], strict=True) if valid]
                count = len(items)
                items.extend(item for item in fallback.items if item not in items)
                items = items[:k]
                results.append(Ranking(tuple(items), count, len(items)-count))
        return results


def reciprocal_fusion(base, content, alpha=.25, constant=60, k=200):
    if len(base) != len(content) or not 0 <= alpha <= 1 or constant <= 0 or k <= 0:
        raise ValueError("invalid fusion configuration or result count")
    result = []
    for left, right in zip(base, content, strict=True):
        scores = {}
        for weight, ranking in ((1-alpha, left), (alpha, right)):
            if weight == 0:
                continue
            for rank, item in enumerate(ranking.items, 1):
                scores[item] = scores.get(item, 0.0) + weight / (constant + rank)
        selected = tuple(sorted(scores, key=lambda item: (-scores[item], item))[:k])
        result.append(Ranking(selected))
    return result
