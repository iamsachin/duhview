import hashlib
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class Severity(StrEnum):
    critical = "critical"
    major = "major"
    minor = "minor"
    nitpick = "nitpick"
    info = "info"

    @property
    def rank(self) -> int:
        """0 = most severe."""
        return list(Severity).index(self)


class Category(StrEnum):
    bug = "bug"
    security = "security"
    performance = "performance"
    reliability = "reliability"
    maintainability = "maintainability"
    requirements = "requirements"


class FindingDraft(BaseModel):
    """What a finder agent reports."""

    file: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    severity: Severity
    category: Category
    title: str
    explanation: str
    suggestion: str | None = None


class FinderOutput(BaseModel):
    findings: list[FindingDraft]


class Verdict(BaseModel):
    verdict: Literal["confirm", "reject"]
    severity: Severity
    confidence: float = Field(ge=0, le=1)
    reason: str


class Finding(FindingDraft):
    id: str
    source: str = "agent"
    verified: bool = False
    confidence: float = Field(default=0.0, ge=0, le=1)

    @classmethod
    def from_draft(cls, d: FindingDraft, source: str = "agent") -> "Finding":
        key = f"{d.file}:{d.start_line}:{d.category}:{d.title}"
        return cls(**d.model_dump(), id=hashlib.sha1(key.encode()).hexdigest()[:12], source=source)


class ReviewResult(BaseModel):
    target: str
    model: str
    verifier_model: str | None
    findings: list[Finding]
    incomplete: list[str]
    notes: list[str] = []  # informational, e.g. an optional scanner that is not installed
    units: int = 1
    cost_usd: float
    cost_known: bool
    archive_dir: str | None = None
