"""Keeps what travels in a URL's query string out of the access log.

Two secrets reach this app in a query string:
  - the researcher key, as ?key= (researcher API calls and /start's cohort
    override), and
  - a rater's sign-in token, as /rate?token=. It is not used up on sign-in: it
    signs the rater in for as long as the rater is active.

uvicorn's access log writes each request's path WITH its query string, and in
production that log is CloudWatch (/ecs/relational-fluency-rater/app, kept 90
days). So the first rater created, and the first sign-in, would have put the
researcher key and a rater's standing credential in the log for anyone who can
read it. Found 2026-11-08, before any rater existed: no such line was ever
written.

The filter keeps each parameter's NAME (what was asked for is still useful in
a log) and drops every VALUE, whatever the name: a list of names to redact
would leak the next secret somebody adds. A name is kept only when it is an
ordinary short name in a name=value pair; anything else (a bare '?<token>',
a 43-character "name") is dropped whole, so a secret cannot survive by being
sent where a name goes. It is installed two ways, so neither
alone has to be right: on the access handler through the log config main()
passes to uvicorn, and on the uvicorn.access logger when this module is
imported (dictConfig does not remove a logger's filters).
"""

from __future__ import annotations

import copy
import logging
import re
from typing import Any, Dict

REMOVED = "[removed]"
ACCESS_LOGGER = "uvicorn.access"

# What a parameter name looks like in this app's URLs (key, token, pid, qid,
# cohort, rater_id, format, PROLIFIC_PID). Longer, or anything else, is not
# kept: it may be a value sent where a name goes.
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,23}")


def redact_query(path_with_query: str) -> str:
    """'/rate?token=abc&x=1' -> '/rate?token=[removed]&x=[removed]'."""
    if "?" not in path_with_query:
        return path_with_query
    path, query = path_with_query.split("?", 1)
    parts = [p for p in query.split("&") if p]
    if not parts:
        return path
    kept = []
    for part in parts:
        name, eq, _ = part.partition("=")
        kept.append(f"{name}={REMOVED}" if eq and _NAME.fullmatch(name) else REMOVED)
    return path + "?" + "&".join(kept)


class StripQueryValues(logging.Filter):
    """Redacts the query values in uvicorn's access record.

    uvicorn logs '%s - "%s %s HTTP/%s" %d' with (client, method,
    path-with-query, http version, status). Any other record passes untouched;
    it never drops a record, so a request is never missing from the log."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            record.args = (args[0], args[1], redact_query(args[2]), args[3], args[4])
        return True


def log_config() -> Dict[str, Any]:
    """uvicorn's own logging config with the filter on its access handler."""
    from uvicorn.config import LOGGING_CONFIG

    cfg = copy.deepcopy(LOGGING_CONFIG)
    cfg.setdefault("filters", {})["strip_query_values"] = {"()": f"{__name__}.StripQueryValues"}
    cfg["handlers"]["access"]["filters"] = ["strip_query_values"]
    return cfg


def install() -> None:
    """Puts the filter on the uvicorn.access logger, once."""
    logger = logging.getLogger(ACCESS_LOGGER)
    if not any(isinstance(f, StripQueryValues) for f in logger.filters):
        logger.addFilter(StripQueryValues())
