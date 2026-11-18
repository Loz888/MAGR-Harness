"""Real GitHub repository metadata must not erase same-repository search hits."""

import asyncio
import json

import pytest

from mgar_v3.repository_analysis.tools import RepositoryAnalysisTools


@pytest.mark.parametrize(
    "metadata,accepted",
    [
        ({"full_name": "Loz888/MAGR-v3", "name": "MAGR-v3", "owner": {"login": "Loz888"}}, True),
        ({"full_name": "loz888/magr-v3", "owner": {"login": "loz888"}}, True),
        ({"name": "magr-v3", "owner": {"login": "loz888"}}, True),
        ({"full_name": "other/magr-v3", "owner": {"login": "other"}}, False),
    ],
    ids=["github-standard", "full-name-with-owner", "split-owner-name", "other-repository"],
)
def test_search_scope_preserves_standard_github_hits(settings, monkeypatch, metadata, accepted):
    tools = RepositoryAnalysisTools(settings, "loz888/magr-v3", "a" * 40)

    async def controlled(name, arguments):
        assert name == "search_code"
        assert arguments["query"] == "repo:loz888/magr-v3 BodyLimitMiddleware"
        return [], {"items": [{
            "path": "v3/src/mgar_v3/api.py",
            "sha": "b" * 40,
            "repository": metadata,
            "text_matches": [{"fragment": "class BodyLimitMiddleware:"}],
        }]}, []

    monkeypatch.setattr(tools, "_call", controlled)
    result = asyncio.run(tools.search_code("BodyLimitMiddleware", 20))
    hits = json.loads(result.text)["items"]
    assert bool(hits) is accepted
    if accepted:
        assert hits[0]["path"] == "v3/src/mgar_v3/api.py"
        assert hits[0]["snippets"] == ["class BodyLimitMiddleware:"]
    # A search pointer alone never becomes trusted source evidence.
    assert result.evidence_kind is None
    assert result.reference.startswith("github://loz888/magr-v3@" + "a" * 40)
