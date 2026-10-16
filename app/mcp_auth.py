# -*- coding: utf-8 -*-
"""
MCP Authentication & Token Verification for Sewa Setu.

Enforces:
1. Verification against the portal's JWKS using RS256.
2. Strict check of 'iss' against the portal's base URL.
3. Strict check of 'aud' against the specific MCP endpoint URL.
4. Strict check of 'scope' (citizen vs officer).
5. Citizen mobile extracted ONLY from verified sub: citizen:<10-digit-mobile>.
6. Officer identity extracted ONLY from verified sub: officer:<username>.
"""

import os
import re
import json
import jwt
from jwt import PyJWKClient, PyJWTError

ISSUER = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")


class AuthenticationError(Exception):
    """Raised when an MCP request fails authentication or authorization checks."""
    pass


def get_jwks():
    """Retrieve the portal's JWKS. In-process or via HTTP."""
    try:
        import app
        import oauth
        return oauth.public_jwks()
    except Exception as e:
        raise AuthenticationError("Could not load JWKS: %s" % e)


def verify_mcp_token(token, expected_audience, required_scope, required_role):
    """
    Verify an incoming bearer token for an MCP server.
    
    Args:
        token (str): JWT string.
        expected_audience (str): Canonical URL of the MCP endpoint (e.g. http://localhost:8000/mcp/citizen).
        required_scope (str): Required scope, 'citizen' or 'officer'.
        required_role (str): Required role, 'citizen' or 'officer'.
        
    Returns:
        dict: The verified token claims, augmented with 'mobile' (for citizen)
              or 'username' (for officer).
              
    Raises:
        AuthenticationError: If token is missing, invalid, expired, or claims do not match.
    """
    if not token or not isinstance(token, str):
        raise AuthenticationError("Missing or invalid token")

    token = token.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()

    try:
        unverified_header = jwt.get_unverified_header(token)
    except PyJWTError as e:
        raise AuthenticationError("Invalid token header: %s" % e)

    kid = unverified_header.get("kid")
    alg = unverified_header.get("alg")
    if alg != "RS256":
        raise AuthenticationError("Unsupported algorithm: %s (expected RS256)" % alg)

    jwks = get_jwks()
    keys = jwks.get("keys", [])
    matching_key = None
    for k in keys:
        if k.get("kid") == kid:
            matching_key = k
            break
            
    if not matching_key and keys:
        # Fall back to single key if only one exists and kid wasn't set
        if len(keys) == 1:
            matching_key = keys[0]

    if not matching_key:
        raise AuthenticationError("Key ID (kid) '%s' not found in JWKS" % kid)

    try:
        public_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(matching_key))
    except Exception as e:
        raise AuthenticationError("Failed to parse public key from JWKS: %s" % e)

    # Verify signature, expiration, issuer, and audience
    try:
        claims = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            issuer=ISSUER,
            audience=expected_audience,
            options={"require": ["exp", "iss", "aud", "sub"]}
        )
    except jwt.ExpiredSignatureError:
        raise AuthenticationError("Token has expired")
    except jwt.InvalidIssuerError:
        raise AuthenticationError("Invalid token issuer")
    except jwt.InvalidAudienceError:
        raise AuthenticationError("Invalid token audience for this endpoint")
    except PyJWTError as e:
        raise AuthenticationError("Token verification failed: %s" % e)

    # Verify scope
    scopes = (claims.get("scope") or "").split()
    if required_scope not in scopes:
        raise AuthenticationError(
            "Insufficient scope: required '%s', got '%s'" % (required_scope, claims.get("scope"))
        )

    # Verify role
    role = claims.get("role")
    if role != required_role:
        raise AuthenticationError(
            "Role mismatch: required '%s', got '%s'" % (required_role, role)
        )

    # Verify and parse subject
    sub = claims.get("sub", "")
    if required_role == "citizen":
        match = re.match(r"^citizen:(\d{10})$", sub)
        if not match:
            raise AuthenticationError(
                "Invalid citizen subject format '%s' (expected citizen:<10-digit-mobile>)" % sub
            )
        claims["mobile"] = match.group(1)
    elif required_role == "officer":
        match = re.match(r"^officer:([a-zA-Z0-9_\-\.]+)$", sub)
        if not match:
            raise AuthenticationError(
                "Invalid officer subject format '%s' (expected officer:<username>)" % sub
            )
        claims["username"] = match.group(1)
    else:
        raise AuthenticationError("Unknown role: %s" % required_role)

    return claims
