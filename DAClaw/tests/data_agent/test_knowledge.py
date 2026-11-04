from agent.data_agent import knowledge


def test_connect_and_search_markdown(tmp_path):
    source = tmp_path / "metrics.md"
    source.write_text(
        "# Financial Metrics\n\nGross margin equals gross profit divided by revenue.\n\n"
        "Refund rate is refunded orders divided by all paid orders.",
        encoding="utf-8",
    )
    connected = knowledge.connect_document(str(source))
    result = knowledge.search("How is refund rate defined?")
    assert connected["chunk_count"] >= 1
    assert result["results"]
    assert result["results"][0]["source"] == "metrics"


def test_remove_source(tmp_path):
    source = tmp_path / "note.txt"
    source.write_text("customer lifetime value definition", encoding="utf-8")
    connected = knowledge.connect_document(str(source))
    assert knowledge.remove_source(connected["source_id"]) is True
    assert knowledge.list_sources() == []
