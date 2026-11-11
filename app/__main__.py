import os

if __name__ == "__main__":
    # Only in the server process: page-parse workers import this module too.
    from alpharidge_ai.packages import repair_substrate_codec

    repair_substrate_codec()

    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=os.getenv("API_HOST", "0.0.0.0"),
        port=int(os.getenv("API_PORT", "8000")),
        reload=os.getenv("API_RELOAD", "false").lower() == "true",
        access_log=False,
    )
