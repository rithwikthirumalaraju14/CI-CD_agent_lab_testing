import json
from types import ModuleType
from zipimport import zipimporter

def run(request, root):
    version = json.loads((root / "context/images.json").read_text())[request["runner"]]
    wheel = root / "vendor/wheels" / f"wirekit-{version}-py3-none-any.whl"
    sdk = ModuleType("wirekit")
    exec(zipimporter(str(wheel)).get_code("wirekit"), sdk.__dict__)
    timeout = request["timeout"]
    if sdk.__version__.startswith("1."):
        client = sdk.Client(token="fixture", timeout=timeout)
    else:
        client = sdk.Client(credential="fixture", timeout_seconds=timeout)
    page = client.page({"0": request["payload"]}, 0)
    return {"version": sdk.__version__, "page": page}
