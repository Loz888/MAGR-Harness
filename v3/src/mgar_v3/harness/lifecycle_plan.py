"""HTTP expectations derived only from explicit public goal text."""

import json
import re

from mgar_v3.harness.lifecycle_types import HTTPExpectation


def public_routes(goal: str) -> tuple[HTTPExpectation, ...]:
    routes = {}
    pattern = r"\bGET\s+[`'\"]?(/[A-Za-z0-9_./~-]*)[`'\"]?\s+(?:returning|returns)\s+"
    for match in re.finditer(pattern, goal, re.I):
        rest = goal[match.end() :].lstrip(" `")
        number = re.match(r"([1-5][0-9]{2})\b\s*", rest)
        status = int(number[1]) if number else 200
        if number:
            rest = rest[number.end() :]
        mime = re.match(r"([A-Za-z]+/[A-Za-z0-9.+-]+)\s*", rest)
        content_type = mime[1] if mime else None
        if mime:
            rest = rest[mime.end() :]
        if rest.lower().startswith("json "):
            content_type, rest = "application/json", rest[5:].lstrip()
        rest = rest.lstrip(" `")
        try:
            body, _ = json.JSONDecoder().raw_decode(rest)
        except ValueError:
            body = None
        routes[match[1]] = HTTPExpectation(
            path=match[1],
            status_code=status,
            content_type=content_type,
            body=body if isinstance(body, str) else None,
            json_body=body if isinstance(body, dict) else None,
        )
    return tuple(routes.values())
