# core/script_sandbox.py — 脚本/表达式沙箱（031-api-testing-enhancements）
#
# 契约（spec FR-006/FR-011、data-model §1.2、contracts §5）：
#   · 自研 AST 白名单，零新依赖；表达式模式（本文件当前范围）与语句模式（US3 追加）共用本模块
#   · 上下文（表达式）：json / status_code / duration_ms / headers / vars
#   · **结果必须是 bool**（宁失败不误判）：非 bool 直接判失败并说明实际类型
#   · 越权（import/lambda/推导式/dunder/未知名称）→ 可读拒绝；异常 → 可读失败（无堆栈泄漏）
#   · 超时兜底：求值放在独立线程，超过 timeout_ms 返回可读错误
#     （表达式模式已禁推导式 → 无法写出循环；此处兜底极端表达式与未来语句模式）
from __future__ import annotations

import ast
import base64 as _base64
import hashlib as _hashlib
import hmac as _hmac
import json as _json_mod
import logging
import random as _random
import re as _re
import time as _time
import urllib.parse as _urllib_parse
import uuid as _uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_MS = 2000
# 语句模式的 for 迭代上界（contracts §5）
MAX_FOR_ITERATIONS = 10_000
# 脚本输出（变量 + print）序列化上限
MAX_OUTPUT_BYTES = 64 * 1024

# 共享执行器：**绝不 shutdown(wait=True)** —— 超时后放弃失控线程，
# 保证调用方按时返回（否则 with ThreadPoolExecutor 退出时会等待失控任务，超时形同虚设）。
_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="vt-sandbox")

# 表达式中可用的安全内置函数（白名单）
SAFE_BUILTINS: dict[str, Callable[..., Any]] = {
    "len": len,
    "min": min,
    "max": max,
    "sum": sum,
    "abs": abs,
    "round": round,
    "any": any,
    "all": all,
    "sorted": sorted,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
}

# 允许的 AST 节点（表达式模式）；刻意排除：推导式/Lambda/Await/Yield/Import/Walrus…
_ALLOWED_NODES: tuple[type[ast.AST], ...] = (
    ast.Expression,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.Subscript,
    ast.Slice,
    ast.Attribute,
    ast.Call,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.Set,
    ast.IfExp,
    ast.Index if hasattr(ast, "Index") else ast.Load,
    # 运算符
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.UAdd,
    ast.USub,
    ast.Not,
    ast.And,
    ast.Or,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
    ast.Is,
    ast.IsNot,
    ast.keyword,
    ast.Starred,
)

_FORBIDDEN_LABELS = {
    ast.ListComp: "列表推导式",
    ast.SetComp: "集合推导式",
    ast.DictComp: "字典推导式",
    ast.GeneratorExp: "生成器表达式",
    ast.Lambda: "lambda",
    ast.Await: "await",
    ast.Yield: "yield",
    ast.YieldFrom: "yield from",
    ast.NamedExpr: "赋值表达式",
    ast.Import: "import",
    ast.ImportFrom: "import",
}


@dataclass
class ExpressionResult:
    """表达式求值结果。

    ``ok`` 为真仅当表达式求值为 **bool True**；``value`` 保留原始结果（False/None/其它），
    ``error`` 为可读原因（语法/越权/异常/超时/非 bool），``detail`` 为失败时的补充说明。
    """

    ok: bool
    value: Any = None
    error: str = ""
    detail: str = ""


class _SandboxRejected(ValueError):
    """AST/名称白名单拒绝。"""


def _check_node(node: ast.AST) -> None:
    for child in ast.walk(node):
        # 1) 明确禁止的语法
        for bad_type, label in _FORBIDDEN_LABELS.items():
            if isinstance(child, bad_type):
                raise _SandboxRejected(f"表达式包含不允许的语法：{label}")
        # 2) 白名单之外一律拒绝
        if not isinstance(child, _ALLOWED_NODES):
            raise _SandboxRejected(f"表达式包含不允许的语法：{type(child).__name__}")
        # 3) dunder 保护（status_code.__class__ / json.__class__ …）
        if isinstance(child, ast.Attribute) and str(child.attr).startswith("_"):
            raise _SandboxRejected("不允许访问下划线属性（dunder 保护）")


def _eval_compiled(tree: ast.Expression, env: dict[str, Any]) -> Any:
    code = compile(tree, "<expression>", "eval")
    return eval(code, {"__builtins__": {}}, env)  # noqa: S307 - AST 白名单已过滤


