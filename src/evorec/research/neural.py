"""A project SASRec-style causal encoder trained with full-vocabulary cross entropy."""

import os
os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")

import numpy as np
import torch
from torch import nn

from evorec.research.baselines import Ranking
from evorec.research.protocol import AvailableAt


def configure_seed(seed):
    import random
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.set_num_threads(4)
    torch.backends.cudnn.benchmark = False
    torch.use_deterministic_algorithms(True)
    torch.backends.cuda.enable_flash_sdp(False)
    torch.backends.cuda.enable_mem_efficient_sdp(False)
    torch.backends.cuda.enable_math_sdp(True)


class SequenceModel(nn.Module):
    def __init__(self, item_count, max_length, hidden=64, heads=2, layers=2, dropout=.2):
        super().__init__()
        self.max_length = max_length
        self.items = nn.Embedding(item_count + 1, hidden, padding_idx=0)
        self.positions = nn.Embedding(max_length, hidden)
        self.dropout = nn.Dropout(dropout)
        block = nn.TransformerEncoderLayer(
            hidden, heads, dim_feedforward=hidden * 4, dropout=dropout,
            activation="gelu", batch_first=True, norm_first=True,
        )
        self.encoder = nn.TransformerEncoder(block, layers, norm=nn.LayerNorm(hidden), enable_nested_tensor=False)
        self.output_bias = nn.Parameter(torch.zeros(item_count))
        self.register_buffer("causal_mask", torch.triu(torch.ones(max_length, max_length, dtype=torch.bool), diagonal=1))
        nn.init.normal_(self.items.weight, std=.02)
        nn.init.normal_(self.positions.weight, std=.02)
        with torch.no_grad():
            self.items.weight[0].zero_()

    def encode(self, sequences, lengths):
        if torch.any(lengths <= 0):
            raise ValueError("empty histories must use the explicit fallback")
        positions = torch.arange(sequences.shape[1], device=sequences.device)
        hidden = self.dropout(self.items(sequences) + self.positions(positions))
        hidden = self.encoder(
            hidden, mask=self.causal_mask[:sequences.shape[1], :sequences.shape[1]],
            src_key_padding_mask=sequences.eq(0),
        )
        return hidden[torch.arange(len(sequences), device=sequences.device), lengths - 1]

    def forward(self, sequences, lengths):
        return self.encode(sequences, lengths) @ self.items.weight[1:].T + self.output_bias


def padded(sequences, max_length):
    values = np.zeros((len(sequences), max_length), dtype=np.int64)
    lengths = np.zeros(len(sequences), dtype=np.int64)
    for index, sequence in enumerate(sequences):
        suffix = sequence[-max_length:]
        values[index, :len(suffix)] = suffix
        lengths[index] = len(suffix)
    return torch.from_numpy(values), torch.from_numpy(lengths)


class NeuralPredictor:
    def __init__(self, model, vocabulary, popular, catalog, device="cuda", batch_size=256):
        self.model, self.vocabulary, self.popular = model, vocabulary, popular
        self.mapping = {item: index + 1 for index, item in enumerate(vocabulary)}
        self.catalog, self.device, self.batch_size = catalog, device, batch_size
        self.first_seen = torch.tensor([catalog[item] for item in vocabulary], device=device, dtype=torch.long)

    @torch.inference_mode()
    def rank_many(self, queries, k=200):
        self.model.eval()
        results = [None] * len(queries)
        for offset in range(0, len(queries), self.batch_size):
            batch = queries[offset:offset + self.batch_size]
            mapped = [tuple(self.mapping[item] for item in query.history if item in self.mapping) for query in batch]
            positions = [index for index, history in enumerate(mapped) if history]
            for index, history in enumerate(mapped):
                if not history:
                    query = batch[index]
                    result = self.popular.rank((), query.seen, AvailableAt(self.catalog, query.timestamp_ms), k)
                    results[offset + index] = Ranking(result.items, 0, len(result.items))
            if not positions:
                continue
            sequences, lengths = padded([mapped[index] for index in positions], self.model.max_length)
            scores = self.model(sequences.to(self.device), lengths.to(self.device))
            if not torch.isfinite(scores).all():
                raise ValueError("non-finite model scores")
            timestamps = torch.tensor([batch[index].timestamp_ms for index in positions], device=self.device)
            scores.masked_fill_(self.first_seen.unsqueeze(0) >= timestamps.unsqueeze(1), float("-inf"))
            row_indices, item_indices = [], []
            for row, index in enumerate(positions):
                known_seen = [self.mapping[item] - 1 for item in batch[index].seen if item in self.mapping]
                row_indices.extend([row] * len(known_seen))
                item_indices.extend(known_seen)
            if row_indices:
                scores[row_indices, item_indices] = float("-inf")
            count = min(k, scores.shape[1])
            indices = torch.argsort(scores, dim=1, descending=True, stable=True)[:, :count]
            finite = torch.gather(scores, 1, indices).isfinite().cpu().tolist()
            indices = indices.cpu().tolist()
            for row, index in enumerate(positions):
                items = tuple(self.vocabulary[item] for item, valid in zip(indices[row], finite[row], strict=True) if valid)
                results[offset + index] = Ranking(items, len(items), 0)
        return results
