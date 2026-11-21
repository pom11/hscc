"""Child-pytest plugin used by ``test_cross_leg_collection.py`` (load with
``-p node_shape_plugin`` + ``PYTHONPATH`` pointing here).

Records, for every item the child collected, the ancestor chain it was built
from — the thing the t_0c4989d7 bug actually corrupted. A hyphenated payload dir
(``hscc-cluster``) must appear as ``Dir:``, never ``Package:``; a real package
(``hscc_daemon``) must still appear as ``Package:``, because a guard that
de-packaged the daemon leg would "fix" the cross-leg crash by silently changing
how the shipped lane imports its own tests.

Written to ``$HSCC_NODE_SHAPE_JSON`` (append, one JSON array per process) rather
than asserted in-process: the child decides, the parent test judges, so the
plugin cannot pass by agreeing with the test that loaded it.
"""

import json
import os

import pytest

_SHAPES = []


def pytest_collection_modifyitems(session, config, items):
    for item in items:
        _SHAPES.append({
            "nodeid": item.nodeid,
            "chain": [
                f"{type(node).__name__}:{node.name}"
                for node in item.iter_parents()
                if isinstance(node, pytest.Directory)
            ],
        })


def pytest_sessionfinish(session, exitstatus):
    path = os.environ.get("HSCC_NODE_SHAPE_JSON")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        for shape in _SHAPES:
            fh.write(json.dumps(shape) + "\n")
