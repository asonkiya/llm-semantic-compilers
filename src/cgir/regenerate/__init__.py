"""Prompt-pack + regeneration. The LLM call is an injectable generator seam."""

from cgir.regenerate.prompt_pack import build_prompt
from cgir.regenerate.regenerator import regenerate

__all__ = ["build_prompt", "regenerate"]
