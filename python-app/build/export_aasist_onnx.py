"""Reproducibly build src/noc_beam/audio/models/aasist.onnx.

Source : clovaai/aasist (MIT) -- the original AASIST paper authors.
Why    : no official AASIST ONNX exists; we export the MIT-licensed
         PyTorch checkpoint ourselves to a contract NOC_Beam's
         AasistDetector consumes.

Security:
  * Downloads are pinned by SHA-256 (verified before use).
  * The checkpoint is loaded with weights_only=True -- only tensors are
    deserialized, no embedded code can execute.
  * torch is required ONLY to run this script; it is NEVER bundled into
    NOC_Beam. The app runs aasist.onnx via onnxruntime alone.

Run (in a throwaway venv with torch+onnx+onnxruntime+onnxscript):
    python build/export_aasist_onnx.py

Output contract (matches audio/fas_models.py AasistDetector):
    input  : float32, shape (batch, 64600), raw 16 kHz mono waveform
    output : float32 logits, shape (batch, 2)
             index 0 = spoof, index 1 = bonafide
             (verified vs clovaai main.py: batch_out[:, 1] is the
              bonafide / CM score). Written as a SINGLE self-contained
              .onnx (weights inline) so there is no .onnx.data sidecar to
              lose, and older onnxruntime builds load it without external-
              data path validation failures.
"""
import hashlib
import sys
import urllib.request
from pathlib import Path

import numpy as np
import onnx
import torch
import torch.nn as nn

RAW = "https://raw.githubusercontent.com/clovaai/aasist/main"
ARCH_URL = f"{RAW}/models/AASIST.py"
CKPT_URL = f"{RAW}/models/weights/AASIST.pth"
ARCH_SHA = "9e0d3e80937dd0577beea7883098465a479da23a198ebc0d712abcc59b0bec50"
CKPT_SHA = "51d2d9cf0738172f61e2a384ec50a54a55363240f67c971ed55a92435bc1a1c0"

D_ARGS = {"architecture": "AASIST", "nb_samp": 64600, "first_conv": 128,
          "filts": [70, [1, 32], [32, 32], [32, 64], [64, 64]],
          "gat_dims": [64, 32], "pool_ratios": [0.5, 0.7, 0.5, 0.5],
          "temperatures": [2.0, 2.0, 100.0, 100.0]}
N = 64600
WORK = Path(__file__).parent / "_aasist_work"
OUT = Path(__file__).parent.parent / "src" / "noc_beam" / "audio" / "models" / "aasist.onnx"


def fetch(url: str, dest: Path, want_sha: str) -> None:
    if not dest.exists():
        urllib.request.urlretrieve(url, dest)
    got = hashlib.sha256(dest.read_bytes()).hexdigest()
    if got != want_sha:
        raise SystemExit(f"SHA mismatch for {dest.name}: got {got} want {want_sha}")
    print(f"verified {dest.name} sha256={got}")


def main() -> None:
    WORK.mkdir(exist_ok=True)
    (WORK / "models").mkdir(exist_ok=True)
    (WORK / "models" / "__init__.py").write_text("")
    fetch(ARCH_URL, WORK / "models" / "AASIST.py", ARCH_SHA)
    fetch(CKPT_URL, WORK / "AASIST.pth", CKPT_SHA)

    sys.path.insert(0, str(WORK))
    from models.AASIST import Model

    sd = torch.load(WORK / "AASIST.pth", map_location="cpu", weights_only=True)
    if not isinstance(sd, dict):
        raise SystemExit("checkpoint is not a state_dict")
    model = Model(D_ARGS)
    model.load_state_dict(sd)
    model.eval()

    class Wrap(nn.Module):
        def __init__(self, m):
            super().__init__()
            self.m = m

        def forward(self, x):
            _, out = self.m(x)
            return out

    wrap = Wrap(model)
    wrap.eval()
    tmp = WORK / "aasist_raw.onnx"
    with torch.no_grad():
        torch.onnx.export(
            wrap, torch.zeros(1, N), str(tmp),
            input_names=["input"], output_names=["logits"],
            dynamic_axes={"input": {0: "batch"}, "logits": {0: "batch"}},
            opset_version=17,
        )
    # Force all weights inline -> single self-contained file (no sidecar).
    onnx.save_model(onnx.load(str(tmp)), str(OUT), save_as_external_data=False)

    import onnxruntime as ort
    sess = ort.InferenceSession(str(OUT), providers=["CPUExecutionProvider"])
    iname = sess.get_inputs()[0].name
    rng = np.random.default_rng(7)
    max_diff = 0.0
    for _ in range(5):
        x = rng.standard_normal((1, N)).astype(np.float32)
        with torch.no_grad():
            _, t = model(torch.from_numpy(x))
        o = sess.run(None, {iname: x})[0]
        max_diff = max(max_diff, float(np.max(np.abs(t.numpy() - o))))
    if max_diff >= 1e-3:
        raise SystemExit(f"PARITY FAIL: onnx vs torch max|diff|={max_diff:.2e}")
    print(f"parity OK (max|diff|={max_diff:.2e})")
    print(f"wrote {OUT} ({OUT.stat().st_size:,} B)")
    print(f"aasist.onnx sha256={hashlib.sha256(OUT.read_bytes()).hexdigest()}")


if __name__ == "__main__":
    main()
