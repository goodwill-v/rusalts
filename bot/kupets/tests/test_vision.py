"""Тесты парсера ответа VLM."""

from vision.recognizer import parse_vision_response


def test_parse_json_query():
    r = parse_vision_response(
        '{"query":"найди чёрные кроссовки Nike Air Max","brand":"Nike",'
        '"category":"обувь","confidence":"high","notes":"кроссовки"}'
    )
    assert r.query == "чёрные кроссовки Nike Air Max"
    assert r.brand == "Nike"
    assert r.confidence == "high"


def test_parse_fenced_json():
    r = parse_vision_response(
        '```json\n{"query":"смартфон Samsung Galaxy A55 8/256","brand":"Samsung"}\n```'
    )
    assert "Samsung" in r.query
    assert r.brand == "Samsung"


def test_parse_plain_line_fallback():
    r = parse_vision_response("Беспроводные наушники Sony WH-1000XM5 чёрные")
    assert "Sony" in r.query
