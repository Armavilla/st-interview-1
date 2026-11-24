import asyncio

import pytest
from pydantic import ValidationError

from harness.schemas import Incident, Query, Status
from harness.tools import MockTools


def test_dataset_search():
    tools = MockTools()
    def matches(query):
        return asyncio.run(tools.read("search_knowledge_base", Query(query=query)))["matches"]
    assert [r["doc_id"] for r in matches("checkout-api")] == ["OPS-17"]
    assert matches("unrelated") == []
    assert tools.services["checkout-api"].duration_minutes == 15


@pytest.mark.parametrize("schema,values", [
    (Query, {"query": "  "}),
    (Incident, {"title": "x", "description": "x", "severity": "P9"}),
    (Status, {"service_name": "x", "duration_minutes": 0, "status": "purple"}),
])
def test_invalid_schema(schema, values):
    with pytest.raises(ValidationError):
        schema.model_validate(values)
