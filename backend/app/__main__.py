import os
import sys

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        reload="--reload" in sys.argv,
        # psycopg's async driver cannot run on Windows' default Proactor loop.
        loop="asyncio:SelectorEventLoop" if sys.platform == "win32" else "auto",
    )
