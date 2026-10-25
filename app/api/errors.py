"""One error envelope for current frontend clients and native API consumers."""
from fastapi.responses import JSONResponse


def error_response(status, detail, *, code="request/rejected", headers=None):
    message = detail if isinstance(detail, str) else "request validation failed"
    return JSONResponse({"detail": detail, "error": {"code": code, "message": message}},
                        status_code=status, headers=headers)
