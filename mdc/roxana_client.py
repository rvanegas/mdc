"""Client for Roxana's AppSync GraphQL API (API-key auth)."""

from __future__ import annotations

import json
import secrets
import urllib.error
import urllib.request

# Alphabet of crypto-random-string's "distinguishable" type, lowercased —
# matches generateDiscussionId in roxana/src/app/util.tsx.
_ID_ALPHABET = "cdehkmprtuwxy012458"
_ID_LENGTH = 4

_GET_DISCUSSION = "query($id: ID!) { getDiscussion(id: $id) { id } }"
_CREATE_SENTENCE = "mutation($input: CreateSentenceInput!) { createSentence(input: $input) { id } }"
_CREATE_DISCUSSION = "mutation($input: CreateDiscussionInput!) { createDiscussion(input: $input) { id } }"
_DELETE_SENTENCE = "mutation($input: DeleteSentenceInput!) { deleteSentence(input: $input) { id } }"


def generate_discussion_id() -> str:
    return "".join(secrets.choice(_ID_ALPHABET) for _ in range(_ID_LENGTH))


def _graphql(url: str, api_key: str, query: str, variables: dict) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "x-api-key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Roxana API request failed: HTTP {e.code} — {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Roxana API unreachable: {e.reason}") from e
    if payload.get("errors"):
        messages = "; ".join(err.get("message", str(err)) for err in payload["errors"])
        raise RuntimeError(f"Roxana API error: {messages}")
    return payload.get("data", {})


def discussion_exists(url: str, api_key: str, discussion_id: str) -> bool:
    data = _graphql(url, api_key, _GET_DISCUSSION, {"id": discussion_id})
    return data.get("getDiscussion") is not None


def create_sentence(url: str, api_key: str, content: str, discussion_id: str) -> str:
    data = _graphql(
        url, api_key, _CREATE_SENTENCE,
        {"input": {"content": content, "discussionId": discussion_id}},
    )
    return data["createSentence"]["id"]


def create_discussion(url: str, api_key: str, discussion_id: str, layout: str) -> None:
    _graphql(
        url, api_key, _CREATE_DISCUSSION,
        {"input": {
            "id": discussion_id,
            "version": 2,
            "revision": 1,
            "isPrivate": False,
            "layout": layout,
            "pool": 1,
        }},
    )


def delete_sentence(url: str, api_key: str, sentence_id: str) -> None:
    _graphql(url, api_key, _DELETE_SENTENCE, {"input": {"id": sentence_id}})
