from __future__ import annotations

import pathlib

import pydantic
import yaml


class SourceCfg(pydantic.BaseModel):
    id: str
    max_recent: int = 5
    max_images: int | None = None  # skip essay if image_count > this
    max_image_ratio: float | None = None  # skip essay if image_count / paragraph_count > this


class EssaysCfg(pydantic.BaseModel):
    sources: list[SourceCfg] = pydantic.Field(default_factory=list)
    cache_dir: pathlib.Path = pathlib.Path("data/essays")
    exclude_ids: list[str] = pydantic.Field(default_factory=list)  # skip these namespaced essay ids


class PrefixCfg(pydantic.BaseModel):
    n_paragraphs: int = 3
    include_headers: bool = True


class ModelCfg(pydantic.BaseModel):
    id: str
    temperature: float = 0.7
    max_tokens: int = 4000
    top_p: float | None = None


class CompletionCfg(pydantic.BaseModel):
    length_tolerance: float = 0.10
    models: list[ModelCfg]


class ParaphrasingCfg(pydantic.BaseModel):
    enabled: bool = True
    models: list[ModelCfg] = pydantic.Field(default_factory=list)


class JudgingCfg(pydantic.BaseModel):
    models: list[str]
    anthropic_models: list[str] = pydantic.Field(default_factory=list)
    criteria: list[str] = pydantic.Field(default_factory=lambda: ["standalone_quality"])
    include_human_as_contestant: bool = True
    max_tokens: int = 32000


class StorageCfg(pydantic.BaseModel):
    completions_log: pathlib.Path = pathlib.Path("data/completions.jsonl")
    judgments_log: pathlib.Path = pathlib.Path("data/judgments.jsonl")
    paraphrases_log: pathlib.Path = pathlib.Path("data/paraphrases.jsonl")


class Config(pydantic.BaseModel):
    essays: EssaysCfg
    prefix: PrefixCfg
    completion: CompletionCfg
    paraphrasing: ParaphrasingCfg = pydantic.Field(default_factory=ParaphrasingCfg)
    judging: JudgingCfg
    storage: StorageCfg
    concurrency: int = 20


def load(path: str | pathlib.Path = "config.yaml") -> Config:
    with open(path) as f:
        data = yaml.safe_load(f)
    return Config.model_validate(data)
