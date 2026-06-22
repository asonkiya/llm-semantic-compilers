"""Runtime configuration."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


class CGIRConfig(BaseModel):
    repo_path: Path
    out_dir: Path = Field(default_factory=lambda: Path(".cgir"))
    target_languages: list[str] = Field(default_factory=lambda: ["python"])
    source_backend: str = "tree-sitter"

    @classmethod
    def for_scan(cls, repo_path: Path, out_dir: Path | None = None) -> CGIRConfig:
        repo_path = repo_path.resolve()
        return cls(
            repo_path=repo_path,
            out_dir=(out_dir or repo_path / ".cgir").resolve(),
        )
