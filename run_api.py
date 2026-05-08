import os

from dotenv import load_dotenv
import uvicorn

load_dotenv(override=True)

if __name__ == "__main__":
    # workers=1 ensures Playwright sync API stays on a single thread.
    # Playwright's sync_api uses greenlets and cannot be called from
    # different threads (uvicorn's default threadpool executor).
    uvicorn.run(
        "app.api:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 4000)),
        reload=False,
        workers=1,
    )
