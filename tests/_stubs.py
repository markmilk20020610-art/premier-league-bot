"""测试用桩：让 bot.py 在没有 google-genai / requests 的环境里也能被导入。"""
import sys
import types as T


def install():
    if "google.genai" in sys.modules:
        return

    g = T.ModuleType("google")
    g.__path__ = []
    genai = T.ModuleType("google.genai")
    genai.Client = lambda **kw: None
    gt = T.ModuleType("google.genai.types")

    class _Any:
        def __init__(self, *a, **k):
            pass

    gt.GenerateContentConfig = _Any
    gt.Tool = _Any
    gt.GoogleSearch = _Any
    gt.HttpOptions = _Any
    g.genai = genai
    genai.types = gt
    sys.modules.update({"google": g, "google.genai": genai, "google.genai.types": gt})

    rq = T.ModuleType("requests")

    class _Resp:
        status_code = 200
        content = b""
        text = ""

        def json(self):
            return {}

    rq.get = lambda *a, **k: _Resp()
    rq.post = lambda *a, **k: _Resp()
    rq.exceptions = T.SimpleNamespace(RequestException=Exception, Timeout=Exception)
    sys.modules["requests"] = rq
