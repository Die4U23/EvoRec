"""ID-free residual listwise ranking on a fixed, legal candidate union."""
from dataclasses import dataclass
import math
from pathlib import Path

import numpy as np
import torch
from torch import nn

from evorec.research.baselines import Ranking
from evorec.research.gating import history_signal

SCALAR_NAMES = ("content_cosine", "cf_rrf", "content_rrf", "prior_strength",
                "model_cold", "log_age", "history_length", "history_coherence")


@dataclass
class PoolInputs:
    items: np.ndarray
    contexts: np.ndarray
    scalars: np.ndarray

    def subset(self, positions):
        return PoolInputs(self.items[positions], self.contexts[positions], self.scalars[positions])

    def save(self, path, **extra):
        np.savez_compressed(path, items=self.items, contexts=self.contexts, scalars=self.scalars, **extra)

    @classmethod
    def join(cls, pools):
        return cls(*(np.concatenate([getattr(p, field) for p in pools]) for field in ("items", "contexts", "scalars")))


def build_pool_inputs(features, catalog, training_items, priors, queries, collaborative, content,
                      *, pool_k=400, decay=.8, constant=60):
    if not (len(queries) == len(collaborative) == len(content)) or pool_k < 1 or constant <= 0:
        raise ValueError("invalid pool inputs")
    contexts = features.histories([q.history for q in queries], decay)
    item_indices = np.zeros((len(queries), pool_k), dtype=np.int32)
    scalars = np.zeros((len(queries), pool_k, len(SCALAR_NAMES)), dtype=np.float32)
    for row, (query, cf, raw) in enumerate(zip(queries, collaborative, content, strict=True)):
        if len(set(cf.items)) != len(cf.items) or len(set(raw.items)) != len(raw.items):
            raise ValueError("duplicate provider candidates")
        items = sorted(set(cf.items) | set(raw.items))
        if len(items) > pool_k:
            raise ValueError("union exceeds declared budget")
        if any(item in query.seen or catalog[item] >= query.timestamp_ms for item in items):
            raise ValueError("illegal candidate")
        indices = [features.mapping[item] for item in items]
        item_indices[row, :len(items)] = np.array(indices)+1  # zero is padding only
        cf_rank = {item: (constant+1)/(constant+i) for i,item in enumerate(cf.items,1)}
        raw_rank = {item: (constant+1)/(constant+i) for i,item in enumerate(raw.items,1)}
        signal = history_signal(features, query.history, decay)
        for column,item in enumerate(items):
            scalars[row,column] = (
                float(contexts[row] @ features.vectors[indices[column]]),
                cf_rank.get(item,0.), raw_rank.get(item,0.), priors.get(item,0.),
                float(item not in training_items),
                min(1., math.log1p((query.timestamp_ms-catalog[item])/86400000)/math.log1p(3650)),
                math.log1p(min(len(query.history),50))/math.log1p(50), signal.coherence)
    # Deliberately never reads query.target, target_model_cold or target_available.
    return PoolInputs(item_indices, contexts, scalars)


def target_positions(pool, features, queries):
    if len(pool.items) != len(queries):
        raise ValueError("query count mismatch")
    targets = []
    for row, query in enumerate(queries):
        item = features.mapping.get(query.target, -2)+1
        matches = np.flatnonzero(pool.items[row] == item)
        targets.append(int(matches[0]) if len(matches) else -1)
    return np.asarray(targets,dtype=np.int64)


class ResidualListRanker(nn.Module):
    def __init__(self, dimensions, hidden=128, bottleneck=64, base_scale=8, residual_scale=4):
        super().__init__()
        self.base_scale, self.residual_scale = base_scale, residual_scale
        self.network = nn.Sequential(nn.Linear(4*dimensions+len(SCALAR_NAMES),hidden),nn.GELU(),
                                     nn.Linear(hidden,bottleneck),nn.GELU(),nn.Linear(bottleneck,1))
        nn.init.zeros_(self.network[-1].weight)
        nn.init.zeros_(self.network[-1].bias)

    def forward(self, contexts, candidates, scalars, valid):
        if contexts.ndim != 2 or candidates.ndim != 3 or candidates.shape[:2] != scalars.shape[:2]:
            raise ValueError("invalid ranker shapes")
        users=contexts[:,None,:].expand_as(candidates)
        values=torch.cat((users,candidates,users*candidates,torch.abs(users-candidates),scalars),dim=-1)
        delta=self.residual_scale*torch.tanh(self.network(values).squeeze(-1))
        base=self.base_scale*.5*(scalars[...,1]+scalars[...,2])
        return (base+delta).masked_fill(~valid,-torch.inf)


def listwise_loss(scores, targets, cold, cold_weight):
    if cold_weight < 1 or len(scores) != len(targets):
        raise ValueError("invalid training weights or count")
    losses=nn.functional.cross_entropy(scores,targets,reduction="none")
    weights=torch.where(cold, torch.as_tensor(float(cold_weight),device=scores.device), 1.)
    return (losses*weights).sum()/weights.sum()


def baseline_rankings(pool, features, k=200):
    scores=.5*(pool.scalars[...,1]+pool.scalars[...,2])
    scores=np.where(pool.items>0,scores,-np.inf)
    return rankings_from_scores(pool,features,scores,k)


def rankings_from_scores(pool, features, scores, k=200):
    if scores.shape != pool.items.shape:
        raise ValueError("score shape mismatch")
    if not np.isfinite(scores[pool.items>0]).all():
        raise ValueError("non-finite candidate score")
    ordered=np.argsort(-scores,axis=1,kind="stable")[:,:k]
    return [Ranking(tuple(features.items[int(pool.items[row,col])-1] for col in columns if pool.items[row,col]>0))
            for row,columns in enumerate(ordered)]


@torch.inference_mode()
def predict(model, pool, features, device="cuda", batch_size=128, k=200):
    model.eval()
    vectors=torch.from_numpy(np.vstack((np.zeros((1,features.vectors.shape[1]),dtype=np.float32),
                                        features.vectors))).to(device)
    scores=np.empty(pool.items.shape,dtype=np.float32)
    for start in range(0,len(pool.items),batch_size):
        end=min(len(pool.items),start+batch_size)
        ids=torch.from_numpy(pool.items[start:end].astype(np.int64)).to(device)
        contexts=torch.from_numpy(pool.contexts[start:end]).to(device)
        scalars=torch.from_numpy(pool.scalars[start:end]).to(device)
        batch=model(contexts,vectors[ids],scalars,ids>0)
        # No representable history: keep the fixed RRF ordering exactly.
        empty=contexts.norm(dim=1)<=1e-8
        batch[empty]=model.base_scale*.5*(scalars[empty,:,1]+scalars[empty,:,2])
        batch.masked_fill_(ids==0,-torch.inf)
        scores[start:end]=batch.cpu().numpy()
    return rankings_from_scores(pool,features,scores,k)


def load_ranker(path, protocol_id, features, model_config, device="cuda"):
    payload=torch.load(Path(path),weights_only=True,map_location="cpu")
    if (payload["protocol_id"] != protocol_id or payload["feature_fingerprint"] != features.fingerprint
            or payload["scalar_names"] != list(SCALAR_NAMES) or payload["model_config"] != model_config):
        raise ValueError("ranker checkpoint protocol or features mismatch")
    model=ResidualListRanker(features.vectors.shape[1],**model_config)
    model.load_state_dict(payload["state_dict"])
    return model.to(device)
