"""Render entry point that adds the authenticated backup export endpoint."""

import asyncio
import logging
import threading

import httpx

import server
from backfill_embeddings import backfill_batch
from backup_export import backup_payload_json, verify_github_oidc
from backup_v2_runtime import register_backup_v2_if_enabled


logger = logging.getLogger("ombre_brain.backup")


async def _authenticated_claims(request):
    authorization = request.headers.get("authorization", "")
    token = (
        authorization[7:].strip()
        if authorization.lower().startswith("bearer ")
        else ""
    )
    return await verify_github_oidc(token)


@server.mcp.custom_route("/api/backup/export", methods=["GET"])
async def backup_export_endpoint(request):
    from starlette.responses import JSONResponse, Response

    try:
        claims = await _authenticated_claims(request)
    except Exception as exc:
        logger.warning("Rejected backup export request: %s", exc)
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        payload = await asyncio.to_thread(
            backup_payload_json,
            server.config["buckets_dir"],
        )
    except Exception as exc:
        logger.exception("Backup export failed")
        return JSONResponse({"error": "backup_export_failed"}, status_code=500)

    logger.info(
        "Backup export completed for %s run %s",
        claims.get("repository"),
        claims.get("run_id"),
    )
    return Response(
        payload,
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


@server.mcp.custom_route("/api/embeddings/backfill", methods=["POST"])
async def embeddings_backfill_endpoint(request):
    from starlette.responses import JSONResponse

    try:
        claims = await _authenticated_claims(request)
    except Exception as exc:
        logger.warning("Rejected embedding backfill request: %s", exc)
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        body = await request.json()
        limit = int(body.get("limit", 20)) if isinstance(body, dict) else 20
        result = await backfill_batch(
            server.bucket_mgr,
            server.embedding_engine,
            limit=limit,
        )
    except Exception as exc:
        logger.exception("Embedding backfill failed")
        return JSONResponse({"error": "embedding_backfill_failed"}, status_code=500)

    logger.info(
        "Embedding backfill completed for %s run %s: %s",
        claims.get("repository"),
        claims.get("run_id"),
        result,
    )
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


def run() -> None:
    transport = server.config.get("transport", "stdio")
    logger.info("Ombre Brain starting with backup export | transport: %s", transport)

    register_backup_v2_if_enabled(server, transport)

    if transport not in ("sse", "streamable-http"):
        server.mcp.run(transport=transport)
        return

    import uvicorn

    async def keepalive_loop():
        await asyncio.sleep(10)
        async with httpx.AsyncClient() as client:
            while True:
                try:
                    await client.get(
                        f"http://localhost:{server.OMBRE_PORT}/health",
                        timeout=5,
                    )
                    logger.debug("Keepalive ping OK")
                except Exception as exc:
                    logger.warning("Keepalive ping failed: %s", exc)
                await asyncio.sleep(60)

    def start_keepalive():
        loop = asyncio.new_event_loop()
        loop.run_until_complete(keepalive_loop())

    threading.Thread(target=start_keepalive, daemon=True).start()

    if transport == "streamable-http":
        app = server.mcp.streamable_http_app()
    else:
        app = server.mcp.sse_app()
    server.add_mcp_auth_middleware(app)
    server.add_http_cors_middleware(app)
    server.install_uvicorn_access_log_redaction()
    uvicorn.run(app, host="0.0.0.0", port=server.OMBRE_PORT)


if __name__ == "__main__":
    run()
