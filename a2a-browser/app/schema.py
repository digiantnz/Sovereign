from typing import Optional
from pydantic import BaseModel


class SearchRequest(BaseModel):
    query: str
    locale: str = "en-US"
    return_format: str = "full"   # "full" | "summary" | "raw"
    test_mode: bool = False


class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str
    source: str   # backend that returned this result
    rank: int


class EvidenceItem(BaseModel):
    claim: str
    source_url: str
    confidence: float


class QueryIntelligence(BaseModel):
    original: str
    interpreted: str
    confidence: float
    type: str            # factual | research | current_events | navigational | transactional
    temporal_sensitivity: str  # low | medium | high


class SovereignSynthesis(BaseModel):
    summary: str
    confidence: float
    consensus: str
    contradiction: Optional[str] = None


class EpistemicMetadata(BaseModel):
    freshness: str
    source_count: int
    diversity_score: float
    cross_verification: bool


class BiasAnalysis(BaseModel):
    bias_flags: list[str]
    narrative_warnings: list[str]
    sentiment: str    # neutral | positive | negative


class StructuredEntities(BaseModel):
    prices: list[str]
    dates: list[str]
    organisations: list[str]
    claims: list[str]


class AiNavigation(BaseModel):
    follow_up_queries: list[str]
    related_queries: list[str]
    suggested_next_action: str


class QualityMetrics(BaseModel):
    result_quality_score: float
    evidence_strength: str    # weak | moderate | strong
    data_completeness: float


class TestModeMetrics(BaseModel):
    outbound_ip: Optional[str]
    backend_used: str
    stage_latencies_ms: dict[str, float]   # search, enrich, sanitize, total


class FetchRequest(BaseModel):
    url: str
    extract: str = "text"   # "text" | "html"


class FetchResponse(BaseModel):
    url: str
    title: str
    content: str
    content_length: int
    fetch_sha256: str


class SearchResponse(BaseModel):
    query_intelligence: QueryIntelligence
    sovereign_synthesis: SovereignSynthesis
    epistemic_metadata: EpistemicMetadata
    bias_analysis: BiasAnalysis
    structured_entities: StructuredEntities
    ai_navigation: AiNavigation
    quality_metrics: QualityMetrics
    results: list[SearchResult]
    evidence: list[EvidenceItem]
    result_sha256: str
    backend_used: str
    test_mode_metrics: Optional[TestModeMetrics] = None
