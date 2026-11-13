"""Stateless Mac inference service; contains no camera or robot-control code.

Run with INFERENCE_TOKEN set, for example::

    python inference_worker.py --model best.pt --host 0.0.0.0 --port 8090

The default bind is loopback. LAN binding must be explicitly requested. Use a
trusted isolated LAN, SSH forwarding, or a TLS reverse proxy for the HTTP token.
"""

import argparse
import asyncio
import hmac
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from inference_protocol import (InferenceError, InferenceTimeout, KEEP_CLASSES,
                                MAX_REQUEST_BYTES, ProtocolError, decode_request,
                                load_json, make_response, make_settings,
                                model_sha256, validate_settings)


class ModelRunner:
    """CPU-only YOLO runner instantiated exclusively inside a child process."""

    def __init__(self, model_path, settings, threads):
        validate_settings(settings)
        path = Path(model_path).resolve()
        self.model_hash = model_sha256(path)  # Missing files never become download requests.
        self.settings = dict(settings)
        os.environ["YOLO_AUTOINSTALL"] = "false"
        os.environ["OMP_NUM_THREADS"] = str(threads)
        os.environ["MKL_NUM_THREADS"] = str(threads)
        import numpy as np
        import torch
        from ultralytics import YOLO
        torch.set_num_threads(threads)
        torch.set_num_interop_threads(min(threads, 2))
        self.model = YOLO(str(path), task="segment")
        if model_sha256(path) != self.model_hash:
            raise InferenceError("Segmentation weights changed while loading")
        names = self.model.names
        if not isinstance(names, dict) or not set(KEEP_CLASSES).issubset(set(names.values())):
            raise InferenceError("Segmentation weights do not contain the required cup classes")
        self.classes = [index for index, name in names.items() if name in KEEP_CLASSES]
        self._predict_image(np.zeros((settings["imgsz"], settings["imgsz"], 3), dtype=np.uint8))

    def _predict_image(self, image):
        options = {key: value for key, value in self.settings.items()
                   if key not in ("classes", "input_format")}
        return self.model.predict(image, classes=self.classes, verbose=False, **options)[0]

    def predict(self, request):
        if request.get("model_sha256") != self.model_hash or request.get("settings") != self.settings:
            raise ProtocolError("Inference request does not match worker weights/settings")
        image = decode_request(request)
        started = time.monotonic()
        result = self._predict_image(image)
        elapsed = (time.monotonic() - started) * 1000
        detections = []
        masks = [] if result.masks is None else result.masks.data
        if len(result.boxes) != len(masks):
            raise ProtocolError("Segmentation model returned unmatched masks and boxes")
        for box, mask in zip(result.boxes, masks):
            import numpy as np
            mask = mask.cpu().numpy()
            if not np.isfinite(mask).all():
                raise ProtocolError("Segmentation model returned a nonfinite mask")
            mask = mask.astype(bool)
            if mask.shape != image.shape[:2]:
                raise ProtocolError("Segmentation model returned a mask at the wrong resolution")
            if not mask.any():
                continue
            detections.append({"class_name": self.model.names[int(box.cls.item())],
                "confidence": float(box.conf.item()), "bbox_xyxy": box.xyxy[0].tolist(),
                "mask": mask})
        return make_response(request, detections, inference_ms=elapsed)


def create_app(backend, token, request_timeout_s=10):
    """Build a service around a backend; injectable for hardware-free tests."""
    from fastapi import FastAPI, HTTPException, Request
    from starlette.responses import JSONResponse
    if (not isinstance(token, str) or not token or len(token) > 4096 or
            any(ord(char) < 33 or ord(char) > 126 for char in token)):
        raise InferenceError("INFERENCE_TOKEN must contain a nonempty printable token")
    from inference_backends import _deadline
    _deadline(request_timeout_s)
    busy = threading.Lock()
    token_bytes = ("Bearer " + token).encode("ascii")

    @asynccontextmanager
    async def lifespan(app):
        try:
            await asyncio.to_thread(backend.start)
            yield
        finally:
            await asyncio.to_thread(backend.close)

    app = FastAPI(title="Cup segmentation worker", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)

    def authenticate(request):
        supplied = request.headers.get("authorization", "").encode("utf-8")
        if not hmac.compare_digest(supplied, token_bytes):
            raise HTTPException(status_code=401, detail="Inference authentication required")

    @app.get("/health")
    async def health(request: Request):
        authenticate(request)
        ready = backend.ready
        return JSONResponse({"ready": ready, "protocol_version": 1}, status_code=200 if ready else 503)

    @app.post("/infer")
    async def infer(request: Request):
        authenticate(request)
        if not busy.acquire(blocking=False):
            raise HTTPException(status_code=503, detail="Inference worker is busy")
        try:
            if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise HTTPException(status_code=415, detail="Inference requires application/json")
            if request.headers.get("content-encoding", "identity").lower() != "identity":
                raise HTTPException(status_code=415, detail="Compressed request bodies are unsupported")
            length = request.headers.get("content-length")
            if length is not None and (len(length) > 10 or not length.isdecimal() or int(length) > MAX_REQUEST_BYTES):
                raise HTTPException(status_code=413, detail="Inference request exceeds size limit")
            body = bytearray()
            started = time.monotonic()
            async def read_body():
                async for chunk in request.stream():
                    if len(body) + len(chunk) > MAX_REQUEST_BYTES:
                        raise HTTPException(status_code=413, detail="Inference request exceeds size limit")
                    body.extend(chunk)
            try:
                await asyncio.wait_for(read_body(), timeout=request_timeout_s)
                payload = load_json(bytes(body), MAX_REQUEST_BYTES)
                # The backend validates dimensions, hashes and model/configuration
                # before inference. The same budget includes reading the body.
                remaining = request_timeout_s - (time.monotonic() - started)
                if remaining <= 0:
                    raise InferenceTimeout("Inference request exceeded its total deadline")
                result = await asyncio.to_thread(backend.predict, payload, remaining)
                return JSONResponse(result)
            except (InferenceTimeout, asyncio.TimeoutError):
                raise HTTPException(status_code=504, detail="Inference deadline exceeded") from None
            except ProtocolError:
                raise HTTPException(status_code=422, detail="Invalid inference request or result") from None
            except InferenceError:
                raise HTTPException(status_code=503, detail="Inference worker unavailable") from None
        finally:
            busy.release()

    return app


def main():
    parser = argparse.ArgumentParser(description="Run the stateless authenticated Mac inference worker")
    parser.add_argument("--model", type=Path, required=True, help="Existing local segmentation weights (.pt)")
    parser.add_argument("--host", default="127.0.0.1", help="Explicitly use 0.0.0.0 to allow LAN connections")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--threads", type=int, default=3)
    parser.add_argument("--conf", type=float, default=0.5)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--request-timeout", type=float, default=10,
                        help="Total request-body and inference deadline in seconds (greater than 0, at most 300)")
    args = parser.parse_args()
    token = os.environ.get("INFERENCE_TOKEN", "")
    if not token:
        parser.error("Set INFERENCE_TOKEN before starting the worker")
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535")
    from inference_backends import ProcessBackend
    try:
        backend = ProcessBackend(args.model, make_settings(args.conf, args.imgsz), threads=args.threads)
        app = create_app(backend, token, args.request_timeout)
    except InferenceError as error:
        parser.error(str(error))
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, workers=1, limit_concurrency=8,
                timeout_keep_alive=5, access_log=False)


if __name__ == "__main__":
    main()
