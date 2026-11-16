"""Self-hosted VAPID (RFC 8292) & encrypted Web Push (RFC 8291 aes128gcm) engine."""
import asyncio
import base64
import contextlib
import ctypes
import ctypes.util
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import secrets
import ssl
import struct
import time
from urllib.parse import urlparse

import httpx

from . import store as db


_background_tasks: set = set()


def _spawn_background(coro) -> None:
    """Run a coroutine fire-and-forget while keeping a strong reference so it is not garbage-collected mid-flight."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


NID_X9_62_prime256v1 = 415
POINT_CONVERSION_UNCOMPRESSED = 4
EVP_CTRL_GCM_SET_IVLEN = 0x9
EVP_CTRL_GCM_GET_TAG = 0x10
EVP_CTRL_GCM_SET_TAG = 0x11

_LIB = None


def _load_libcrypto():
    global _LIB
    if _LIB is not None:
        return _LIB

    candidates = []
    ssl_file = getattr(ssl._ssl, '__file__', None)
    if ssl_file:
        ssl_path = Path(ssl_file).resolve()
        for parent in ssl_path.parents[:4]:
            candidates.extend(sorted(parent.glob('libcrypto*.dylib')))
            candidates.extend(sorted(parent.glob('libcrypto.so*')))
    found = ctypes.util.find_library('crypto')
    if found:
        candidates.append(found)

    lib = None
    for cand in candidates:
        try:
            lib = ctypes.CDLL(str(cand))
            break
        except OSError:
            continue
    if lib is None:
        raise RuntimeError('未找到可用的 OpenSSL libcrypto 动态库')

    lib.EC_KEY_new_by_curve_name.argtypes = [ctypes.c_int]
    lib.EC_KEY_new_by_curve_name.restype = ctypes.c_void_p
    lib.EC_KEY_free.argtypes = [ctypes.c_void_p]
    lib.EC_KEY_generate_key.argtypes = [ctypes.c_void_p]
    lib.EC_KEY_generate_key.restype = ctypes.c_int
    lib.EC_KEY_get0_group.argtypes = [ctypes.c_void_p]
    lib.EC_KEY_get0_group.restype = ctypes.c_void_p
    lib.EC_KEY_get0_public_key.argtypes = [ctypes.c_void_p]
    lib.EC_KEY_get0_public_key.restype = ctypes.c_void_p
    lib.EC_KEY_get0_private_key.argtypes = [ctypes.c_void_p]
    lib.EC_KEY_get0_private_key.restype = ctypes.c_void_p
    lib.EC_KEY_set_private_key.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    lib.EC_KEY_set_private_key.restype = ctypes.c_int
    lib.EC_KEY_set_public_key.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    lib.EC_KEY_set_public_key.restype = ctypes.c_int

    lib.BN_bin2bn.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
    lib.BN_bin2bn.restype = ctypes.c_void_p
    lib.BN_bn2binpad.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    lib.BN_bn2binpad.restype = ctypes.c_int
    lib.BN_free.argtypes = [ctypes.c_void_p]

    lib.EC_POINT_new.argtypes = [ctypes.c_void_p]
    lib.EC_POINT_new.restype = ctypes.c_void_p
    lib.EC_POINT_free.argtypes = [ctypes.c_void_p]
    lib.EC_POINT_mul.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ]
    lib.EC_POINT_mul.restype = ctypes.c_int
    lib.EC_POINT_point2oct.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
        ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p,
    ]
    lib.EC_POINT_point2oct.restype = ctypes.c_size_t
    lib.EC_POINT_oct2point.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p,
        ctypes.c_size_t, ctypes.c_void_p,
    ]
    lib.EC_POINT_oct2point.restype = ctypes.c_int

    lib.ECDH_compute_key.argtypes = [
        ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p,
    ]
    lib.ECDH_compute_key.restype = ctypes.c_int

    lib.ECDSA_do_sign.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
    lib.ECDSA_do_sign.restype = ctypes.c_void_p
    lib.ECDSA_SIG_get0.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    lib.ECDSA_SIG_free.argtypes = [ctypes.c_void_p]

    lib.EVP_CIPHER_CTX_new.restype = ctypes.c_void_p
    lib.EVP_CIPHER_CTX_free.argtypes = [ctypes.c_void_p]
    lib.EVP_aes_128_gcm.restype = ctypes.c_void_p
    lib.EVP_EncryptInit_ex.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_char_p, ctypes.c_char_p,
    ]
    lib.EVP_EncryptInit_ex.restype = ctypes.c_int
    lib.EVP_EncryptUpdate.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int),
        ctypes.c_char_p, ctypes.c_int,
    ]
    lib.EVP_EncryptUpdate.restype = ctypes.c_int
    lib.EVP_EncryptFinal_ex.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int),
    ]
    lib.EVP_EncryptFinal_ex.restype = ctypes.c_int

    lib.EVP_DecryptInit_ex.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_char_p, ctypes.c_char_p,
    ]
    lib.EVP_DecryptInit_ex.restype = ctypes.c_int
    lib.EVP_DecryptUpdate.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int),
        ctypes.c_char_p, ctypes.c_int,
    ]
    lib.EVP_DecryptUpdate.restype = ctypes.c_int
    lib.EVP_DecryptFinal_ex.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int),
    ]
    lib.EVP_DecryptFinal_ex.restype = ctypes.c_int
    lib.EVP_CIPHER_CTX_ctrl.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
    ]
    lib.EVP_CIPHER_CTX_ctrl.restype = ctypes.c_int

    _LIB = lib
    return lib


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def b64url_decode(text: str) -> bytes:
    cleaned = text.strip()
    padding = '=' * ((4 - len(cleaned) % 4) % 4)
    return base64.urlsafe_b64decode(cleaned + padding)


def generate_p256_keypair() -> tuple[bytes, bytes]:
    """Return `(private_scalar_32b, uncompressed_public_point_65b)` on P-256."""
    lib = _load_libcrypto()
    key = lib.EC_KEY_new_by_curve_name(NID_X9_62_prime256v1)
    if not key:
        raise RuntimeError('无法分配 P-256 密钥对象')
    try:
        if lib.EC_KEY_generate_key(key) != 1:
            raise RuntimeError('P-256 密钥对生成失败')
        priv_buf = ctypes.create_string_buffer(32)
        if lib.BN_bn2binpad(lib.EC_KEY_get0_private_key(key), priv_buf, 32) != 32:
            raise RuntimeError('读取 P-256 私钥标量失败')
        pub_buf = ctypes.create_string_buffer(65)
        written = lib.EC_POINT_point2oct(
            lib.EC_KEY_get0_group(key),
            lib.EC_KEY_get0_public_key(key),
            POINT_CONVERSION_UNCOMPRESSED,
            pub_buf,
            65,
            None,
        )
        if written != 65:
            raise RuntimeError('读取 P-256 非压缩公钥失败')
        return priv_buf.raw, pub_buf.raw
    finally:
        lib.EC_KEY_free(key)


def _ec_key_from_private_bytes(private_bytes: bytes) -> int:
    if len(private_bytes) != 32:
        raise ValueError('P-256 私钥必须为 32 字节')
    lib = _load_libcrypto()
    key = lib.EC_KEY_new_by_curve_name(NID_X9_62_prime256v1)
    if not key:
        raise RuntimeError('无法初始化 P-256 密钥')
    bn = lib.BN_bin2bn(private_bytes, 32, None)
    if not bn:
        lib.EC_KEY_free(key)
        raise RuntimeError('无法加载私钥大整数')
    pub = None
    try:
        group = lib.EC_KEY_get0_group(key)
        pub = lib.EC_POINT_new(group)
        if not pub or lib.EC_POINT_mul(group, pub, bn, None, None, None) != 1:
            raise ValueError('无效的 P-256 私钥标量')
        if lib.EC_KEY_set_private_key(key, bn) != 1 or lib.EC_KEY_set_public_key(key, pub) != 1:
            raise ValueError('设置 P-256 密钥失败')
        return key
    except Exception:
        lib.EC_KEY_free(key)
        raise
    finally:
        if pub:
            lib.EC_POINT_free(pub)
        lib.BN_free(bn)


def validate_p256_public_point(public_bytes: bytes) -> None:
    """Validate that `public_bytes` is a 65-byte uncompressed point on P-256."""
    if len(public_bytes) != 65 or public_bytes[0] != 0x04:
        raise ValueError('p256dh 公钥必须是 65 字节非压缩 P-256 曲线点')
    lib = _load_libcrypto()
    key = lib.EC_KEY_new_by_curve_name(NID_X9_62_prime256v1)
    if not key:
        raise RuntimeError('无法初始化曲线校验器')
    point = None
    try:
        group = lib.EC_KEY_get0_group(key)
        point = lib.EC_POINT_new(group)
        if not point or lib.EC_POINT_oct2point(group, point, public_bytes, len(public_bytes), None) != 1:
            raise ValueError('p256dh 公钥不在 P-256 曲线上')
    finally:
        if point:
            lib.EC_POINT_free(point)
        lib.EC_KEY_free(key)


def ecdh_p256(private_bytes: bytes, peer_public_bytes: bytes) -> bytes:
    """Compute 32-byte ECDH shared secret on P-256."""
    validate_p256_public_point(peer_public_bytes)
    lib = _load_libcrypto()
    key = _ec_key_from_private_bytes(private_bytes)
    point = None
    try:
        group = lib.EC_KEY_get0_group(key)
        point = lib.EC_POINT_new(group)
        if not point or lib.EC_POINT_oct2point(group, point, peer_public_bytes, len(peer_public_bytes), None) != 1:
            raise ValueError('无效的对端 P-256 公钥')
        shared = ctypes.create_string_buffer(32)
        out_len = lib.ECDH_compute_key(shared, 32, point, key, None)
        if out_len != 32:
            raise RuntimeError('ECDH 共享密钥派生失败')
        return shared.raw
    finally:
        if point:
            lib.EC_POINT_free(point)
        lib.EC_KEY_free(key)


def sign_es256_jws(signing_input: bytes, private_bytes: bytes) -> bytes:
    """Sign `signing_input` with ECDSA-SHA256 and return 64-byte IEEE P1363 `(r || s)`."""
    lib = _load_libcrypto()
    digest = hashlib.sha256(signing_input).digest()
    key = _ec_key_from_private_bytes(private_bytes)
    sig = None
    try:
        sig = lib.ECDSA_do_sign(digest, len(digest), key)
        if not sig:
            raise RuntimeError('ES256 签名失败')
        r_ptr = ctypes.c_void_p()
        s_ptr = ctypes.c_void_p()
        lib.ECDSA_SIG_get0(sig, ctypes.byref(r_ptr), ctypes.byref(s_ptr))
        r_buf = ctypes.create_string_buffer(32)
        s_buf = ctypes.create_string_buffer(32)
        if lib.BN_bn2binpad(r_ptr, r_buf, 32) != 32 or lib.BN_bn2binpad(s_ptr, s_buf, 32) != 32:
            raise RuntimeError('编码 ES256 签名标量失败')
        return r_buf.raw + s_buf.raw
    finally:
        if sig:
            lib.ECDSA_SIG_free(sig)
        lib.EC_KEY_free(key)


def aes128gcm_encrypt(key: bytes, nonce: bytes, plaintext: bytes) -> bytes:
    """Encrypt with AES-128-GCM (12-byte IV, empty AAD) and return `ciphertext || 16B_tag`."""
    if len(key) != 16 or len(nonce) != 12:
        raise ValueError('AES-128-GCM 需要 16 字节密钥与 12 字节随机数')
    lib = _load_libcrypto()
    ctx = lib.EVP_CIPHER_CTX_new()
    if not ctx:
        raise RuntimeError('无法分配 AES-GCM 上下文')
    try:
        cipher = lib.EVP_aes_128_gcm()
        if lib.EVP_EncryptInit_ex(ctx, cipher, None, None, None) != 1:
            raise RuntimeError('AES-GCM 初始化失败')
        if lib.EVP_CIPHER_CTX_ctrl(ctx, EVP_CTRL_GCM_SET_IVLEN, 12, None) != 1:
            raise RuntimeError('设置 AES-GCM IV 长度失败')
        if lib.EVP_EncryptInit_ex(ctx, None, None, key, nonce) != 1:
            raise RuntimeError('加载 AES-GCM 密钥失败')
        out_buf = ctypes.create_string_buffer(len(plaintext) + 16)
        out_len = ctypes.c_int(0)
        if lib.EVP_EncryptUpdate(ctx, out_buf, ctypes.byref(out_len), plaintext, len(plaintext)) != 1:
            raise RuntimeError('AES-GCM 加密更新失败')
        total = out_len.value
        final_buf = ctypes.create_string_buffer(16)
        final_len = ctypes.c_int(0)
        if lib.EVP_EncryptFinal_ex(ctx, final_buf, ctypes.byref(final_len)) != 1:
            raise RuntimeError('AES-GCM 加密收尾失败')
        tag_buf = ctypes.create_string_buffer(16)
        if lib.EVP_CIPHER_CTX_ctrl(ctx, EVP_CTRL_GCM_GET_TAG, 16, tag_buf) != 1:
            raise RuntimeError('读取 AES-GCM 认证标签失败')
        return out_buf.raw[:total] + final_buf.raw[:final_len.value] + tag_buf.raw
    finally:
        lib.EVP_CIPHER_CTX_free(ctx)


def aes128gcm_decrypt(key: bytes, nonce: bytes, ciphertext_with_tag: bytes) -> bytes:
    """Decrypt AES-128-GCM `ciphertext || 16B_tag` (used for RFC 8291 verification)."""
    if len(key) != 16 or len(nonce) != 12 or len(ciphertext_with_tag) < 16:
        raise ValueError('无效的 AES-128-GCM 输入长度')
    ciphertext = ciphertext_with_tag[:-16]
    tag = ciphertext_with_tag[-16:]
    lib = _load_libcrypto()
    ctx = lib.EVP_CIPHER_CTX_new()
    if not ctx:
        raise RuntimeError('无法分配 AES-GCM 解密上下文')
    try:
        cipher = lib.EVP_aes_128_gcm()
        if lib.EVP_DecryptInit_ex(ctx, cipher, None, None, None) != 1:
            raise RuntimeError('AES-GCM 解密初始化失败')
        if lib.EVP_CIPHER_CTX_ctrl(ctx, EVP_CTRL_GCM_SET_IVLEN, 12, None) != 1:
            raise RuntimeError('设置 AES-GCM 解密 IV 失败')
        if lib.EVP_DecryptInit_ex(ctx, None, None, key, nonce) != 1:
            raise RuntimeError('加载 AES-GCM 解密密钥失败')
        out_buf = ctypes.create_string_buffer(len(ciphertext) + 16)
        out_len = ctypes.c_int(0)
        if lib.EVP_DecryptUpdate(ctx, out_buf, ctypes.byref(out_len), ciphertext, len(ciphertext)) != 1:
            raise RuntimeError('AES-GCM 解密数据失败')
        total = out_len.value
        tag_buf = ctypes.create_string_buffer(tag, 16)
        if lib.EVP_CIPHER_CTX_ctrl(ctx, EVP_CTRL_GCM_SET_TAG, 16, tag_buf) != 1:
            raise RuntimeError('设置 AES-GCM 标签失败')
        final_buf = ctypes.create_string_buffer(16)
        final_len = ctypes.c_int(0)
        if lib.EVP_DecryptFinal_ex(ctx, final_buf, ctypes.byref(final_len)) != 1:
            raise ValueError('AES-GCM 认证标签校验未通过')
        return out_buf.raw[:total] + final_buf.raw[:final_len.value]
    finally:
        lib.EVP_CIPHER_CTX_free(ctx)


def _hkdf_sha256(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    okm = hmac.new(prk, info + b'\x01', hashlib.sha256).digest()
    return okm[:length]


def encrypt_webpush_payload(plaintext: bytes, p256dh_b64: str, auth_b64: str) -> bytes:
    """Encrypt `plaintext` using RFC 8291 (`aes128gcm` content encoding)."""
    ua_public = b64url_decode(p256dh_b64)
    auth_secret = b64url_decode(auth_b64)
    validate_p256_public_point(ua_public)
    if len(auth_secret) != 16:
        raise ValueError('Web Push auth 密钥必须为 16 字节')

    as_private, as_public = generate_p256_keypair()
    ecdh_secret = ecdh_p256(as_private, ua_public)

    key_info = b'WebPush: info\x00' + ua_public + as_public
    ikm = _hkdf_sha256(auth_secret, ecdh_secret, key_info, 32)

    salt = os.urandom(16)
    cek = _hkdf_sha256(salt, ikm, b'Content-Encoding: aes128gcm\x00', 16)
    nonce = _hkdf_sha256(salt, ikm, b'Content-Encoding: nonce\x00', 12)

    padded = plaintext + b'\x02'
    encrypted = aes128gcm_encrypt(cek, nonce, padded)
    header = salt + struct.pack('>IB', 4096, len(as_public)) + as_public
    return header + encrypted


def decrypt_webpush_payload(body: bytes, ua_private: bytes, ua_public: bytes, auth_secret: bytes) -> bytes:
    """Decrypt an RFC 8291 `aes128gcm` payload (used in unit tests to verify standard compliance)."""
    if len(body) < 16 + 4 + 1 + 65 + 17:
        raise ValueError('aes128gcm 报文过短')
    salt = body[:16]
    _rs, idlen = struct.unpack('>IB', body[16:21])
    if idlen != 65:
        raise ValueError('无效的发送方公钥长度')
    as_public = body[21:21 + idlen]
    ciphertext = body[21 + idlen:]

    ecdh_secret = ecdh_p256(ua_private, as_public)
    key_info = b'WebPush: info\x00' + ua_public + as_public
    ikm = _hkdf_sha256(auth_secret, ecdh_secret, key_info, 32)
    cek = _hkdf_sha256(salt, ikm, b'Content-Encoding: aes128gcm\x00', 16)
    nonce = _hkdf_sha256(salt, ikm, b'Content-Encoding: nonce\x00', 12)

    padded = aes128gcm_decrypt(cek, nonce, ciphertext)
    stripped = padded.rstrip(b'\x00')
    if not stripped or stripped[-1] != 0x02:
        raise ValueError('缺少 RFC 8188 终止分隔符 0x02')
    return stripped[:-1]


def ensure_vapid_keys() -> dict:
    """Load or atomically create persistent VAPID P-256 keys in `db.DATA / 'vapid.json'` (`0600`)."""
    path = db.DATA / 'vapid.json'
    if path.is_file():
        try:
            data = json.loads(path.read_text())
            priv = b64url_decode(data['private_key'])
            pub = b64url_decode(data['public_key'])
            if len(priv) == 32 and len(pub) == 65 and pub[0] == 0x04:
                return data
        except Exception:
            pass

    priv, pub = generate_p256_keypair()
    data = {
        'private_key': b64url_encode(priv),
        'public_key': b64url_encode(pub),
        'created': time.time(),
    }
    db.DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    db.write_private(path, json.dumps(data, indent=2))
    return data


def build_vapid_authorization(endpoint: str, vapid: dict | None = None) -> str:
    """Build RFC 8292 `Authorization: vapid t=<jwt>, k=<pubkey>` header for `endpoint`."""
    keys = vapid or ensure_vapid_keys()
    parsed = urlparse(endpoint)
    audience = f'{parsed.scheme}://{parsed.netloc}'
    pub_url = str(db.config().get('public_base_url') or '').strip()
    default_sub = pub_url if pub_url.startswith('https://') else 'mailto:noreply@rove.app'
    subject = db.config().get('vapid_subject') or os.environ.get('ROVE_VAPID_SUBJECT') or default_sub
    if subject == 'mailto:admin@localhost':
        subject = default_sub

    header_b64 = b64url_encode(json.dumps({'typ': 'JWT', 'alg': 'ES256'}, separators=(',', ':')).encode())
    claims_b64 = b64url_encode(json.dumps({
        'aud': audience,
        'exp': int(time.time()) + 43200,
        'sub': subject,
    }, separators=(',', ':')).encode())
    signing_input = f'{header_b64}.{claims_b64}'.encode('ascii')
    sig = sign_es256_jws(signing_input, b64url_decode(keys['private_key']))
    jwt_token = f'{header_b64}.{claims_b64}.{b64url_encode(sig)}'
    return f"vapid t={jwt_token}, k={keys['public_key']}"


def validate_push_endpoint(endpoint: str) -> str:
    """Validate Web Push endpoint URL and block SSRF to loopback/private hosts unless test-enabled."""
    cleaned = (endpoint or '').strip()
    parsed = urlparse(cleaned)
    allow_local = bool(db.config().get('allow_local_push_endpoints'))

    if not parsed.netloc or not parsed.hostname:
        raise ValueError('推送端点 URL 无效')
    if parsed.scheme != 'https' and not (allow_local and parsed.scheme == 'http'):
        raise ValueError('Web Push 订阅端点必须使用 HTTPS')

    host = parsed.hostname.lower()
    if not allow_local:
        if host in ('localhost', 'ip6-localhost') or host.endswith(('.local', '.internal', '.lan')):
            raise ValueError('不允许订阅内网或本机推送端点')
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
                raise ValueError('不允许订阅私有 IP 推送端点')
        except ValueError as exc:
            if '不允许' in str(exc):
                raise
    return cleaned


def validate_subscription_keys(p256dh: str, auth: str) -> tuple[str, str]:
    p256dh_clean = (p256dh or '').strip()
    auth_clean = (auth or '').strip()
    try:
        pub = b64url_decode(p256dh_clean)
        auth_bytes = b64url_decode(auth_clean)
    except Exception as exc:
        raise ValueError('推送加密密钥 Base64 编码无效') from exc
    validate_p256_public_point(pub)
    if len(auth_bytes) != 16:
        raise ValueError('Web Push auth 密钥长度必须为 16 字节')
    return b64url_encode(pub), b64url_encode(auth_bytes)


def endpoint_hash(endpoint: str) -> str:
    return hashlib.sha256(endpoint.strip().encode('utf-8')).hexdigest()


def get_push_status() -> dict:
    vapid = ensure_vapid_keys()
    rows = db.query(
        'SELECT id, endpoint, device_label, created, updated, last_success, last_error '
        'FROM push_subscriptions ORDER BY updated DESC'
    )
    items = []
    for row in rows:
        parsed = urlparse(row['endpoint'])
        items.append({
            'id': row['id'],
            'endpoint_hash': endpoint_hash(row['endpoint']),
            'endpoint_host': parsed.hostname or '',
            'device_label': row['device_label'] or '已订阅设备',
            'created': row['created'],
            'updated': row['updated'],
            'last_success': row['last_success'],
            'last_error': row['last_error'],
        })
    return {
        'enabled': True,
        'public_key': vapid['public_key'],
        'subscriptions_count': len(items),
        'subscriptions': items,
    }


def upsert_subscription(endpoint: str, p256dh: str, auth: str, device_label: str = '', user_agent: str = '') -> dict:
    clean_ep = validate_push_endpoint(endpoint)
    clean_p256dh, clean_auth = validate_subscription_keys(p256dh, auth)
    label = (device_label or '').strip()[:120] or '移动设备'
    ua = (user_agent or '').strip()[:300]
    now = time.time()

    existing = db.one('SELECT id FROM push_subscriptions WHERE endpoint=?', (clean_ep,))
    if existing:
        sub_id = existing['id']
        db.execute(
            'UPDATE push_subscriptions SET p256dh=?, auth=?, device_label=?, user_agent=?, updated=?, last_error=NULL WHERE id=?',
            (clean_p256dh, clean_auth, label, ua, now, sub_id),
        )
    else:
        sub_id = secrets.token_hex(8)
        db.execute(
            'INSERT INTO push_subscriptions(id, endpoint, p256dh, auth, device_label, user_agent, created, updated) '
            'VALUES(?,?,?,?,?,?,?,?)',
            (sub_id, clean_ep, clean_p256dh, clean_auth, label, ua, now, now),
        )
    status = get_push_status()
    return {'ok': True, 'id': sub_id, 'endpoint_hash': endpoint_hash(clean_ep), **status}


def remove_subscription(*, endpoint: str | None = None, sub_id: str | None = None) -> dict:
    if sub_id:
        db.execute('DELETE FROM push_subscriptions WHERE id=?', (sub_id.strip(),))
    elif endpoint:
        db.execute('DELETE FROM push_subscriptions WHERE endpoint=?', (endpoint.strip(),))
    status = get_push_status()
    return {'ok': True, **status}


async def send_push_to_row(client: httpx.AsyncClient, row: dict, payload: dict, vapid: dict) -> dict:
    raw_json = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(raw_json) > 3600:
        trimmed = dict(payload)
        trimmed['body'] = (trimmed.get('body') or '')[:240]
        raw_json = json.dumps(trimmed, ensure_ascii=False, separators=(',', ':')).encode('utf-8')

    encrypted = encrypt_webpush_payload(raw_json, row['p256dh'], row['auth'])
    headers = {
        'Authorization': build_vapid_authorization(row['endpoint'], vapid),
        'Content-Type': 'application/octet-stream',
        'Content-Encoding': 'aes128gcm',
        'TTL': '86400',
        'Urgency': 'high',
    }
    topic = (payload.get('topic') or '').strip()
    if topic and topic.isascii() and len(topic) <= 32:
        headers['Topic'] = topic

    try:
        res = await client.post(row['endpoint'], content=encrypted, headers=headers)
    except Exception as exc:
        err_msg = str(exc)[:300]
        db.execute('UPDATE push_subscriptions SET last_error=? WHERE id=?', (err_msg, row['id']))
        return {'id': row['id'], 'status': 'failed', 'removed': False, 'error': err_msg}

    if 200 <= res.status_code < 300:
        db.execute('UPDATE push_subscriptions SET last_success=?, last_error=NULL WHERE id=?', (time.time(), row['id']))
        return {'id': row['id'], 'status': 'sent', 'removed': False, 'http_status': res.status_code}

    if res.status_code in (404, 410):
        db.execute('DELETE FROM push_subscriptions WHERE id=?', (row['id'],))
        return {'id': row['id'], 'status': 'expired', 'removed': True, 'http_status': res.status_code}

    err_msg = f'HTTP {res.status_code}: {res.text[:180]}'.strip()
    db.execute('UPDATE push_subscriptions SET last_error=? WHERE id=?', (err_msg, row['id']))
    return {'id': row['id'], 'status': 'failed', 'removed': False, 'http_status': res.status_code, 'error': err_msg}


async def notify_all(
    *,
    title: str,
    body: str,
    session_id: str | None = None,
    approval_id: str | None = None,
    actions: list[dict] | None = None,
    tag: str = 'workstation',
    kind: str = 'info',
    require_interaction: bool = False,
    target_endpoint: str | None = None,
) -> dict:
    """Encrypt and deliver a Web Push notification to subscribed devices."""
    if target_endpoint:
        rows = db.query('SELECT * FROM push_subscriptions WHERE endpoint=?', (target_endpoint.strip(),))
    else:
        rows = db.query('SELECT * FROM push_subscriptions ORDER BY updated DESC')

    if not rows:
        return {'ok': True, 'sent': 0, 'failed': 0, 'removed': 0, 'errors': []}

    vapid = ensure_vapid_keys()
    url = f'./?session={session_id}' if session_id else './'
    if session_id and approval_id:
        url = f'./?session={session_id}&approval={approval_id}'
    payload = {
        'title': title[:120],
        'body': body[:300],
        'tag': tag[:80],
        'url': url,
        'sessionId': session_id,
        'approvalId': approval_id,
        'actions': actions or [],
        'kind': kind,
        'requireInteraction': bool(require_interaction),
        'timestamp': int(time.time() * 1000),
    }

    sent = 0
    failed = 0
    removed = 0
    errors = []
    async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
        for row in rows:
            outcome = await send_push_to_row(client, row, payload, vapid)
            if outcome['status'] == 'sent':
                sent += 1
            elif outcome['removed']:
                removed += 1
            else:
                failed += 1
                if outcome.get('error'):
                    errors.append(outcome['error'])

    return {'ok': True, 'sent': sent, 'failed': failed, 'removed': removed, 'errors': errors}


def _extract_job_summary_snippet(job_id: str, engine: str) -> str:
    rows = db.query('SELECT kind, payload FROM events WHERE job_id=? ORDER BY id DESC LIMIT 20', (job_id,))
    for row in rows:
        try:
            payload = json.loads(row['payload'])
        except Exception:
            continue
        if row['kind'] == 'codex':
            item = (payload.get('params') or {}).get('item') or {}
            if payload.get('method') == 'item/completed' and item.get('type') == 'agentMessage':
                txt = (item.get('text') or '').strip()
                if txt:
                    return ' '.join(txt.split())[:120]
        elif row['kind'] == 'antigravity':
            if payload.get('event') == 'agy_step' and payload.get('step_kind') == 'agent':
                txt = (payload.get('text') or '').strip()
                if txt:
                    return ' '.join(txt.split())[:120]
            res = payload.get('result') if payload.get('event') == 'result' else None
            if isinstance(res, dict) and res.get('response'):
                return ' '.join(str(res['response']).split())[:120]
    label = 'Antigravity' if engine == 'antigravity' else 'Codex'
    return f'{label} 已完成后台任务，点击查看完整输出。'


async def notify_job_approval(session: dict, project: dict | None, approval_id: str, method: str, params: dict) -> None:
    """Best-effort push notification when a background job pauses for approval or user input."""
    try:
        proj_name = (project or {}).get('name') or 'Rove'
        sess_title = (session or {}).get('title') or '当前会话'
        if method == 'item/tool/requestUserInput':
            questions = params.get('questions') or []
            first_q = (questions[0].get('question') if questions and isinstance(questions[0], dict) else '') or '请回答交互问题以继续执行'
            title = f'等待你的回答 · {proj_name}'
            body = f'{sess_title}：{first_q}'
        else:
            cmd = params.get('command') or params.get('reason') or '请求执行工具操作'
            if isinstance(cmd, list):
                cmd = ' '.join(str(x) for x in cmd)
            title = f'等待你的审批 · {proj_name}'
            body = f'{sess_title}：{cmd}'
        actions = None
        if method != 'item/tool/requestUserInput':
            actions = [
                {'action': 'accept', 'title': '✓ 批准执行'},
                {'action': 'decline', 'title': '✕ 拒绝'},
            ]
        await notify_all(
            title=title,
            body=body,
            session_id=session.get('id'),
            approval_id=approval_id,
            actions=actions,
            tag=f'approval-{approval_id}',
            kind='approval',
            require_interaction=True,
        )
        with contextlib.suppress(Exception):
            from . import feishu_relay
            _spawn_background(feishu_relay.send_approval_notification(session, project, approval_id, method, params))
    except Exception:
        pass


async def notify_job_outcome(job_id: str, session: dict, project: dict | None, outcome: dict) -> None:
    """Best-effort push notification when a background job finishes or fails."""
    try:
        status = (outcome or {}).get('status')
        if status not in ('completed', 'failed', 'submitted'):
            return
        proj_name = (project or {}).get('name') or 'Rove'
        sess_title = (session or {}).get('title') or '当前会话'
        if status == 'completed':
            snippet = _extract_job_summary_snippet(job_id, session.get('engine') or 'codex')
            title = f'任务已完成 · {proj_name}'
            body = f'{sess_title} — {snippet}'
            kind = 'completed'
        elif status == 'submitted':
            title = f'云端任务已提交 · {proj_name}'
            body = f'{sess_title}：已成功提交至 Codex 云端执行'
            kind = 'submitted'
        else:
            err = (outcome.get('error') or '请打开工作台查看错误详情').strip()
            title = f'任务执行中断 · {proj_name}'
            body = f'{sess_title}：{err}'
            kind = 'failed'
        await notify_all(
            title=title,
            body=body,
            session_id=session.get('id'),
            tag=f"job-{session.get('id') or job_id}",
            kind=kind,
            require_interaction=False,
        )
        with contextlib.suppress(Exception):
            from . import feishu_relay
            _spawn_background(feishu_relay.send_outcome_notification(job_id, session, project, outcome))
    except Exception:
        pass
