import math
from dataclasses import dataclass

import torch
import torch.nn as nn
from torch.nn import functional as F


@dataclass
class GPTConfig:
    block_size: int = 32
    vocab_size: int = 65
    n_layer: int = 4
    n_head: int = 4
    n_embed: int = 64
    dropout: float = 0.0
    bias: bool = False


class LayerNorm(nn.Module):

    def __init__(self, ndim):
        super().__init__()
        self.layer_norm = nn.LayerNorm(ndim)

    def forward(self, x):
        return self.layer_norm(x)


class SelfAttention(nn.Module):

    def __init__(self, config):
        super().__init__()
        assert config.n_embed % config.n_head == 0
        self.attn = nn.Linear(config.n_embed, 3 * config.n_embed, bias=config.bias)
        self.proj = nn.Linear(config.n_embed, config.n_embed, bias=config.bias)
        self.attn_dropout = nn.Dropout(config.dropout)
        self.resid_dropout = nn.Dropout(config.dropout)
        self.n_head = config.n_head
        self.n_embed = config.n_embed
        self.dropout = config.dropout
        self.register_buffer("bias", torch.tril(torch.ones(config.block_size, config.block_size))
                             .view(1, 1, config.block_size, config.block_size))

    def forward(self, x):
        B, T, C = x.size()  # batch_size, sequence_length, embedding dimensionality
        attn_layer = self.attn(x)  # calculate query, key, values for all heads
        q, k, v = attn_layer.split(self.n_embed, dim=2)  # split query, key, values
        q = q.view(B, T, self.n_head, self.n_embed // self.n_head)  # (B, T, n_head, head_size)
        k = k.view(B, T, self.n_head, self.n_embed // self.n_head)  # (B, T, n_head, head_size)
        v = v.view(B, T, self.n_head, self.n_embed // self.n_head)  # (B, T, n_head, head_size)

        q = q.transpose(1, 2)  # (B, n_head, T, head_size)
        k = k.transpose(1, 2)  # (B, n_head, T, head_size)
        v = v.transpose(1, 2)  # (B, n_head, T, head_size)

        attn = (q @ k.transpose(-2, -1)) * (1.0 / math.sqrt(k.size(-1)))
        attn = attn.masked_fill(self.bias[:, :, :T, :T] == 0, float('-inf'))
        attn = F.softmax(attn, dim=-1)
        attn = self.attn_dropout(attn)
        y = attn @ v  # （B, nh, T, T) x (B, nh, T, hs)->(B, nh, T, hs)
        y = y.transpose(1, 2).contiguous().view(B, T, C)
        y = self.resid_dropout(y)
        return y


class MLP(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.fc = nn.Linear(config.n_embed, 4 * config.n_embed, bias=config.bias)
        self.gelu = nn.GELU()
        self.project = nn.Linear(4 * config.n_embed, config.n_embed, bias=config.bias)
        self.dropout = nn.Dropout(config.dropout)

    def forward(self, x):
        x = self.fc(x)
        x = self.gelu(x)
        x = self.project(x)
        x = self.dropout(x)
        return x


class Block(nn.Module):

    def __init__(self, config):
        super().__init__()
        self.ln_1 = LayerNorm(config.n_embed)
        self.attn = SelfAttention(config)
        self.ln_2 = LayerNorm(config.n_embed)
        self.mlp = MLP(config)

    def forward(self, x):
        x = x + self.attn(self.ln_1(x))
        x = x + self.mlp(self.ln_2(x))
        return x


class GPT(nn.Module):

    def __init__(self, config):
        super().__init__()
        # do some validation
        assert config.vocab_size is not None
        assert config.block_size is not None
        assert config.n_embed is not None
        assert config.n_layer is not None
        assert config.dropout is not None
        self.config = config

        self.transformer = nn.ModuleDict(dict(
            word_embedding=nn.Embedding(config.vocab_size, config.n_embed),
            positional_embedding=nn.Embedding(config.block_size, config.n_embed),
            dropout=nn.Dropout(config.dropout),
            blocks=nn.ModuleList([Block(config) for _ in range(config.n_layer)]),
            layer_norm=LayerNorm(config.n_embed)
        ))
        self.linear_transform = nn.Linear(config.n_embed, config.vocab_size, bias=False)
        # https://paperswithcode.com/method/weight-tying
        self.transformer.word_embedding.weight = self.linear_transform.weight
        self.apply(self._init_weights)
        for pn, p in self.named_parameters():
            if pn.endswith('project.weight'):
                torch.nn.init.normal_(p, mean=0.0, std=0.02/math.sqrt(2*config.n_layer))

        print("number of parameters: %.2fM" % (self.get_num_params() / 1e6))

    def get_num_params(self, non_embedding=True):
        n_params =  sum(p.numel() for p in self.parameters())
        if non_embedding:
            n_params -= self.transformer.word_embedding.weight.numel()
            n_params -= self.transformer.positional_embedding.weight.numel()
        return n_params

    def _init_weights(self, module):
        if isinstance(module, nn.Linear):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)
            if module.bias is not None:
                torch.nn.init.zeros_(module.bias)
        elif isinstance(module, nn.Embedding):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)

    def forward(self, idx, targets=None):
        device = idx.device
        b, t = idx.size()
        assert t <= self.config.block_size
        pos = torch.arange(0, t, dtype=torch.long, device=device)
        word_emb = self.transformer.word_embedding(idx)
        pos_emb = self.transformer.positional_embedding(pos)
        x = self.transformer.dropout(word_emb + pos_emb)
        for block in self.transformer.blocks:
            x = block(x)
        x = self.transformer.layer_norm(x)
        if targets is not None:
            logits = self.linear_transform(x)
            loss = F.cross_entropy(logits.view(-1, logits.size(-1)), targets.view(-1))
        else:
            logits = self.linear_transform(x[:, [-1], :])
            loss = None
        return logits, loss

    def generate(self, idx, max_new_tokens, temperature=1.0):
        for _ in range(max_new_tokens):
            idx_truncated = idx if idx.size(1) <= self.config.block_size else idx[:, -self.config.block_size:]
            logits, _ = self(idx_truncated)
            logits = logits[:, -1, :] / temperature
            probs = F.softmax(logits, dim=-1)
            idx_next = torch.multinomial(probs, num_samples=1)
            idx = torch.cat((idx, idx_next), dim=1)
        return idx
