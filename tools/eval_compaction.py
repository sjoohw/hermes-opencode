"""Compare a fixed handoff before and after real Hermes compaction.

Use the Hermes checkout on PYTHONPATH and its Python interpreter. The default
candidate and the examiner use the installed OpenCode configuration. Use
--profile-compression with a separately configured Hermes compression profile
to compare another candidate while keeping OpenCode's examiner fixed.
"""
import argparse
import copy
import json
from contextlib import nullcontext
from pathlib import Path
import sys
from unittest.mock import patch

from agent.context_compressor import ContextCompressor
from evals.compaction.fixtures import synthetic_transcript, total_tokens
from evals.compaction.runner import keyword_search, serialize_for_exam

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from support import client as acp

# Gold identifiers occur only in the archived conversation and in the scorer.
CASES = [
    ("constraint", "The acceptance constraint is seal CEDAR-37. Reject unsigned exports.",
     "Which seal is required for accepting an export?", "CEDAR-37"),
    ("reason", "We chose offline validation because of incident AMBER-52: the clock service drifted.",
     "Which incident motivated offline validation?", "AMBER-52"),
    ("failure", "Trial SABLE-81 already failed because the old reader truncated records. Do not retry it.",
     "Which failed trial should be skipped?", "SABLE-81"),
    ("next", "The one outstanding action is ticket IRIS-24: validate the original archive checksum.",
     "Which outstanding ticket should be resumed?", "IRIS-24"),
]


def run(profile_compression):
    messages = synthetic_transcript(n_turns=60, seed=7)
    for index, (_, fact, _, _) in enumerate(CASES):
        offset = 12 + 12 * index
        messages[offset:offset] = [{"role": "user", "content": "Record the outcome of this review."},
                                   {"role": "assistant", "content": fact}]

    client = acp.OpenCodeACPClient()
    try:
        compressor = ContextCompressor(model="opencode-default", config_context_length=32768,
                                       quiet_mode=True, abort_on_summary_failure=True)
        compressor.tail_token_budget = 3000

        def relay(messages, route_info=None, **kwargs):
            result = client.chat.completions.create(messages=messages, timeout=120)
            if route_info is not None:
                route_info.update(provider="hermes-opencode-acp", model=result.model)
            return result

        route = nullcontext() if profile_compression else patch("agent.context_compressor.call_llm", relay)
        with route:
            compressed = compressor.compress(copy.deepcopy(messages), current_tokens=total_tokens(messages), force=True)
        if not compressed or compressor._last_summary_error or len(compressed) >= len(messages):
            raise RuntimeError("Hermes did not produce a successful compaction")
        survivors = {m.get("content") for m in compressed if isinstance(m.get("content"), str)}
        archive = [m for m in messages if m.get("content") not in survivors]
        if not all(any(fact in m.get("content", "") for m in archive) for _, fact, _, _ in CASES):
            raise RuntimeError("Expected all fixed facts inside the compacted region: " + str(
                [key for key, fact, _, _ in CASES if not any(fact in m.get("content", "") for m in archive)]))
        questions = {label: q for label, _, q, _ in CASES}
        questions["action"] = "Give only the ticket ID you would execute now to continue without repeating failed work."
        for label, context in (("uncompressed", messages), ("summary", compressed), ("with_search", compressed)):
            text = serialize_for_exam(context)
            if label == "with_search":
                text += "\nArchived search results:\n" + "\n".join(keyword_search(archive, q) for q in questions.values())
            prompt = text + "\nAnswer each question with one ID or UNKNOWN. Return only a JSON object.\n" + json.dumps(questions)
            result = relay([{"role": "user", "content": prompt}])
            answer = result.choices[0].message.content.strip()
            if answer.startswith("```"):
                answer = "\n".join(answer.splitlines()[1:-1])
            answers = json.loads(answer)
            expected = {key: gold for key, _, _, gold in CASES}
            expected["action"] = expected["next"]
            scores = {key: answers.get(key) == gold for key, gold in expected.items()}
            print(json.dumps({"arm": label, "context_tokens_estimate": total_tokens(context),
                              "compression_model": getattr(compressor, "_last_aux_resolved_model", None),
                              "examiner": result.model, "scores": scores,
                              "missed": [k for k in scores if not scores[k]]}), flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-compression", action="store_true")
    run(parser.parse_args().profile_compression)
