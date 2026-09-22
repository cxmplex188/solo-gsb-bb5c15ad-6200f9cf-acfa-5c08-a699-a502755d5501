"""FastAPI 应用入口。"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status

from . import __version__
from .db import Database, utcnow
from .matcher import validate
from .models import (
    ErrorResponse,
    ReferenceOut,
    ReferenceUpsert,
    ValidateRequest,
    ValidateResponse,
)
from .seed import SEED_REFERENCES


def _payload_hash(req: ValidateRequest) -> str:
    """请求体的规范化哈希：同一 submission_id 必须对应同一载荷。"""
    canonical = json.dumps(
        {
            "char": req.char,
            "strokes": req.strokes,
            "config": req.config.model_dump(),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def create_app(db_path: str | None = None, seed_demo_data: bool | None = None) -> FastAPI:
    db = Database(db_path or os.environ.get("DATABASE_PATH", "./strokes.db"))
    if seed_demo_data is None:
        seed_demo_data = os.environ.get("SEED_DEMO_DATA", "1") != "0"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if seed_demo_data and not db.list_references():
            for char, strokes in SEED_REFERENCES.items():
                db.upsert_reference(char, strokes)
        yield
        db.close()

    app = FastAPI(
        title="汉字笔顺轨迹校验 API",
        description=(
            "接收汉字参考笔画序列与用户书写轨迹，按**笔画方向、起止点、笔顺**"
            "做归一化匹配，支持可配置的分叉/拐角笔画容差，返回首个不合格笔画、"
            "偏差原因与整体得分。相同 `submission_id` 的提交幂等。"
        ),
        version=__version__,
        lifespan=lifespan,
    )

    # ---------------- 健康检查 ----------------

    @app.get("/health", tags=["meta"], summary="健康检查")
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    # ---------------- 参考字库 ----------------

    @app.put(
        "/references/{char}",
        response_model=ReferenceOut,
        tags=["references"],
        summary="登记或覆盖某字的参考笔画（幂等）",
    )
    def put_reference(char: str, body: ReferenceUpsert) -> dict:
        if len(char) != 1:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "char 必须是单个汉字")
        rec = db.upsert_reference(char, body.strokes)
        return {**rec, "stroke_count": len(rec["strokes"])}

    @app.get(
        "/references/{char}",
        response_model=ReferenceOut,
        responses={404: {"model": ErrorResponse}},
        tags=["references"],
        summary="查询某字的参考笔画",
    )
    def get_reference(char: str) -> dict:
        rec = db.get_reference(char)
        if rec is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"未登记参考字: {char}")
        return {**rec, "stroke_count": len(rec["strokes"])}

    @app.get("/references", tags=["references"], summary="列出已登记的参考字")
    def list_references() -> dict:
        return {"items": db.list_references()}

    @app.delete(
        "/references/{char}",
        responses={404: {"model": ErrorResponse}},
        tags=["references"],
        summary="删除某字的参考笔画",
    )
    def delete_reference(char: str) -> dict:
        if not db.delete_reference(char):
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"未登记参考字: {char}")
        return {"deleted": char}

    # ---------------- 笔顺校验 ----------------

    @app.post(
        "/validate",
        response_model=ValidateResponse,
        responses={
            404: {"model": ErrorResponse, "description": "参考字未登记"},
            409: {"model": ErrorResponse, "description": "幂等键冲突（同 id 不同载荷）"},
        },
        tags=["validation"],
        summary="校验一次书写提交（幂等）",
    )
    def post_validate(req: ValidateRequest) -> dict:
        payload_hash = _payload_hash(req)

        existing = db.get_submission(req.submission_id)
        if existing is not None:
            if existing["payload_hash"] != payload_hash:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "submission_id 已存在且载荷不同，请更换幂等键",
                )
            return {**existing["result"], "replayed": True}

        ref = db.get_reference(req.char)
        if ref is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"未登记参考字: {req.char}，请先 PUT /references/{req.char}",
            )

        result = validate(ref["strokes"], req.strokes, req.config)
        record = {
            "submission_id": req.submission_id,
            "char": req.char,
            "passed": result.passed,
            "score": result.score,
            "first_failed_stroke": result.first_failed_stroke,
            "deviation_reasons": result.deviation_reasons,
            "expected_strokes": len(ref["strokes"]),
            "received_strokes": len(req.strokes),
            "stroke_results": result.stroke_results,
            "created_at": utcnow(),
        }
        stored = db.create_submission(req.submission_id, payload_hash, req.char, record)
        # 并发下同 id 已被写入：以库存记录为准
        if stored["payload_hash"] != payload_hash:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "submission_id 已存在且载荷不同，请更换幂等键",
            )
        replayed = stored["created_at"] != record["created_at"]
        return {**stored["result"], "replayed": replayed}

    @app.get(
        "/submissions/{submission_id}",
        response_model=ValidateResponse,
        responses={404: {"model": ErrorResponse}},
        tags=["validation"],
        summary="按幂等键查询历史校验结果",
    )
    def get_submission(submission_id: str) -> dict:
        rec = db.get_submission(submission_id)
        if rec is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"提交不存在: {submission_id}")
        return {**rec["result"], "replayed": True}

    return app


app = create_app()
