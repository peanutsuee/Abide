"""Internal Q2 source-aware raw-byte span production and attribution.

This module deliberately has no public route, provider client, or raw-evidence
reverse-fetch path.  It only turns caller-held captured revision bytes into
prevalidated host candidates.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any, Iterable

from raw_evidence_store import (
    HASH_ALGORITHM,
    REVISION_SCHEMA_VERSION,
    RawEvidenceError,
    RawEvidenceStore,
)


SOURCE_AWARE_PRODUCER_VERSION = "utf8-plain-text-source-v1"
SUPPORTED_SOURCE_FORMATS = frozenset(
    {"utf8_plain_text", "plain_text_utf8", "text/plain; charset=utf-8"}
)
UNSUPPORTED_SOURCE_FORMATS = frozenset(
    {
        "claude_json",
        "chatgpt_json",
        "json",
        "jsonl",
        "markdown",
        "non_utf8",
        "mixed_encoding",
    }
)


# Model-facing views are bounded prefixes of the strict-decoded segment.
# The immutable authority remains the complete raw-byte descriptor range; these
# limits only bound text sent through the existing importer extraction context.
MODEL_CANDIDATE_SEGMENT_MAX_CHARS = 2048
MODEL_CANDIDATE_SEGMENT_MAX_BYTES = 8192
MODEL_CANDIDATE_CONTEXT_MAX_CHARS = 4096
MODEL_CANDIDATE_CONTEXT_MAX_BYTES = 16384


@dataclass(frozen=True)
class SourceSpanCandidate:
    """The only candidate shape allowed to cross into model selection."""

    opaque_candidate_token: str
    source_segment: str
    source_context: str

    def to_model_payload(self) -> dict[str, str]:
        return {
            "opaque_candidate_token": self.opaque_candidate_token,
            "source_segment": self.source_segment,
            "source_context": self.source_context,
        }


@dataclass(frozen=True)
class SourceAwareProduction:
    status: str
    reason: str | None
    candidates: tuple[SourceSpanCandidate, ...]

    def model_candidates(self) -> list[dict[str, str]]:
        return [candidate.to_model_payload() for candidate in self.candidates]


def raw_byte_slice(raw_bytes: bytes, start: int, end: int) -> bytes:
    """Return one strict half-open raw-byte slice without decoding or normalizing."""

    if not isinstance(raw_bytes, bytes):
        raise RawEvidenceError("invalid_input")
    if (
        isinstance(start, bool)
        or not isinstance(start, int)
        or isinstance(end, bool)
        or not isinstance(end, int)
        or start < 0
        or start >= end
        or end > len(raw_bytes)
    ):
        raise RawEvidenceError("raw_byte_range_invalid")
    return raw_bytes[start:end]


def _lossless_utf8_segments(raw_bytes: bytes) -> list[tuple[int, int, str]]:
    """Decode strictly, then retain byte offsets for original LF-delimited lines."""

    if not isinstance(raw_bytes, bytes):
        raise RawEvidenceError("invalid_input")
    raw_bytes.decode("utf-8", errors="strict")
    segments: list[tuple[int, int, str]] = []
    start = 0
    while start < len(raw_bytes):
        newline = raw_bytes.find(b"\n", start)
        end = len(raw_bytes) if newline < 0 else newline + 1
        segment = raw_byte_slice(raw_bytes, start, end)
        segments.append((start, end, segment.decode("utf-8", errors="strict")))
        start = end
    return segments


def _bounded_model_text(text: str, *, max_chars: int, max_bytes: int) -> str:
    """Return a deterministic UTF-8 prefix without normalizing source text."""

    if len(text) <= max_chars and len(text.encode("utf-8")) <= max_bytes:
        return text
    end = min(len(text), max_chars)
    while end and len(text[:end].encode("utf-8")) > max_bytes:
        end -= 1
    return text[:end]


def stable_span_id(
    *,
    revision_id: str,
    producer_version: str,
    run_id: str,
    run_item_key: str,
    input_digest: str,
    raw_byte_start: int,
    raw_byte_end: int,
) -> str:
    """Derive a retry-stable internal descriptor id from explicit producer scope."""

    material = "\x00".join(
        (
            "q2-span-id-v1",
            revision_id,
            producer_version,
            run_id,
            run_item_key,
            input_digest,
            str(raw_byte_start),
            str(raw_byte_end),
        )
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:32]


class SourceAwareSpanProducer:
    """Host-side producer for strict UTF-8 plain-text captured revisions."""

    def __init__(
        self,
        store: RawEvidenceStore,
        *,
        producer_version: str = SOURCE_AWARE_PRODUCER_VERSION,
    ) -> None:
        if not isinstance(store, RawEvidenceStore) or store.is_disabled:
            raise RawEvidenceError("store_disabled")
        if not isinstance(producer_version, str) or not producer_version:
            raise RawEvidenceError("producer_version_invalid")
        self.store = store
        self.producer_version = producer_version

    def produce_candidates(
        self,
        raw_bytes: bytes,
        *,
        source_format: str,
        revision_id: str,
        run_id: str,
        run_item_key: str,
        input_digest: str,
        allow_sealed: bool = False,
        allow_restricted_admin: bool = False,
    ) -> SourceAwareProduction:
        """Create descriptors/mappings before returning model-safe candidates."""

        if source_format not in SUPPORTED_SOURCE_FORMATS:
            return SourceAwareProduction("UNSUPPORTED", "source_format_unsupported", ())
        if not isinstance(raw_bytes, bytes):
            return SourceAwareProduction("UNSUPPORTED", "invalid_input", ())
        if hashlib.sha256(raw_bytes).hexdigest() != input_digest:
            return SourceAwareProduction("UNAVAILABLE", "input_digest_mismatch", ())
        try:
            revision = self.store.get_revision(
                revision_id,
                allow_sealed=allow_sealed,
                allow_restricted_admin=allow_restricted_admin,
            )
        except RawEvidenceError as exc:
            if exc.code in {"sealed_access_denied", "restricted_admin_access_denied"}:
                return SourceAwareProduction("ACCESS_DENIED", "authorization_denied", ())
            return SourceAwareProduction("UNAVAILABLE", "revision_unavailable", ())
        if (
            revision["content_size_bytes"] != len(raw_bytes)
            or revision["content_hash"] != input_digest
        ):
            return SourceAwareProduction("UNAVAILABLE", "revision_input_mismatch", ())
        if (
            revision["revision_schema_version"] != REVISION_SCHEMA_VERSION
            or revision["hash_algorithm"] != HASH_ALGORITHM
        ):
            return SourceAwareProduction("UNSUPPORTED", "revision_metadata_unsupported", ())
        try:
            segments = _lossless_utf8_segments(raw_bytes)
        except UnicodeDecodeError:
            return SourceAwareProduction("UNSUPPORTED", "invalid_utf8", ())
        if not segments:
            return SourceAwareProduction("UNSUPPORTED", "empty_source", ())

        prepared_specs: list[dict[str, Any]] = []
        model_views: list[tuple[str, str]] = []
        for start, end, segment_text in segments:
            span_id = stable_span_id(
                revision_id=revision_id,
                producer_version=self.producer_version,
                run_id=run_id,
                run_item_key=run_item_key,
                input_digest=input_digest,
                raw_byte_start=start,
                raw_byte_end=end,
            )
            prepared_specs.append(
                {
                    "span_id": span_id,
                    "raw_byte_start": start,
                    "raw_byte_end": end,
                    "span_hash": hashlib.sha256(
                        raw_byte_slice(raw_bytes, start, end)
                    ).hexdigest(),
                }
            )
            model_views.append(
                (
                    _bounded_model_text(
                        segment_text,
                        max_chars=MODEL_CANDIDATE_SEGMENT_MAX_CHARS,
                        max_bytes=MODEL_CANDIDATE_SEGMENT_MAX_BYTES,
                    ),
                    _bounded_model_text(
                        segment_text,
                        max_chars=MODEL_CANDIDATE_CONTEXT_MAX_CHARS,
                        max_bytes=MODEL_CANDIDATE_CONTEXT_MAX_BYTES,
                    ),
                )
            )
        try:
            mappings = self.store.create_candidate_set(
                revision_id=revision_id,
                run_id=run_id,
                run_item_key=run_item_key,
                producer_version=self.producer_version,
                input_digest=input_digest,
                candidates=prepared_specs,
                allow_sealed=allow_sealed,
                allow_restricted_admin=allow_restricted_admin,
            )
        except RawEvidenceError:
            return SourceAwareProduction("UNAVAILABLE", "candidate_production_failed", ())
        candidates = tuple(
            SourceSpanCandidate(
                opaque_candidate_token=mapping["candidate_token"],
                source_segment=model_view[0],
                source_context=model_view[1],
            )
            for mapping, model_view in zip(mappings, model_views)
        )
        return SourceAwareProduction("READY", None, tuple(candidates))

    def resolve_model_attribution(
        self,
        candidate_tokens: Iterable[str],
        *,
        revision_id: str,
        run_id: str,
        run_item_key: str,
        input_digest: str,
        allow_sealed: bool = False,
        allow_restricted_admin: bool = False,
    ) -> dict[str, Any]:
        """Resolve only tokens from the current explicit host candidate set."""

        return self.store.resolve_candidate_tokens(
            candidate_tokens=candidate_tokens,
            revision_id=revision_id,
            run_id=run_id,
            run_item_key=run_item_key,
            producer_version=self.producer_version,
            input_digest=input_digest,
            allow_sealed=allow_sealed,
            allow_restricted_admin=allow_restricted_admin,
        )

    def create_selected_citations(
        self,
        candidate_tokens: Iterable[str],
        *,
        lineage_id: str,
        revision_id: str,
        run_id: str,
        run_item_key: str,
        input_digest: str,
        allow_sealed: bool = False,
        allow_restricted_admin: bool = False,
    ) -> dict[str, Any]:
        """Map valid model selections to citations; invalid/empty means no citation."""

        resolved = self.resolve_model_attribution(
            candidate_tokens,
            revision_id=revision_id,
            run_id=run_id,
            run_item_key=run_item_key,
            input_digest=input_digest,
            allow_sealed=allow_sealed,
            allow_restricted_admin=allow_restricted_admin,
        )
        if resolved["status"] != "valid":
            return {"status": resolved["status"], "citation_count": 0}
        count = 0
        for span_id in resolved["span_ids"]:
            self.store.create_lineage_citation(
                lineage_id,
                span_id,
                revision_id=revision_id,
                allow_sealed=allow_sealed,
                allow_restricted_admin=allow_restricted_admin,
            )
            count += 1
        return {"status": "valid", "citation_count": count}


__all__ = [
    "SOURCE_AWARE_PRODUCER_VERSION",
    "MODEL_CANDIDATE_SEGMENT_MAX_CHARS",
    "MODEL_CANDIDATE_SEGMENT_MAX_BYTES",
    "MODEL_CANDIDATE_CONTEXT_MAX_CHARS",
    "MODEL_CANDIDATE_CONTEXT_MAX_BYTES",
    "SUPPORTED_SOURCE_FORMATS",
    "UNSUPPORTED_SOURCE_FORMATS",
    "SourceAwareProduction",
    "SourceAwareSpanProducer",
    "SourceSpanCandidate",
    "raw_byte_slice",
    "stable_span_id",
]
