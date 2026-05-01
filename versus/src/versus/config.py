from __future__ import annotations

import pathlib
from typing import Any

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
    # Stable label used to scope run scripts (--prefix-label) and the
    # /versus/results UI dropdown. The canonical entry is implicitly
    # "default" if unset; sibling variants under prefix_variants must
    # name themselves explicitly.
    id: str = "default"
    n_paragraphs: int = 3
    include_headers: bool = True


class ModelCfg(pydantic.BaseModel):
    """Per-model entry in ``completion.models`` — selects the model and its
    paraphrase axis flag. Sampling / thinking / effort live in the
    registry under ``Config.models[id]``; we keep this entry minimal so
    the registry is the unambiguous source of truth.
    """

    id: str
    # When True, this model also produces paraphrases (in addition to
    # completions). Default False so the same list serves the completion
    # axis directly while paraphrases stay opt-in per-model.
    paraphrase: bool = False


class CompletionCfg(pydantic.BaseModel):
    length_tolerance: float = 0.10
    models: list[ModelCfg]


class ParaphrasingCfg(pydantic.BaseModel):
    enabled: bool = True


class JudgingCfg(pydantic.BaseModel):
    models: list[str]
    criteria: list[str] = pydantic.Field(default_factory=lambda: ["standalone_quality"])
    include_human_as_contestant: bool = True
    max_tokens: int = 32000


class SamplingCfg(pydantic.BaseModel):
    temperature: float | None
    max_tokens: int
    top_p: float | None = None


class VersusModelConfig(pydantic.BaseModel):
    """Per-model effective config that versus applies on the wire.

    Source of truth for what gets sent to the provider — sampling,
    Anthropic thinking block, effort level, optional max-thinking
    budget, optional service tier. Direct paths (completions,
    paraphrases, blind judge) and bridge paths (ws/orch) both read
    from this registry, so a single yaml edit changes the effective
    condition everywhere consistently. The recorded
    ``model_config_hash`` on each row forks naturally on any change,
    keeping old rows reproducible.

    Forward-looking optional fields (top_k, stop_sequences, etc.) get
    added here when they become relevant. ``service_tier`` is most
    useful on prod (``"priority"`` for cost-tolerant lower-latency
    runs); ``max_thinking_tokens`` caps explicit thinking budget when
    a model supports extended thinking with a budget knob.
    """

    sampling: SamplingCfg
    thinking: dict[str, Any] | None = None
    effort: str | None = None
    max_thinking_tokens: int | None = None
    service_tier: str | None = None


class StorageCfg(pydantic.BaseModel):
    # Completions and judgments live in versus_texts / versus_judgments
    # (Postgres) — see versus.versus_db. Only the dormant paraphrase code
    # path still uses a file-backed log.
    paraphrases_log: pathlib.Path = pathlib.Path("data/paraphrases.jsonl")


class Config(pydantic.BaseModel):
    essays: EssaysCfg
    prefix: PrefixCfg
    # Optional sibling prefix configs tracked in parallel with the
    # canonical `prefix`. Each must have a distinct `id`. Run scripts
    # default to the canonical variant; pass --prefix-label <id> to
    # target a sibling.
    prefix_variants: list[PrefixCfg] = pydantic.Field(default_factory=list)
    completion: CompletionCfg
    paraphrasing: ParaphrasingCfg = pydantic.Field(default_factory=ParaphrasingCfg)
    judging: JudgingCfg
    # Per-model effective config registry, keyed by model id. Every
    # model that appears in ``completion.models`` or ``judging.models``
    # must have an entry here (validated on load). Source of truth for
    # what versus actually sends; both direct and bridge call paths
    # read from this registry rather than recomputing from rumil's
    # implicit rules.
    models: dict[str, VersusModelConfig] = pydantic.Field(default_factory=dict)
    storage: StorageCfg
    concurrency: int = 20
    # Per-model concurrency cap. Each unique completion/judge model id
    # gets its own semaphore of this size, so a slow reasoning model
    # can't starve a fast lane. Total in-flight calls = this × n_models.
    # Provider rate limits attach to model ids, so 8 is a safe default
    # for OpenAI; Anthropic / Google would happily take more.
    per_model_concurrency: int = 8

    @pydantic.model_validator(mode="after")
    def _check_unique_prefix_ids(self) -> Config:
        ids = [self.prefix.id, *(v.id for v in self.prefix_variants)]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate prefix variant ids: {sorted(dupes)}")
        return self

    @pydantic.model_validator(mode="after")
    def _check_model_registry_coverage(self) -> Config:
        """Every model used by completion / judging needs a registry entry.

        Catches typos and forgotten registry updates at load time rather
        than at first-call time when the row would silently fall back to
        rumil's implicit rules.
        """
        used: set[str] = {m.id for m in self.completion.models}
        used.update(self.judging.models)
        missing = sorted(used - set(self.models.keys()))
        if missing:
            raise ValueError(
                "config.models is missing entries for: "
                + ", ".join(missing)
                + ". Add a per-model VersusModelConfig under `models:` for each."
            )
        return self


def load(path: str | pathlib.Path = "config.yaml") -> Config:
    with open(path) as f:
        data = yaml.safe_load(f)
    return Config.model_validate(data)
