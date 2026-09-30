from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass, field
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


@dataclass
class ContentTiming:
    started: float
    timestamps: list[float] = field(default_factory=list)
    fragments: list[str] = field(default_factory=list)

    def observe(self, content: str | None, timestamp: float) -> None:
        if content:
            if timestamp < self.started or (self.timestamps and timestamp < self.timestamps[-1]):
                raise ValueError("Non-monotonic content timestamps")
            self.timestamps.append(timestamp)
            self.fragments.append(content)

    def finish(self, ended: float, visible_tokens: int, streaming: bool) -> dict[str, Any]:
        if ended < self.started or (self.timestamps and ended < self.timestamps[-1]):
            raise ValueError("Completion predates content")
        available = streaming and bool(self.timestamps)
        span = (self.timestamps[-1] - self.timestamps[0]) * 1000 if available else None
        gaps = [(later - earlier) * 1000 for earlier, later in zip(self.timestamps, self.timestamps[1:])]
        return {
            "duration_ms": (ended - self.started) * 1000,
            "first_content_ms": (self.timestamps[0] - self.started) * 1000 if available else None,
            "last_content_ms": (self.timestamps[-1] - self.started) * 1000 if available else None,
            "content_span_ms": span,
            "estimated_visible_token_interval_ms": span / (visible_tokens - 1) if available and len(self.timestamps) > 1 and visible_tokens > 1 else None,
            "content_event_count": len(self.timestamps) if streaming else None,
            "content_event_gap_p50_ms": quantile(gaps, 0.5) if available else None,
            "content_event_gap_p90_ms": quantile(gaps, 0.9) if available else None,
            "stream_tail_ms": (ended - self.timestamps[-1]) * 1000 if available else None,
        }


FIELDS = {
    "case_id": "string", "stage": "integer", "order_id": "string", "part": "string",
    "region": "string", "priority": "string", "quantity": "integer", "unit_price": "integer",
    "total": "integer", "route": "string", "service_days": "integer", "status": "string",
    "validation_note": "string",
}
VALIDATION_NOTE = "The requested order has been checked against the reference catalog. Quantity and unit price determine the total. Delivery routing and service days follow the stated priority rule."
SCHEMA = {
    "type": "object", "properties": {name: {"type": kind} for name, kind in FIELDS.items()},
    "required": list(FIELDS), "additionalProperties": False,
}


def make_case(index: int) -> dict[str, Any]:
    generator = random.Random(91000 + index)
    regions = ["Italy", "Germany", "France", "Sweden"]
    parts = ["filter", "sensor", "bearing", "valve"]
    orders = [
        {"order_id": f"ORD-{index:03d}-{position:02d}", "part": generator.choice(parts),
         "region": generator.choice(regions), "priority": generator.choice(["normal", "urgent"]),
         "quantity": generator.randint(2, 15), "unit_price": generator.randint(10, 90)}
        for position in range(14)
    ]
    selected = orders[generator.randrange(len(orders))]
    expected = {"case_id": f"CASE-{index:03d}", "stage": 1, **selected,
                "total": selected["quantity"] * selected["unit_price"],
                "route": "express" if selected["priority"] == "urgent" else "standard",
                "service_days": 1 if selected["priority"] == "urgent" else 3, "status": "validated",
                "validation_note": VALIDATION_NOTE}
    return {"index": index, "orders": orders, "expected": expected}


def validate_answer(text: str, expected: dict[str, Any]) -> dict[str, Any]:
    try:
        actual = json.loads(text)
    except (ValueError, TypeError):
        return {"json_valid": False, "schema_valid": False, "correct": False}
    schema_valid = isinstance(actual, dict) and set(actual) == set(FIELDS)
    if schema_valid:
        schema_valid = all(type(actual[name]) is (str if kind == "string" else int) for name, kind in FIELDS.items())
    return {"json_valid": True, "schema_valid": schema_valid, "correct": schema_valid and actual == expected}


def build_messages(case: dict[str, Any], stage: int, nonce: str, previous: str | None = None) -> list[dict[str, str]]:
    if stage == 2 and previous is None:
        raise ValueError("The second call requires the actual first response")
    instruction = (
        f"Request marker {nonce}. Ignore the marker when answering. "
        "Return only the specified JSON object. Copy all order fields exactly. "
        "Compute total = quantity * unit_price. Urgent priority means route express and service_days 1; "
        "normal means route standard and service_days 3. Set status validated. "
        f"Set validation_note to this exact text: {VALIDATION_NOTE} "
        f"Set case_id {case['expected']['case_id']} and stage {stage}. "
        "The reference catalog is context only; use the requested order, not the first or last row."
    )
    task = {"requested_order": case["expected"]["order_id"], "reference_catalog": case["orders"]}
    if stage == 1:
        task["task"] = "Select the requested order from the catalog and compute the required fields."
    else:
        task["task"] = "Verify and recompute the order from the previous result against the catalog. Return stage 2."
        task["previous_result"] = previous
    return [{"role": "developer", "content": instruction}, {"role": "user", "content": canonical_json(task)}]


def request_body(deployment: str, api: str, messages: list[dict[str, str]], streaming: bool, effort: str = "none") -> dict[str, Any]:
    body: dict[str, Any] = {"model": deployment, "stream": streaming, "store": False}
    if api == "chat":
        body.update(messages=messages, reasoning_effort=effort, verbosity="low", max_completion_tokens=512,
                    response_format={"type": "json_schema", "json_schema": {"name": "order_result", "strict": True, "schema": SCHEMA}})
        if streaming:
            body["stream_options"] = {"include_usage": True}
    elif api == "responses":
        body.update(input=messages, reasoning={"effort": effort}, max_output_tokens=512,
                    text={"verbosity": "low", "format": {"type": "json_schema", "name": "order_result", "strict": True, "schema": SCHEMA}})
    else:
        raise ValueError(f"Unknown API: {api}")
    return body