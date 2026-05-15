"""Lambda@Edge viewer-request handler — JWT authentication for CloudFront.

SAMPLE CODE — NOT INTENDED FOR PRODUCTION USE.
This code is provided as a reference implementation only.

Configuration is loaded from SSM Parameter Store on cold start and cached.
JWT tokens are verified using pure-Python RSA PKCS#1 v1.5 (stdlib only).
"""

import base64
import hashlib
import hmac as hmac_mod
import json
import logging
import re
import time
import urllib.parse
import urllib.request

# ── Logging ──
# Initial level is INFO; adjusted after SSM config is loaded (see load_config).
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ── SSM configuration ──
SSM_REGION = "__SSM_REGION__"
SSM_PARAM_NAME = "__SSM_PARAM_NAME__"

_config = None


def load_config():
    """Fetch the auth config from SSM Parameter Store (once per cold start).

    Uses raw HTTPS + SigV4 via the Lambda execution role's credentials.
    """
    global _config
    if _config is not None:
        return _config

    # Lambda@Edge does not support *custom* environment variables, but the
    # runtime still injects reserved credential variables (AWS_ACCESS_KEY_ID,
    # AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN).  We read them via the C-level
    # environ mapping to avoid pulling in the full ``os`` module.
    from os import environ as _environ

    aws_access_key_id = _environ.get("AWS_ACCESS_KEY_ID", "")
    aws_secret_access_key = _environ.get("AWS_SECRET_ACCESS_KEY", "")
    aws_session_token = _environ.get("AWS_SESSION_TOKEN", "")

    host = "ssm.%s.amazonaws.com" % SSM_REGION
    request_body = json.dumps({"Name": SSM_PARAM_NAME})

    now = time.gmtime()
    amz_date = time.strftime("%Y%m%dT%H%M%SZ", now)
    date_stamp = time.strftime("%Y%m%d", now)

    def _sha256(data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        return hashlib.sha256(data).hexdigest()

    def _hmac(key, data):
        if isinstance(key, str):
            key = key.encode("utf-8")
        if isinstance(data, str):
            data = data.encode("utf-8")
        return hmac_mod.new(key, data, hashlib.sha256).digest()

    # Build SigV4 signature for POST request to SSM JSON API
    method = "POST"
    service = "ssm"
    canonical_uri = "/"
    canonical_querystring = ""
    payload_hash = _sha256(request_body)

    header_names = ["content-type", "host", "x-amz-date", "x-amz-target"]
    if aws_session_token:
        header_names.append("x-amz-security-token")
    header_names.sort()
    signed_headers = ";".join(header_names)

    canonical_headers_obj = {
        "content-type": "application/x-amz-json-1.1",
        "host": host,
        "x-amz-date": amz_date,
        "x-amz-target": "AmazonSSM.GetParameter",
    }
    if aws_session_token:
        canonical_headers_obj["x-amz-security-token"] = aws_session_token

    canonical_headers = (
        "\n".join("%s:%s" % (h, canonical_headers_obj[h]) for h in header_names)
        + "\n"
    )

    canonical_request = "\n".join(
        [
            method,
            canonical_uri,
            canonical_querystring,
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )
    credential_scope = "%s/%s/%s/aws4_request" % (date_stamp, SSM_REGION, service)
    string_to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, credential_scope, _sha256(canonical_request)]
    )

    signing_key = _hmac("AWS4" + aws_secret_access_key, date_stamp)
    signing_key = _hmac(signing_key, SSM_REGION)
    signing_key = _hmac(signing_key, service)
    signing_key = _hmac(signing_key, "aws4_request")
    signature = hmac_mod.new(
        signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    auth_header = (
        "AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s"
        % (aws_access_key_id, credential_scope, signed_headers, signature)
    )

    req_headers = {
        "Content-Type": "application/x-amz-json-1.1",
        "X-Amz-Target": "AmazonSSM.GetParameter",
        "X-Amz-Date": amz_date,
        "Host": host,
        "Authorization": auth_header,
    }
    if aws_session_token:
        req_headers["X-Amz-Security-Token"] = aws_session_token

    url = "https://%s/" % host
    req = urllib.request.Request(
        url, data=request_body.encode("utf-8"), headers=req_headers, method="POST"
    )
    # Scheme is guaranteed https by construction above; validate defensively
    if not url.startswith("https://"):
        raise ValueError("SSM endpoint URL must use HTTPS")
    resp = urllib.request.urlopen(req, timeout=3)
    body = resp.read().decode("utf-8")

    ssm_response = json.loads(body)
    if (
        not ssm_response.get("Parameter")
        or not ssm_response["Parameter"].get("Value")
    ):
        raise ValueError(
            "SSM parameter %s not found or empty" % SSM_PARAM_NAME
        )

    _config = json.loads(ssm_response["Parameter"]["Value"])
    logger.info(
        "Loaded config from SSM: region=%s userPoolId=%s",
        _config.get("cognitoRegion"),
        _config.get("userPoolId"),
    )

    # Apply log level from config (e.g. "DEBUG", "WARNING"); default to INFO.
    configured_level = _config.get("logLevel", "INFO").upper()
    logging.getLogger().setLevel(getattr(logging, configured_level, logging.INFO))
    logger.debug("Log level set to %s from SSM config", configured_level)

    return _config


# ── JWT verification ──

JWKS_TTL = 3600  # 1 hour in seconds
_jwks_cache = None
_jwks_cache_time = 0

# SHA-256 DigestInfo ASN.1 prefix (RFC 8017)
_SHA256_DIGEST_INFO_PREFIX = (
    b"\x30\x31\x30\x0d\x06\x09\x60\x86\x48\x01"
    b"\x65\x03\x04\x02\x01\x05\x00\x04\x20"
)


def base64url_decode(s):
    """Decode a base64url-encoded string (no padding required)."""
    s = s.replace("-", "+").replace("_", "/")
    # Add padding if needed
    pad = 4 - len(s) % 4
    if pad != 4:
        s += "=" * pad
    return base64.b64decode(s)


def _verify_rsa_sha256(message_bytes, signature_bytes, jwk):
    """PKCS#1 v1.5 RSA-SHA256 verification using stdlib only.

    Implements textbook RSA verification: sig^e mod n, then checks
    PKCS#1 v1.5 padding with SHA-256 DigestInfo.
    """
    # Extract n, e from JWK (base64url-decode to int)
    n = int.from_bytes(base64url_decode(jwk["n"]), "big")
    e = int.from_bytes(base64url_decode(jwk["e"]), "big")

    # RSA "decrypt" the signature: m = sig^e mod n
    sig_int = int.from_bytes(signature_bytes, "big")
    decrypted = pow(sig_int, e, n)

    # Convert back to bytes (key length)
    key_len = (n.bit_length() + 7) // 8
    em = decrypted.to_bytes(key_len, "big")

    # Verify PKCS#1 v1.5 padding: 0x00 0x01 [0xFF...] 0x00 [DigestInfo+Hash]
    digest = hashlib.sha256(message_bytes).digest()
    digest_info = _SHA256_DIGEST_INFO_PREFIX + digest

    expected_suffix = b"\x00" + digest_info
    padding_len = key_len - len(expected_suffix) - 2
    if padding_len < 8:
        return False
    expected = b"\x00\x01" + (b"\xff" * padding_len) + expected_suffix

    return em == expected


def fetch_jwks(cognito_region, user_pool_id):
    """Fetch JWKS from Cognito, cache for 1 hour."""
    global _jwks_cache, _jwks_cache_time
    now = time.time()
    if _jwks_cache is not None and (now - _jwks_cache_time) < JWKS_TTL:
        return _jwks_cache

    url = (
        "https://cognito-idp.%s.amazonaws.com/%s/.well-known/jwks.json"
        % (cognito_region, user_pool_id)
    )
    # Validate scheme defensively before opening
    if not url.startswith("https://"):
        raise ValueError("JWKS URL must use HTTPS")
    resp = urllib.request.urlopen(url)
    data = resp.read().decode("utf-8")
    _jwks_cache = json.loads(data)
    _jwks_cache_time = time.time()
    return _jwks_cache


def verify_jwt(token, cfg):
    """Verify JWT token and return payload claims."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Invalid JWT")

    header = json.loads(base64url_decode(parts[0]).decode("utf-8"))
    payload = json.loads(base64url_decode(parts[1]).decode("utf-8"))

    now = int(time.time())
    if payload.get("exp") and payload["exp"] < now:
        raise ValueError("Token expired")

    expected_issuer = "https://cognito-idp.%s.amazonaws.com/%s" % (
        cfg["cognitoRegion"],
        cfg["userPoolId"],
    )
    if payload.get("iss") != expected_issuer:
        raise ValueError("Invalid issuer")

    token_aud = payload.get("aud") or payload.get("client_id")
    if token_aud != cfg["clientId"]:
        raise ValueError("Invalid audience")

    jwks = fetch_jwks(cfg["cognitoRegion"], cfg["userPoolId"])
    key = None
    for k in jwks["keys"]:
        if k["kid"] == header.get("kid"):
            key = k
            break
    if key is None:
        raise ValueError("Key not found in JWKS")

    # Verify RSA signature
    signature_input = ("%s.%s" % (parts[0], parts[1])).encode("utf-8")
    signature = base64url_decode(parts[2])

    if not _verify_rsa_sha256(signature_input, signature, key):
        raise ValueError("Invalid signature")

    return payload


# ── Request routing ──

_STATIC_ASSET_RE = re.compile(r"\.(css|js|ico|woff2?|ttf|eot)$", re.IGNORECASE)


def _parse_cookies(headers):
    """Parse cookies from CloudFront headers format."""
    cookies = {}
    cookie_headers = headers.get("cookie")
    if not cookie_headers:
        return cookies
    for h in cookie_headers:
        for pair in h["value"].split(";"):
            pair = pair.strip()
            if not pair:
                continue
            parts = pair.split("=", 1)
            name = parts[0].strip()
            value = parts[1].strip() if len(parts) > 1 else ""
            if name:
                cookies[name] = value
    return cookies


def _login_redirect(cfg):
    """Build 302 redirect response to Cognito hosted login."""
    url = (
        "%s/oauth2/authorize"
        "?client_id=%s"
        "&response_type=code"
        "&scope=openid+email+profile"
        "&redirect_uri=%s"
        % (
            cfg["cognitoDomain"],
            cfg["clientId"],
            urllib.parse.quote(cfg["callbackUrl"], safe=""),
        )
    )
    return {
        "status": "302",
        "statusDescription": "Found",
        "headers": {
            "location": [{"key": "Location", "value": url}],
            "cache-control": [
                {"key": "Cache-Control", "value": "no-cache, no-store"}
            ],
        },
    }


def handler(event, context):
    """Lambda@Edge viewer-request handler."""
    request = event["Records"][0]["cf"]["request"]
    uri = request["uri"]

    # Load config from SSM (cached after first call)
    try:
        cfg = load_config()
    except Exception as e:
        logger.error("Failed to load config from SSM: %s", e)
        return {
            "status": "503",
            "statusDescription": "Service Unavailable",
            "headers": {
                "content-type": [
                    {"key": "Content-Type", "value": "text/html"}
                ],
                "cache-control": [
                    {"key": "Cache-Control", "value": "no-cache, no-store"}
                ],
            },
            "body": "<html><body><h1>Service Unavailable</h1>"
            "<p>Authentication configuration could not be loaded. "
            "Please try again later.</p></body></html>",
        }

    # Allow static assets without authentication
    if _STATIC_ASSET_RE.search(uri):
        return request

    # Allow OAuth callback
    if request.get("querystring") and "code=" in request["querystring"]:
        return request

    # Allow SPA bootstrap config
    if uri == "/config.json":
        return request

    cookies = _parse_cookies(request.get("headers", {}))
    id_token = cookies.get("awana-id-token")

    if not id_token:
        return _login_redirect(cfg)

    try:
        verify_jwt(id_token, cfg)
        return request
    except Exception as e:
        logger.warning("JWT verification failed: %s", e)
        return _login_redirect(cfg)