def _readable_eval_error(exc: BaseException, env: dict[str, Any]) -> str:
    """把求值期异常转成可读原因（不带堆栈）。"""
    if isinstance(exc, KeyError):
        key = exc.args[0] if exc.args else "?"
        return f"响应字段不存在：{key}（表达式中访问了不存在的键）"
    if isinstance(exc, NameError):
        name = str(exc).split("'")[1] if "'" in str(exc) else str(exc)
        available = "、".join(sorted(k for k in env if not k.startswith("_")))
        return f"未定义名称：{name}（可用：{available}）"
    if isinstance(exc, IndexError):
        return f"下标越界：{exc}"
    if isinstance(exc, ZeroDivisionError):
        return "除零错误"
    if isinstance(exc, TypeError):
        return f"类型不匹配：{exc}"
    return f"表达式求值异常：{type(exc).__name__}: {exc}"


def evaluate_expression(
    expression: Any,
    context: Optional[dict[str, Any]] = None,
    *,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> ExpressionResult:
    """求值一条表达式断言。

    失败语义：任何问题都以 ``ExpressionResult(ok=False, error=...)`` 返回，**不抛异常**，
    保证「非法表达式不中断批次」（spec SC-006）。
    """
    text = str(expression or "").strip()
    if not text:
        return ExpressionResult(ok=False, error="表达式为空")

    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        return ExpressionResult(ok=False, error=f"表达式语法错误：{exc.msg}")

    try:
        _check_node(tree)
    except _SandboxRejected as exc:
        return ExpressionResult(ok=False, error=str(exc))

    env: dict[str, Any] = dict(SAFE_BUILTINS)
    env.update(context or {})

    status, value, exc = _run_limited(lambda: _eval_compiled(tree, env), timeout_ms)
    if status == "timeout":
        return ExpressionResult(ok=False, error=f"表达式执行超时（>{timeout_ms}ms）")
    if status == "error":
        return ExpressionResult(ok=False, error=_readable_eval_error(exc, env))

    if isinstance(value, bool):
        if value:
            return ExpressionResult(ok=True, value=True)
        return ExpressionResult(
            ok=False,
            value=False,
            error="",
            detail=f"表达式求值为 False：{text}",
        )

    type_name = type(value).__name__
    shown = repr(value)
    if len(shown) > 80:
        shown = shown[:80] + "…"
    return ExpressionResult(
        ok=False,
        value=value,
        error=f"表达式结果必须是布尔值（实际 {type_name}: {shown}）",
    )


def _run_limited(fn: Callable[[], Any], timeout_ms: int) -> tuple[str, Any, BaseException | None]:
    """在共享执行器里限时执行。返回 ("done"|"timeout"|"error", value, exc)。"""
    future = _EXECUTOR.submit(fn)
    try:
        return "done", future.result(timeout=max(0.05, timeout_ms / 1000.0)), None
    except FutureTimeout:
        future.cancel()  # 已运行的线程无法强杀 → 放弃（见模块头部说明）
        return "timeout", None, None
    except BaseException as exc:  # noqa: BLE001 - 交给调用方转可读错误
        return "error", None, exc


# ── 语句模式（US3）：受限 Python 子集 ───────────────────────────────────────

_ALLOWED_STMT_NODES: tuple[type[ast.AST], ...] = _ALLOWED_NODES + (
    ast.Module,
    ast.Assign,
    ast.AugAssign,
    ast.If,
    ast.For,
    ast.Expr,
    ast.Pass,
    ast.Store,
)

_FORBIDDEN_STMT_LABELS = {
    ast.While: "while 循环（请改用有界的 for）",
    ast.FunctionDef: "函数定义",
    ast.AsyncFunctionDef: "异步函数定义",
    ast.ClassDef: "类定义",
    ast.Import: "import",
    ast.ImportFrom: "import",
    ast.Try: "try/except",
    ast.With: "with",
    ast.Raise: "raise",
    ast.Global: "global",
    ast.Nonlocal: "nonlocal",
    ast.Delete: "del",
    ast.Assert: "assert",
    ast.Match: "match",
    ast.Break: "break",
    ast.Continue: "continue",
}


class _BoundedIterator:
    """for 迭代上界包装：超出即抛错（可读）。"""

    def __init__(self, iterable: Any, limit: int = MAX_FOR_ITERATIONS) -> None:
        self._iterable = iterable
        self._limit = limit

    def __iter__(self):
        count = 0
        for item in self._iterable:
            count += 1
            if count > self._limit:
                raise ValueError(f"for 迭代超过上界 {self._limit}（contracts §5：迭代必须有界）")
            yield item


class _BoundedForRewriter(ast.NodeTransformer):
    """把 ``for x in EXPR:`` 改写为 ``for x in __bounded(EXPR):``。"""

    def visit_For(self, node: ast.For):  # noqa: N802 - ast 命名约定
        self.generic_visit(node)
        # 静态检查：range(<字面量>) 直接给出可读错误
        it = node.iter
        if (
            isinstance(it, ast.Call)
            and isinstance(it.func, ast.Name)
            and it.func.id == "range"
            and it.args
            and all(isinstance(a, ast.Constant) for a in it.args)
        ):
            try:
                upper = max(int(a.value) for a in it.args)
            except (TypeError, ValueError):
                upper = 0
            if upper > MAX_FOR_ITERATIONS:
                raise _SandboxRejected(
                    f"for 迭代上界为 {MAX_FOR_ITERATIONS}，range({upper}) 超出（contracts §5）"
                )
        node.iter = ast.Call(
            func=ast.Name(id="__bounded", ctx=ast.Load()), args=[node.iter], keywords=[]
        )
        return node


def _check_statement_tree(tree: ast.AST) -> None:
    for child in ast.walk(tree):
        for bad_type, label in _FORBIDDEN_STMT_LABELS.items():
            if isinstance(child, bad_type):
                raise _SandboxRejected(f"脚本包含不允许的语法：{label}")
        if isinstance(child, ast.Name) and child.id.startswith("__") and child.id != "__bounded":
            raise _SandboxRejected(f"脚本包含不允许的名称：{child.id}")
        if isinstance(child, ast.Attribute) and str(child.attr).startswith("_"):
            raise _SandboxRejected("不允许访问下划线属性（dunder 保护）")
        if not isinstance(child, _ALLOWED_STMT_NODES):
            label = _FORBIDDEN_LABELS.get(type(child)) or type(child).__name__
            raise _SandboxRejected(f"脚本包含不允许的语法：{label}")


def _crypto_helpers() -> dict[str, Callable[..., Any]]:
    """cryptography 安全子集（contracts §5）：AES-GCM/CBC、RSA PKCS1/PSS、EC。"""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    def _to_bytes(value: Any) -> bytes:
        """str → utf-8 编码；bytes/bytearray 原样；其余转 str 再编码（契约：字符串/字节都可用）。"""
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        if isinstance(value, str):
            return value.encode("utf-8")
        return str(value).encode("utf-8")

    def _load_private(pem: Any):
        return serialization.load_pem_private_key(
            pem.encode("utf-8") if isinstance(pem, str) else _to_bytes(pem), password=None
        )

    def _load_public(pem: Any):
        return serialization.load_pem_public_key(
            pem.encode("utf-8") if isinstance(pem, str) else _to_bytes(pem)
        )

    def aes_gcm_encrypt(key: Any, nonce: Any, plaintext: Any) -> bytes:
        return AESGCM(_to_bytes(key)).encrypt(_to_bytes(nonce), _to_bytes(plaintext), None)

    def aes_gcm_decrypt(key: Any, nonce: Any, ciphertext: Any) -> bytes:
        return AESGCM(_to_bytes(key)).decrypt(_to_bytes(nonce), _to_bytes(ciphertext), None)

    def _cbc(key: Any, iv: Any):
        return Cipher(algorithms.AES(_to_bytes(key)), modes.CBC(_to_bytes(iv)))

    def aes_cbc_encrypt(key: Any, iv: Any, plaintext: Any) -> bytes:
        enc = _cbc(key, iv).encryptor()
        return enc.update(_to_bytes(plaintext)) + enc.finalize()

    def aes_cbc_decrypt(key: Any, iv: Any, ciphertext: Any) -> bytes:
        dec = _cbc(key, iv).decryptor()
        return dec.update(_to_bytes(ciphertext)) + dec.finalize()

    def _hash_alg(name: str):
        return {"sha256": hashes.SHA256(), "sha1": hashes.SHA1(), "sha384": hashes.SHA384()}[name]

    def rsa_sign_pkcs1(private_pem: Any, data: Any, hash_algo: str = "sha256") -> bytes:
        key = _load_private(private_pem)
        return key.sign(_to_bytes(data), padding.PKCS1v15(), _hash_alg(hash_algo))

    def rsa_verify_pkcs1(public_pem: Any, signature: Any, data: Any, hash_algo: str = "sha256") -> bool:
        key = _load_public(public_pem)
        try:
            key.verify(_to_bytes(signature), _to_bytes(data), padding.PKCS1v15(), _hash_alg(hash_algo))
            return True
        except Exception:  # noqa: BLE001 - 验签失败即 False
            return False

    def rsa_sign_pss(private_pem: Any, data: Any, hash_algo: str = "sha256") -> bytes:
        key = _load_private(private_pem)
        return key.sign(
            _to_bytes(data),
            padding.PSS(mgf=padding.MGF1(_hash_alg(hash_algo)), salt_length=32),
            _hash_alg(hash_algo),
        )

    def rsa_verify_pss(public_pem: Any, signature: Any, data: Any, hash_algo: str = "sha256") -> bool:
        key = _load_public(public_pem)
        try:
            key.verify(
                _to_bytes(signature),
                _to_bytes(data),
                padding.PSS(mgf=padding.MGF1(_hash_alg(hash_algo)), salt_length=32),
                _hash_alg(hash_algo),
            )
            return True
        except Exception:  # noqa: BLE001
            return False

    def ec_sign(private_pem: Any, data: Any) -> bytes:
        return _load_private(private_pem).sign(_to_bytes(data), ec.ECDSA(hashes.SHA256()))

    def ec_verify(public_pem: Any, signature: Any, data: Any) -> bool:
        key = _load_public(public_pem)
        try:
            key.verify(_to_bytes(signature), _to_bytes(data), ec.ECDSA(hashes.SHA256()))
            return True
        except Exception:  # noqa: BLE001
            return False

    return {
        "aes_gcm_encrypt": aes_gcm_encrypt,
        "aes_gcm_decrypt": aes_gcm_decrypt,
        "aes_cbc_encrypt": aes_cbc_encrypt,
        "aes_cbc_decrypt": aes_cbc_decrypt,
        "rsa_sign_pkcs1": rsa_sign_pkcs1,
        "rsa_verify_pkcs1": rsa_verify_pkcs1,
        "rsa_sign_pss": rsa_sign_pss,
        "rsa_verify_pss": rsa_verify_pss,
        "ec_sign": ec_sign,
        "ec_verify": ec_verify,
    }


def _script_prelude(seed: int = 0) -> dict[str, Any]:
    """预注入命名空间（module 代理 + 便捷函数 + 密码学子集）。"""
    rng = _random.Random(seed)
    prelude: dict[str, Any] = {
        "json": SimpleNamespace(dumps=_json_mod.dumps, loads=_json_mod.loads),
        "base64": SimpleNamespace(b64encode=_base64.b64encode, b64decode=_base64.b64decode),
        "hashlib": SimpleNamespace(md5=_hashlib.md5, sha1=_hashlib.sha1, sha256=_hashlib.sha256),
        "hmac_sha256": lambda key, msg: _hmac.new(
            key.encode("utf-8") if isinstance(key, str) else bytes(key),
            msg.encode("utf-8") if isinstance(msg, str) else bytes(msg),
            _hashlib.sha256,
        ).hexdigest(),
        "hmac_sha1": lambda key, msg: _hmac.new(
            key.encode("utf-8") if isinstance(key, str) else bytes(key),
            msg.encode("utf-8") if isinstance(msg, str) else bytes(msg),
            _hashlib.sha1,
        ).hexdigest(),
        "time": SimpleNamespace(time=_time.time, strftime=_time.strftime),
        "random": SimpleNamespace(
            int=rng.randint, choice=rng.choice, uuid4=_uuid.uuid4
        ),
        "urllib_parse": SimpleNamespace(quote=_urllib_parse.quote, urlencode=_urllib_parse.urlencode),
        "re": SimpleNamespace(match=_re.match, search=_re.search, sub=_re.sub, findall=_re.findall),
        "hex": lambda data: (data if isinstance(data, bytes) else bytes(data)).hex(),
        "unhex": lambda text: bytes.fromhex(text),
        "b64encode": lambda data: _base64.b64encode(
            data if isinstance(data, bytes) else str(data).encode("utf-8")
        ).decode("ascii"),
    }
    prelude.update(_crypto_helpers())
    return prelude


@dataclass
class ScriptResult:
    """脚本执行结果。

    ``variables`` 仅含脚本**新写入**的变量（写回作用域）；``error`` 为可读原因；
    ``output`` 为 print 输出（受 64KB 上限约束，供「试跑」展示）。
    """

    ok: bool
    variables: dict = field(default_factory=dict)
    error: str = ""
    output: str = ""


def run_script(
    script: Any,
    context: Optional[dict[str, Any]] = None,
    *,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    seed: int = 0,
) -> ScriptResult:
    """执行一段脚本（前置/后置共用）。

    失败语义：语法/越权/异常/超时/超输出上限 → ``ok=False`` + 可读 ``error``，不抛异常。
    """
    text = str(script or "")
    if not text.strip():
        return ScriptResult(ok=True)

    try:
        tree = ast.parse(text, mode="exec")
    except SyntaxError as exc:
        return ScriptResult(ok=False, error=f"脚本语法错误：{exc.msg}（第 {exc.lineno} 行）")

    try:
        _check_statement_tree(tree)
        tree = _BoundedForRewriter().visit(tree)
        ast.fix_missing_locations(tree)
    except _SandboxRejected as exc:
        return ScriptResult(ok=False, error=str(exc))

    printed: list[str] = []

    def _capture_print(*args: Any, sep: str = " ", **_kw: Any) -> None:
        printed.append(sep.join(str(a) for a in args))

    env: dict[str, Any] = _script_prelude(seed)
    # 脚本可用的安全内置（表达式白名单 + 容器/迭代构造，range 由 __bounded 限界）
    env.update(
        {
            **SAFE_BUILTINS,
            "range": range,
            "enumerate": enumerate,
            "dict": dict,
            "list": list,
            "tuple": tuple,
            "set": set,
            "isinstance": isinstance,
            "ord": ord,
            "chr": chr,
        }
    )
    injected = set(env)
    env["__bounded"] = _BoundedIterator
    env["print"] = _capture_print
    injected |= {"__bounded", "print"}
    # 只读上下文（vars 快照 + 响应相关），不允许直接被脚本改（改的是新名字）
    read_context = dict(context or {})
    env.update(read_context)
    injected |= set(read_context)

    def _exec() -> None:
        code = compile(tree, "<script>", "exec")
        exec(code, {"__builtins__": {}}, env)  # noqa: S102 - AST 白名单已过滤

    status, _value, exc = _run_limited(_exec, timeout_ms)
    if status == "timeout":
        return ScriptResult(ok=False, error=f"脚本执行超时（>{timeout_ms}ms）")
    if status == "error":
        if isinstance(exc, _SandboxRejected):
            return ScriptResult(ok=False, error=str(exc))
        summary = str(exc).strip().splitlines()[0] if str(exc).strip() else "无详细信息"
        return ScriptResult(
            ok=False,
            error=f"脚本异常: {type(exc).__name__}: {summary[:200]}",
        )

    # 新变量 = 执行后新增的名字（排除预注入与上下文）
    new_vars: dict[str, Any] = {
        name: value
        for name, value in env.items()
        if name not in injected and not name.startswith("_")
    }
    output = "\n".join(printed)
    try:
        dumped = _json_mod.dumps(new_vars, default=str, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        return ScriptResult(ok=False, error=f"脚本变量无法序列化：{exc}")
    total = len(dumped.encode("utf-8")) + len(output.encode("utf-8"))
    if total > MAX_OUTPUT_BYTES:
        return ScriptResult(
            ok=False,
            error=f"脚本输出超过上限 {MAX_OUTPUT_BYTES // 1024}KB（实际约 {total // 1024}KB）",
        )
    return ScriptResult(ok=True, variables=new_vars, output=output)


def _evaluate_safely(expr: str, context: dict, timeout_ms: int) -> ExpressionResult:
    """内部包装：把 eval 期间的运行时异常转成可读错误（避免堆栈泄漏）。"""
    try:
        return evaluate_expression(expr, context, timeout_ms=timeout_ms)
    except Exception as exc:  # noqa: BLE001 - 兜底，绝不让断言执行崩溃
        logger.exception("表达式求值异常: %s", expr)
        return ExpressionResult(ok=False, error=f"表达式求值异常：{type(exc).__name__}: {exc}")


__all__ = [
    "DEFAULT_TIMEOUT_MS",
    "MAX_FOR_ITERATIONS",
    "MAX_OUTPUT_BYTES",
    "ExpressionResult",
    "SAFE_BUILTINS",
    "ScriptResult",
    "evaluate_expression",
    "run_script",
]
