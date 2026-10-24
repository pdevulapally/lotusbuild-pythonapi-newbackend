import os

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.auth import get_uid
from app.github import oauth
from app.github.credentials import save_token

router = APIRouter(prefix="/auth/github")
BINDING_COOKIE = "__Host-lotus_github_oauth"


@router.post("/start")
def start(uid: str = Depends(get_uid)):
    # The frontend must make this credentialed request on the API origin (for
    # example through a same-origin reverse proxy), then navigate to the URL.
    # Do not turn this into a redirecting bearer-auth fetch, or put the binding
    # secret in the JSON response. Cross-origin CORS credentials stay disabled
    # until explicit deployment origins and cookie behavior are tested.
    url, browser_secret = oauth.begin_authorization(uid)
    response = JSONResponse({"url": url}, headers={"Cache-Control": "no-store"})
    response.set_cookie(
        BINDING_COOKIE,
        browser_secret,
        max_age=oauth.STATE_TTL_SECONDS,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


@router.get("/callback")
def callback(
    request: Request,
    code: str = Query(min_length=1, max_length=2048),
    state: str = Query(min_length=43, max_length=43),
):
    try:
        uid = oauth.resolve_state(state, request.cookies.get(BINDING_COOKIE))
    except ValueError:
        response = JSONResponse({"detail": "invalid or expired OAuth state"}, status_code=400)
    else:
        try:
            token = oauth.exchange_code_for_token(code)
            save_token(uid, token)
        except Exception:
            # Do not return provider payloads, authorization codes or tokens.
            response = JSONResponse(
                {"detail": "GitHub linking failed; start a new connection attempt"}, status_code=502
            )
        else:
            response = RedirectResponse(os.environ["GITHUB_OAUTH_SUCCESS_REDIRECT_URL"], status_code=303)
    response.headers["Cache-Control"] = "no-store"
    response.delete_cookie(BINDING_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return response
