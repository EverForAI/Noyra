"""Bounded HTTP adapter for public accounts, distinct from operator sessions."""

from __future__ import annotations

import json
from contextlib import suppress
from dataclasses import asdict
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qs, urlsplit

from noyra.core.errors import IntegrityError, InvalidTransitionError, NotFoundError

from .accounts import AccountError, AccountRateLimited, AccountStore, digest
from .posts import PublicPostCapacityError, PublicPostQueueFullError, PublicPostRateLimitError

COOKIE = "noyra_participant"


def _reply(h: Any, status: int, payload: Any, *, cookie: str | None = None) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    if len(body) > 2_000_000:
        status = 413
        body = b'{"error":"participant_response_too_large"}'
    h.send_response(status)
    if cookie is not None:
        h.send_header("Set-Cookie", cookie)
    if status == 429:
        h.send_header("Retry-After", "60")
    h._headers("application/json; charset=utf-8", len(body), cache_control="no-store")
    h.end_headers()
    with suppress(BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        h.wfile.write(body)


def _token(h: Any) -> tuple[str, bool]:
    auth = h.headers.get("Authorization", "")
    if auth:
        return (auth[7:], True) if auth.startswith("Bearer noyra_participant_") else ("", True)
    try:
        cookie = SimpleCookie()
        cookie.load(h.headers.get("Cookie", ""))
        return (cookie[COOKIE].value, False) if COOKIE in cookie else ("", False)
    except Exception:
        return "", False


def _same_origin(h: Any) -> bool:
    if h.headers.get("Sec-Fetch-Site", "") not in {"", "same-origin", "none"}:
        return False
    origin = h.headers.get("Origin")
    if not origin:
        return True
    parsed = urlsplit(origin)
    return parsed.scheme in {"http", "https"} and parsed.netloc == h.headers.get("Host", "")


def _cookie(owner: Any, value: str, *, clear: bool = False) -> str:
    return (
        f"{COOKIE}={value}; HttpOnly; SameSite=Strict; Path=/; Max-Age={0 if clear else 604800}"
        + ("; Secure" if owner.settings.admin_session_cookie_secure else "")
    )


def dispatch(h: Any, owner: Any, method: str) -> bool:
    parsed = urlsplit(getattr(h, "_request_target", h.path) if method == "POST" else h.path)
    path = parsed.path.replace("/api/v1/", "/api/", 1)
    if path == "/api/admin/participant-sources" and method == "POST":
        if not h._authorized("operator"):
            h._discard_small_request_body()
            _reply(h, 401, {"error": "unauthorized"})
            return True
        payload = h._request_json()
        if payload is None:
            return True
        try:
            if set(payload) != {"entity_id", "binding_id"}:
                raise ValueError("invalid_binding")
            from noyra.core.types import new_id, utc_now

            from .participants import ParticipantStore, source_namespace

            with owner.kernel.database.transaction() as c:
                binding = c.execute(
                    "SELECT * FROM interaction_bindings WHERE subject_id=? AND binding_id=? "
                    "AND status='active'",
                    (owner.kernel.subject_id, payload["binding_id"]),
                ).fetchone()
                if binding is None:
                    raise ValueError("binding_not_found")
                bound_who = ParticipantStore.source(
                    c,
                    owner.kernel.subject_id,
                    binding["channel"],
                    source_namespace(
                        binding["channel"], binding["transport_id"], binding["external_account_id"]
                    ),
                    binding["external_sender_id"],
                    entity_id=payload["entity_id"],
                )
                c.execute(
                    "INSERT INTO audit_records VALUES(?,?,?,?,?,?)",
                    (
                        new_id("audit"),
                        owner.kernel.subject_id,
                        "participant_source_bound",
                        "operator",
                        json.dumps(
                            {"entity_id": bound_who.entity_id, "binding_id": binding["binding_id"]}
                        ),
                        utc_now(),
                    ),
                )
            _reply(h, 200, {"entity_id": bound_who.entity_id, "source_id": bound_who.source_id})
        except (ValueError, NotFoundError):
            _reply(h, 409, {"error": "participant_source_conflict"})
        return True
    if not path.startswith("/api/participants/"):
        return False
    if not h._at_rest_available():
        if method == "POST":
            h._discard_small_request_body()
        _reply(h, 503, {"error": "at_rest_boundary_unavailable"})
        return True
    store = AccountStore(owner.kernel.database, owner.kernel.subject_id)
    token, api = _token(h)
    who = store.authenticate(token, api=api)
    try:
        query = parse_qs(parsed.query, keep_blank_values=True)
        if any(len(v) != 1 for v in query.values()):
            raise AccountError("invalid_query")
        if method == "GET":
            if path == "/api/participants/session":
                _reply(
                    h,
                    200,
                    {
                        "participant": asdict(who) if who else None,
                        "csrf_token": digest("csrf:" + token) if who and not api else None,
                    },
                )
            else:
                from .collaboration import ParticipantCollaboration

                if path == "/api/participants/opportunities":
                    data = ParticipantCollaboration(owner).opportunity_page(query)
                else:
                    if who is None:
                        raise PermissionError("participant_login_required")
                    data = ParticipantCollaboration(owner).read(path, query, who)
                _reply(h, 200, data)
            return True
        if query:
            raise AccountError("invalid_query")
        if h.headers.get_content_type() != "application/json":
            h._discard_small_request_body()
            _reply(h, 415, {"error": "json_required"})
            return True
        if not _same_origin(h):
            h._discard_small_request_body()
            raise PermissionError("same_origin_required")
        payload = h._request_json()
        if payload is None:
            return True
        client = owner.client_ip(str(h.client_address[0]), h.headers.get("X-Forwarded-For"))
        public = path.rsplit("/", 1)[-1]
        if path != "/api/participants/" + public:
            raise LookupError("unknown_participant_route")
        if public in {"register", "login", "recover"}:
            store.throttle(client, public, 5 if public == "register" else 20)
            username = payload.get("username", "")
            if not isinstance(username, str):
                raise AccountError("invalid_account_details")
            store.throttle(username.strip().lower(), public + ":name", 10)
            if public == "register":
                allowed = {
                    "username",
                    "password",
                    "display_name",
                    "entity_type",
                    "captcha_id",
                    "captcha_answer",
                }
                if set(payload) - allowed:
                    raise AccountError("invalid_account_details")
                owner.public_posts.verify_captcha(
                    owner.kernel.subject_id,
                    client_ip=client,
                    challenge_id=payload.get("captcha_id"),
                    answer=payload.get("captcha_answer"),
                )
                data = store.register(
                    username,
                    payload.get("password", ""),
                    payload.get("display_name", ""),
                    kind=payload.get("entity_type", "human"),
                )
                _reply(h, 201, data)
            elif public == "login":
                if set(payload) - {"username", "password"}:
                    raise AccountError("invalid_account_details")
                token, csrf, data = store.login(username, payload.get("password", ""))
                _reply(
                    h, 200, {"participant": data, "csrf_token": csrf}, cookie=_cookie(owner, token)
                )
            else:
                if set(payload) - {"username", "recovery_code", "password"}:
                    raise AccountError("invalid_account_details")
                code = store.reset(
                    username, payload.get("recovery_code", ""), payload.get("password", "")
                )
                _reply(h, 200, {"recovery_code": code}, cookie=_cookie(owner, "", clear=True))
            return True
        if who is None:
            raise PermissionError("participant_login_required")
        if not api and not store.authenticate(token, csrf=h.headers.get("X-Participant-CSRF", "")):
            raise PermissionError("participant_csrf_required")
        store.throttle(who.entity_id, "writes", 120)
        if public == "logout":
            if api:
                raise PermissionError("browser_session_required")
            store.logout(token)
            _reply(h, 200, {"ok": True}, cookie=_cookie(owner, "", clear=True))
        elif public == "password":
            if api:
                raise PermissionError("browser_session_required")
            if set(payload) != {"current_password", "password"}:
                raise AccountError("invalid_account_details")
            code = store.change_password(who, payload["current_password"], payload["password"])
            _reply(h, 200, {"recovery_code": code}, cookie=_cookie(owner, "", clear=True))
        elif public == "credential":
            if api:
                raise PermissionError("browser_session_required")
            if set(payload) - {"revoke"} or (
                "revoke" in payload and type(payload["revoke"]) is not bool
            ):
                raise AccountError("invalid_account_details")
            _reply(h, 200, {"api_key": store.api_key(who, revoke=payload.get("revoke") is True)})
        else:
            from .collaboration import ParticipantCollaboration

            _reply(h, 200, ParticipantCollaboration(owner).write(path, payload, who, client))
    except PublicPostRateLimitError:
        _reply(h, 429, {"error": "public_post_rate_limited"})
    except (PublicPostCapacityError, PublicPostQueueFullError):
        _reply(h, 503, {"error": "public_post_capacity_full"})
    except AccountRateLimited as error:
        _reply(h, 429, {"error": str(error)})
    except AccountError as error:
        _reply(h, 401 if str(error) == "invalid_credentials" else 400, {"error": str(error)})
    except PermissionError as error:
        _reply(h, 401 if who is None else 403, {"error": str(error)})
    except (LookupError, NotFoundError):
        _reply(h, 404, {"error": "participant_item_not_found"})
    except IntegrityError:
        _reply(h, 503, {"error": "participant_integrity_unavailable"})
    except InvalidTransitionError:
        _reply(h, 409, {"error": "participant_state_changed"})
    except (ValueError, TypeError, KeyError, AttributeError):
        _reply(h, 400, {"error": "invalid_participant_request"})
    return True
