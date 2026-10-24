import os

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse

from app.auth import get_uid
from app.github import oauth
from app.github.credentials import save_token

router = APIRouter(prefix="/auth/github")


@router.get("/start")
def start(uid: str = Depends(get_uid)):
    return RedirectResponse(oauth.build_authorize_url(uid))


@router.get("/callback")
def callback(code: str, state: str):
    # No auth dependency here - GitHub redirects the user's own browser straight
    # to this URL with no bearer token available. Recovering uid from the
    # Fernet-verified state IS the authentication for this leg.
    try:
        uid = oauth.resolve_state(state)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid or expired OAuth state")
    token = oauth.exchange_code_for_token(code)
    save_token(uid, token)
    return RedirectResponse(os.environ["GITHUB_OAUTH_SUCCESS_REDIRECT_URL"])
