from server.build_info import build_sha


def test_build_sha(monkeypatch):
    monkeypatch.setenv("BUILD_SHA", " ABC1234 ")
    assert build_sha() == "abc1234"
    for bad in ("", "latest", "main", "abc12", "g" * 7):
        monkeypatch.setenv("BUILD_SHA", bad)
        assert build_sha() is None
    monkeypatch.delenv("BUILD_SHA")
    assert build_sha() is None
