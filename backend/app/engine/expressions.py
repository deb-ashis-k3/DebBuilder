"""n8n-style `{{ ... }}` expressions evaluated with a sandboxed Jinja engine.

Inside braces you can use (the `$` prefix is optional):
  $json            current input item
  $binary          files on the current item ($binary.data.fileName / mimeType / fileSize)
  $input.all / $input.first / $input.item
  $('Node Name').json / .items             output of an earlier node (by name)
  $('Node Name').item                      the item paired with the current one (see NodeRef)
  $node['Node Name'].json                   same, n8n v0 syntax
  $now, $today, $execution.id, $workflow.id/name, $env.VAR (secrets hidden), $vars.key
  $state.key       the workflow's graph state (Settings → State, Update State node)
A string that is exactly one `{{ expr }}` returns the native value (dict, list,
number...); mixed text renders as a string.

Expressions are read-only (lists / dicts / sets can't be changed in place, so `$state`, `$vars` and other
nodes' items stay as they were) and bounded: anything that would build a huge value (`7 ** 7 ** 9`,
`'a' * 10**9`, `'{:>1000000000}'`, `x|center(10**9)` …) fails with "Expression result too large" instead of
freezing the server. See the limits below.
"""

import json as _json
import math
import os
import re
import sys
import types
import uuid
from collections import deque
from collections.abc import Mapping, MutableMapping, MutableSequence, MutableSet
from datetime import date, datetime, timedelta, timezone
from typing import Any

from jinja2 import filters as _jf
from jinja2 import Undefined, pass_environment, pass_eval_context
from jinja2.compiler import CodeGenerator
from jinja2.sandbox import ImmutableSandboxedEnvironment, SandboxedFormatter
from jinja2.utils import generate_lorem_ipsum

# ---------------------------------------------------------------------------
# Limits. Expressions run on the event loop and every editor can write them, so a value that takes seconds
# (or gigabytes) to build would stall every workflow on the server.
# ---------------------------------------------------------------------------
MAX_SOURCE = 100_000  # characters of expression code (inside the {{ }}) in one field
MAX_STR = 10_000_000  # characters in a string an expression builds (also: rough size of a value as text)
MAX_ITEMS = 1_000_000  # elements in a list / tuple an expression builds
MAX_INT_BITS = 100_000  # ~30,000 digits
MAX_WIDTH = 100_000  # width / precision in a format spec ('{:>N}', '%Ns', '%.Nf')


class ExpressionTooLarge(ValueError):
    pass


def _too_large(what: str) -> ExpressionTooLarge:
    return ExpressionTooLarge(f"Expression result too large: {what}")


def _weight(v: Any, limit: int = MAX_STR) -> int:
    """Rough size of `v` as text: characters of its strings plus one per element, counting a shared reference
    every time it appears (as str() / JSON would print it). Stops counting once past `limit`, so this costs at
    most `limit` steps."""
    total, stack = 0, [v]
    while stack:
        x = stack.pop()
        if isinstance(x, (str, bytes, bytearray)):
            total += len(x) + 1
        elif isinstance(x, Mapping):
            total += 1
            stack.extend(x.keys())
            stack.extend(x.values())
        elif isinstance(x, (list, tuple, set, frozenset, deque)):
            total += 1
            stack.extend(x)
        else:
            total += 1
        if total > limit:
            break
    return total


def _check_text(v: Any, what: str = "value") -> Any:
    """Before turning `v` into text (str(), JSON, ~): refuse values that would print as more than MAX_STR."""
    if isinstance(v, str):
        return v
    if _weight(v) > MAX_STR:
        raise _too_large(f"the {what} would be more than {MAX_STR:,} characters as text")
    return v


def _check_len(n: int, what: str, limit: int = MAX_STR, unit: str = "characters") -> None:
    if n > limit:
        raise _too_large(f"{what} would make {n:,} {unit} (limit {limit:,})")


_SEQ = (str, bytes, bytearray, list, tuple)


def _check_mul(left: Any, right: Any) -> None:
    if isinstance(left, int) and isinstance(right, int):
        if left.bit_length() + right.bit_length() > MAX_INT_BITS:
            raise _too_large(f"a number with more than {MAX_INT_BITS:,} bits")
        return
    if isinstance(left, int) and isinstance(right, _SEQ):
        left, right = right, left
    if not (isinstance(left, _SEQ) and isinstance(right, int)) or right <= 0:
        return
    n = len(left) * right
    if isinstance(left, (str, bytes, bytearray)):
        _check_len(n, "repeating text")
        return
    _check_len(n, "repeating a list", MAX_ITEMS, "items")
    if _weight(left, MAX_STR // right + 1) * right > MAX_STR:
        raise _too_large(f"repeating this list {right:,} times would be more than {MAX_STR:,} characters as text")


def _check_add(left: Any, right: Any) -> None:
    if isinstance(left, (str, bytes, bytearray)) and isinstance(right, (str, bytes, bytearray)):
        _check_len(len(left) + len(right), "joining text")
    elif isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        _check_len(len(left) + len(right), "joining lists", MAX_ITEMS, "items")


def _check_pow(base: Any, exp: Any) -> None:
    if isinstance(base, int) and isinstance(exp, int) and exp > 0 and abs(base) > 1:
        bits = exp * math.log2(abs(base))
        if bits > MAX_INT_BITS:
            raise _too_large(f"{base} ** {exp} has about {bits:,.0f} bits (limit {MAX_INT_BITS:,})")


# printf-style specs: %[(key)][flags][width][.precision]type. Bounded key so a '%(' without ')' stays linear.
_PRINTF = re.compile(r"%(?:\([^)]{0,200}\))?[-#0 +]*(\*|\d+)?(?:\.(\*|\d+))?")


def _num(digits: str, what: str) -> int:
    if len(digits) > 7 or int(digits) > MAX_WIDTH:
        raise _too_large(f"{what} {digits[:12]} in a format (limit {MAX_WIDTH:,})")
    return int(digits)


def _check_printf(fmt: Any, args: Any) -> None:
    """`'%10s' % x` / `|format`: bounded widths, and the result estimated before it is built."""
    if not isinstance(fmt, (str, bytes)):
        return
    text = fmt if isinstance(fmt, str) else fmt.decode("latin-1")
    widths, star, refs = 0, False, 0
    for m in _PRINTF.finditer(text):
        refs += 1
        for g in m.groups():
            if g == "*":
                star = True
            elif g:
                widths += _num(g, "width / precision")
    if star:
        for a in args if isinstance(args, tuple) else (args,):
            if isinstance(a, int) and abs(a) > MAX_WIDTH:
                raise _too_large(f"width / precision {a} in a format (limit {MAX_WIDTH:,})")
            if isinstance(a, int):
                widths += abs(a)
    if isinstance(args, Mapping):  # '%(a)s%(a)s…' may print one value many times
        per = MAX_STR // max(refs, 1) + 1
        biggest = max((_weight(v, per) for v in args.values()), default=0)
        est = len(text) + widths + refs * biggest
    else:
        est = len(text) + widths + _weight(args)
    _check_len(est, "formatting")


def _check_str_call(s: Any, name: str, args: tuple, kwargs: dict) -> tuple:
    """Estimate the size of str / bytes methods that can grow their input a lot; returns (possibly) new args."""
    if name in ("center", "ljust", "rjust", "zfill"):
        width = args[0] if args else kwargs.get("width")
        if isinstance(width, int):
            _check_len(width, f".{name}()")
    elif name == "expandtabs":
        size = args[0] if args else kwargs.get("tabsize", 8)
        if isinstance(size, int) and size > 0:
            _check_len(len(s) + s.count("\t" if isinstance(s, str) else b"\t") * size, ".expandtabs()")
    elif name == "replace" and len(args) >= 2 and isinstance(args[0], type(s)) and isinstance(args[1], type(s)):
        old, new = args[0], args[1]
        n = len(s) + 1 if not old else s.count(old)
        count = args[2] if len(args) > 2 else kwargs.get("count", -1)
        if isinstance(count, int) and count >= 0:
            n = min(n, count)
        _check_len(len(s) + n * max(len(new) - len(old), 0), ".replace()")
    elif name == "join" and args:
        parts = args[0] if isinstance(args[0], (list, tuple)) else list(args[0])
        est = len(s) * max(len(parts) - 1, 0)
        _check_len(est, ".join()")
        for p in parts:
            if isinstance(p, (str, bytes, bytearray)):
                est += len(p)
                if est > MAX_STR:
                    _check_len(est, ".join()")
        args = (parts, *args[1:])
    elif name == "translate" and args and isinstance(args[0], Mapping) and isinstance(s, str):
        longest = max((len(v) for v in args[0].values() if isinstance(v, str)), default=1)
        _check_len(len(s) * max(longest, 1), ".translate()")
    return args


class _LimitedFormatter(SandboxedFormatter):
    """str.format with bounded widths and a bounded total: '{0}{0}{0}…' can't print one big value many times."""

    def __init__(self, env):
        super().__init__(env)
        self._out = 0

    def format_field(self, value, format_spec):
        for digits in re.findall(r"\d+", format_spec or ""):
            _num(digits, "width / precision")
        s = super().format_field(_check_text(value), format_spec)
        self._out += len(s)
        _check_len(self._out, "formatting")
        return s


# Mutating methods, also blocked when reached through the type (`list.append($state.lst, 1)`).
_MUTATING = (
    (MutableSet, frozenset({"add", "clear", "difference_update", "discard", "pop", "remove",
                            "symmetric_difference_update", "update", "intersection_update"})),
    (MutableMapping, frozenset({"clear", "pop", "popitem", "setdefault", "update"})),
    (MutableSequence, frozenset({"append", "clear", "pop", "reverse", "insert", "sort", "extend", "remove"})),
    (deque, frozenset({"append", "appendleft", "clear", "extend", "extendleft", "insert", "pop", "popleft",
                       "remove", "rotate"})),
)


class _CodeGen(CodeGenerator):
    def visit_Concat(self, node, frame):  # `a ~ b`: through the environment so the result size is checked
        self.write("environment.expr_concat((")
        for arg in node.nodes:
            self.visit(arg, frame)
            self.write(", ")
        self.write("))")

    def writeline(self, x, node=None, extra=0):
        # Jinja binds `self` to a TemplateReference (template blocks): meaningless in an expression, so None.
        if x.endswith("= TemplateReference(context)"):
            x = x[: -len("TemplateReference(context)")] + "None"
        super().writeline(x, node, extra)


class _SandboxEnv(ImmutableSandboxedEnvironment):
    """Jinja's immutable sandbox (no `.append` / `.update` / … on lists, dicts and sets) plus size limits on
    the operators and methods that can build huge values from a short expression."""

    code_generator_class = _CodeGen
    intercepted_binops = frozenset({"*", "**", "+", "%"})

    def getattr(self, obj, attribute):
        # n8n semantics: `$json.items` / `.keys` / `.values` mean the *field*, not the dict
        # method Jinja would otherwise return (dict.items is a builtin, not data).
        if isinstance(obj, dict) and attribute in obj:
            return obj[attribute]
        return super().getattr(obj, attribute)

    def is_safe_attribute(self, obj, attr, value):
        if not super().is_safe_attribute(obj, attr, value):
            return False
        if attr == "fromkeys":  # dict.fromkeys(range(n), big): n references to one big value
            return False
        if isinstance(obj, type):
            return not any(issubclass(obj, cls) and attr in names for cls, names in _MUTATING)
        return True

    def call_binop(self, context, operator, left, right):
        if operator == "*":
            _check_mul(left, right)
        elif operator == "**":
            _check_pow(left, right)
        elif operator == "+":
            _check_add(left, right)
        elif operator == "%":
            _check_printf(left, right)
        return self.binop_table[operator](left, right)

    def wrap_str_format(self, value):
        wrapped = super().wrap_str_format(value)
        if wrapped is None or type(value.__self__) is not str:
            return wrapped  # Markup keeps Jinja's escaping formatter (autoescape is off here anyway)
        s, is_map = value.__self__, value.__name__ == "format_map"

        def fmt(*args, **kwargs):
            if is_map:
                if kwargs or len(args) != 1:
                    raise TypeError("format_map() takes exactly one argument")
                args, kwargs = (), args[0]
            return _LimitedFormatter(self).vformat(s, args, kwargs)

        return fmt

    def call(self, context, obj, /, *args, **kwargs):
        if isinstance(obj, types.BuiltinMethodType) and isinstance(getattr(obj, "__self__", None), (str, bytes)):
            args = _check_str_call(obj.__self__, obj.__name__, args, kwargs)
        elif (isinstance(obj, types.MethodDescriptorType) and obj.__objclass__ in (str, bytes)
              and args and isinstance(args[0], (str, bytes))):  # str.center('x', 10**9)
            args = (args[0], *_check_str_call(args[0], obj.__name__, args[1:], kwargs))
        return super().call(context, obj, *args, **kwargs)

    def expr_concat(self, parts) -> str:
        out, total = [], 0
        for p in parts:
            s = p if isinstance(p, str) else str(_check_text(p))
            total += len(s)
            _check_len(total, "joining text with ~")
            out.append(s)
        return "".join(out)


# ---- size-checked filters / globals ----------------------------------------
def _f_center(value, width=80):
    if isinstance(width, int):
        _check_len(width, "|center")
    return _jf.do_center(value, width)


def _f_indent(s, width=4, first=False, blank=False):
    text = str(s)
    per = width if isinstance(width, int) else len(str(width))
    _check_len(len(text) + (text.count("\n") + 1) * max(per, 0), "|indent")
    return _jf.do_indent(s, width, first, blank)


@pass_eval_context
def _f_replace(eval_ctx, s, old, new, count=None):
    _check_str_call(str(s), "replace", (str(old), str(new), -1 if count is None else count), {})
    return _jf.do_replace(eval_ctx, s, old, new, count)


@pass_eval_context
def _f_join(eval_ctx, value, d="", attribute=None):
    items = list(value)
    budget = MAX_STR - len(str(d)) * max(len(items) - 1, 0)
    if budget < 0 or (attribute is None and sum(_weight(x, budget + 1) for x in items) > budget):
        raise _too_large(f"|join would make more than {MAX_STR:,} characters")
    return _jf.sync_do_join(eval_ctx, items, d, attribute)


def _f_format(value, *args, **kwargs):
    _check_printf(str(value), kwargs or args)
    return _jf.do_format(value, *args, **kwargs)


@pass_environment
def _f_wordwrap(environment, s, width=79, break_long_words=True, wrapstring=None, break_on_hyphens=True):
    text, ws = str(s), wrapstring or environment.newline_sequence
    lines = len(text) // max(int(width), 1) + text.count("\n") + 1
    _check_len(len(text) + lines * len(ws), "|wordwrap")
    return _jf.do_wordwrap(environment, s, width, break_long_words, wrapstring, break_on_hyphens)


def _f_batch(value, linecount, fill_with=None):
    _check_len(int(linecount), "|batch", MAX_ITEMS, "items per batch")
    return _jf.do_batch(value, linecount, fill_with)


def _f_slice(value, slices, fill_with=None):
    _check_len(int(slices), "|slice", 100_000, "slices")
    return _jf.sync_do_slice(value, slices, fill_with)


@pass_eval_context
def _f_tojson(eval_ctx, value, indent=None):
    if indent is not None and (len(str(indent)) > 8 if isinstance(indent, str) else int(indent) > 8):
        raise _too_large("|tojson indent is limited to 8")
    if isinstance(value, Undefined):  # a missing key renders as JSON null instead of a TypeError
        value = None
    return _jf.do_tojson(eval_ctx, _check_text(value), indent)


def _dumps(v):
    return _json.dumps(_check_text(v), default=str)


class _Str:
    """`str(x)` with the size check; `str.upper(x)` etc. still reach the real type's methods."""

    __slots__ = ()

    def __call__(self, *args, **kwargs):
        if args:
            _check_text(args[0])
        return str(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(str, name)


_str = _Str()


def _sum(iterable, start=0):
    """sum(), but `sum(lists, [])` (quadratic in Python) runs linear and bounded."""
    if isinstance(start, (list, tuple)):
        out = list(start)
        for part in iterable:
            out.extend(part)
            _check_len(len(out), "sum()", MAX_ITEMS, "items")
        return tuple(out) if isinstance(start, tuple) else out
    return sum(iterable, start)


def _lipsum(n=5, html=True, min=20, max=100):  # Jinja's parameter names
    if int(n) > 100 or int(max) > 1000 or int(min) > 1000:
        raise _too_large("lipsum() is limited to 100 paragraphs of 1,000 words")
    return generate_lorem_ipsum(n, html, min, max)


_env = _SandboxEnv(autoescape=False)
_env.filters.update(
    json=_dumps, fromjson=lambda v: _json.loads(v) if isinstance(v, str) else v,
    center=_f_center, indent=_f_indent, replace=_f_replace, join=_f_join, format=_f_format, wordwrap=_f_wordwrap,
    batch=_f_batch, slice=_f_slice, tojson=_f_tojson, pprint=lambda v: _jf.do_pprint(_check_text(v)),
    string=lambda v: _jf.soft_str(_check_text(v)),
)
_env.globals["lipsum"] = _lipsum

_DOLLAR = re.compile(r"\$(?=[A-Za-z_(])")
_DOLLAR_CALL = re.compile(r"\$\(")


def _blocks(s: str) -> tuple[list[tuple[int, int, str]], str]:
    """Every `{{ … }}` in `s` as (start, end, code) — same pairing as re.finditer(r"\\{\\{(.*?)\\}\\}", s, re.S):
    each '{{' closes at the next '}}' — plus the text outside them. One linear pass: the regex retried every
    '{{' up to the end of the string, which took seconds on a long field."""
    blocks, rest, pos = [], [], 0
    while True:
        a = s.find("{{", pos)
        if a < 0:
            break
        b = s.find("}}", a + 2)
        if b < 0:
            break
        blocks.append((a, b + 2, s[a + 2:b]))
        rest.append(s[pos:a])
        pos = b + 2
    rest.append(s[pos:])
    return blocks, "".join(rest)


def _check_source(blocks: list[tuple[int, int, str]]) -> None:
    size = sum(len(code) for _, _, code in blocks)
    if size > MAX_SOURCE:
        raise ValueError(f"Expression too long: {size:,} characters of code (limit {MAX_SOURCE:,})")


# ---------------------------------------------------------------------------
# Context objects
# ---------------------------------------------------------------------------
class ItemRef(dict):
    """One item, usable both ways: n8n's `.first().json.x` / `.item.json.x` and the short `.first.x`."""

    @property
    def json(self) -> "ItemRef":
        return self

    def __call__(self) -> "ItemRef":
        return self


class ItemsRef(list):
    """A list of items that is also callable, so `.all()` (n8n) and `.all` both work."""

    def __call__(self) -> "ItemsRef":
        return self


def _item(d: dict | None) -> ItemRef:
    return ItemRef(d or {})


class _ItemList:
    """One node's items, wrapped as ItemRef lazily and once per node execution: evaluating a field over n items
    that references a node with m items costs O(1) per access instead of O(m). Sharing the wrappers between
    items is safe because expressions can't modify them (immutable sandbox)."""

    __slots__ = ("_all", "_wrapped", "raw")

    def __init__(self, raw: list[dict] | None):
        self.raw = raw or []
        self._wrapped: dict[int, ItemRef] = {}
        self._all: ItemsRef | None = None

    def at(self, i: int) -> ItemRef:
        w = self._wrapped.get(i)
        if w is None:
            w = self._wrapped[i] = _item(self.raw[i] if -len(self.raw) <= i < len(self.raw) else None)
        return w

    def paired(self, i: int) -> ItemRef:
        n = len(self.raw)
        return self.at(i if 0 <= i < n else max(n - 1, 0))

    def all(self) -> ItemsRef:
        if self._all is None:
            self._all = ItemsRef(self.at(i) for i in range(len(self.raw)))
        return self._all


class InputRef:
    """`$input.all()` / `.first()` / `.last()` / `.item`. The wrapped items are shared by every item's context
    of one node execution, so `$input.all()[0]` over thousands of items stays linear."""

    __slots__ = ("_item_d", "_item_ref", "_items")

    def __init__(self, items: "_ItemList | list[dict]", item: dict | None):
        self._items = items if isinstance(items, _ItemList) else _ItemList(items)
        self._item_d = item
        self._item_ref: ItemRef | None = None

    @property
    def all(self) -> ItemsRef:
        return self._items.all()

    @property
    def first(self) -> ItemRef:
        return self._items.at(0)

    @property
    def last(self) -> ItemRef:
        return self._items.at(-1)

    @property
    def item(self) -> ItemRef:
        if self._item_ref is None:
            self._item_ref = _item(self._item_d)
        return self._item_ref

    def __getitem__(self, key: str):
        return getattr(self, key)


class NodeRef:
    """`$('Name')` / `$node['Name']`: `.json` / `.first()` = its first item, `.last()`, `.all()` / `.items`, and
    `.item` = the item *paired* with the one being evaluated. Pairing is by position (item i of this node ↔
    item i of the referenced node), which is what n8n's pairedItem lineage gives for 1:1 nodes (Set, HTTP
    Request, Code per item…); full lineage through filters / splits / merges isn't tracked. When the
    referenced node has fewer items than the current index, its last item is used (so a node with a single
    item, e.g. a trigger, pairs with every item). `.itemMatching(i)` = the item paired with index i."""

    __slots__ = ("_index", "_items")

    def __init__(self, items: "_ItemList | list[dict]", index: int = 0):
        self._items = items if isinstance(items, _ItemList) else _ItemList(items)
        self._index = index

    @property
    def items(self) -> ItemsRef:
        return self._items.all()

    @property
    def all(self) -> ItemsRef:
        return self._items.all()

    @property
    def json(self) -> ItemRef:
        return self._items.at(0)

    @property
    def first(self) -> ItemRef:
        return self._items.at(0)

    @property
    def last(self) -> ItemRef:
        return self._items.at(-1)

    @property
    def item(self) -> ItemRef:
        return self._items.paired(self._index)

    def itemMatching(self, index: int) -> ItemRef:  # n8n's name
        return self._items.paired(int(index))

    def __getitem__(self, key):
        return self.json.get(key)


class NodeAccessor:
    """Supports both `$('Name')` and `$node['Name']`. `cache` (name → wrapped items) is shared by every item's
    context of one node execution."""

    __slots__ = ("_by_name", "_cache", "_index")

    def __init__(self, by_name: dict[str, list[dict]], cache: dict | None = None, index: int = 0):
        self._by_name = by_name
        self._cache = {} if cache is None else cache
        self._index = index

    def __call__(self, name: str) -> NodeRef:
        items = self._cache.get(name)
        if items is None:
            if name not in self._by_name:
                raise KeyError(f"Node '{name}' has not run (or does not exist)")
            items = self._cache[name] = _ItemList(self._by_name[name])
        return NodeRef(items, self._index)

    def __getitem__(self, name: str) -> NodeRef:
        return self(name)


# ---------------------------------------------------------------------------
# $env: the server's environment minus anything secret (every user can write expressions).
# ---------------------------------------------------------------------------
_SECRET_NAME = re.compile(
    r"KEY|SECRET|TOKEN|PASS|PWD|CREDENTIAL|PRIVATE|AUTH|DSN|URL|URI|DATABASE|CONN|SALT|CERT|SESSION|COOKIE|"
    r"SIGN|HMAC|JWT|BEARER|ENCRYPT|CIPHER|NONCE|SEED|VAULT|WEBHOOK|SMTP|IMAP|LIVEKIT",
    re.I,
)
# Services whose settings usually carry credentials, whatever the variable is called.
_SECRET_PREFIX = ("PG", "POSTGRES", "MYSQL", "MARIADB", "MONGO", "REDIS", "AMQP", "RABBIT", "KAFKA", "AWS_",
                  "AZURE_", "GCP_", "GOOGLE_", "OPENAI", "ANTHROPIC", "DEBBUILDER")
# Values that are secrets whatever their name: URLs with user:password@, PEM blocks, conn strings, JWTs, AWS keys.
_SECRET_VALUE = re.compile(
    r"^[a-z][a-z0-9+.\-]*://[^/?#\s]*@|-----BEGIN|(?:^|[;\s])(?:password|pwd)\s*=|^eyJ[\w-]+\.[\w-]+\.|AKIA[0-9A-Z]{16}",
    re.I,
)
_settings_names: frozenset[str] | None = None


def _app_setting_names() -> frozenset[str]:
    """Every server setting (app/config.py) as its environment name: ENCRYPTION_KEY, INTERNAL_TOKEN, …"""
    global _settings_names
    if _settings_names is None:
        try:
            from ..config import Settings

            _settings_names = frozenset(n.upper() for n in Settings.model_fields)
        except Exception:  # noqa: BLE001 - never let a config problem open the environment up
            _settings_names = frozenset({"DATABASE_URL", "ENCRYPTION_KEY", "INTERNAL_TOKEN"})
    return _settings_names


def env_allowed(key: str) -> bool:
    if not key or not isinstance(key, str):
        return False
    up = key.upper()
    return not (_SECRET_NAME.search(key) or up.startswith(_SECRET_PREFIX) or up in _app_setting_names())


def env_value(key: str) -> str | None:
    """`$env.NAME`, or None when the name or the value looks like a secret."""
    if not env_allowed(key):
        return None
    if sys.platform == "win32" and key != key.upper():
        return None  # Windows ignores case; match names exactly like everywhere else
    value = os.environ.get(key)
    if value is None or _SECRET_VALUE.search(value):
        return None
    return value


class _EnvVars:
    """`$env.NAME`: the server's environment, minus anything that looks like a secret."""

    __slots__ = ()

    def __getattr__(self, key):
        return env_value(key)

    def __getitem__(self, key):
        return env_value(key)


_ENV = _EnvVars()


def _translate(expr: str) -> str:
    expr = _DOLLAR_CALL.sub("node(", expr)
    return _DOLLAR.sub("", expr)


class ExprScope:
    """The part of an expression context that is the same for every item of one node execution. Build it once
    per node execution and call `context(item, i)` per item: `$('Node')` / `$input` wrappers are then built
    once instead of once per item."""

    def __init__(self, items: list[dict], node_outputs: dict[str, list[dict]], meta: dict,
                 state: dict | None = None):
        self.items = items
        self._input = _ItemList(items)
        self._node_outputs = node_outputs
        self._nodes: dict[str, _ItemList] = {}
        self._base: dict[str, Any] = {
            "items": items,
            "timedelta": timedelta,
            "date": date,
            "datetime": datetime,
            "execution": {"id": meta.get("execution_id"), "mode": meta.get("mode")},
            "workflow": {"id": meta.get("workflow_id"), "name": meta.get("workflow_name")},
            "vars": meta.get("vars", {}),
            "state": state or {},
            "env": _ENV,
            "uuid": lambda: str(uuid.uuid4()),
            "len": len, "str": _str, "int": int, "float": float, "bool": bool,
            "round": round, "min": min, "max": max, "sum": _sum, "abs": abs,
            "sorted": sorted, "list": list, "dict": dict,
            "dumps": _dumps,
            "loads": _json.loads,
        }

    def context(self, item: dict | None, item_index: int) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        ctx = dict(self._base)
        ctx.update(
            json=item or {},
            binary=(item or {}).get("binary") or {},
            input=InputRef(self._input, item),
            itemIndex=item_index,
            node=NodeAccessor(self._node_outputs, self._nodes, item_index),
            now=now,
            today=now.date(),
        )
        return ctx


def build_context(
    item: dict | None,
    items: list[dict],
    item_index: int,
    node_outputs: dict[str, list[dict]],
    meta: dict,
    state: dict | None = None,
) -> dict[str, Any]:
    """One item's context. For many items of the same node, build an ExprScope once and use its context()."""
    return ExprScope(items, node_outputs, meta, state).context(item, item_index)


def evaluate(value: Any, ctx: dict[str, Any]) -> Any:
    """Recursively resolve expressions inside strings, lists and dicts."""
    if isinstance(value, str):
        return _eval_str(value, ctx)
    if isinstance(value, list):
        return [evaluate(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: evaluate(v, ctx) for k, v in value.items()}
    return value


def _own(v: Any) -> Any:
    """The shared ItemRef / ItemsRef wrappers returned as a node's value become the node's own copies."""
    if isinstance(v, ItemsRef):
        return ItemsRef(ItemRef(i) for i in v)
    if isinstance(v, ItemRef):
        return ItemRef(v)
    return v


def _eval_str(s: str, ctx: dict[str, Any]) -> Any:
    # n8n marks expression fields with a leading "="; accept and strip it.
    if s.startswith("={{"):
        s = s[1:]
    if "{{" not in s:
        return s
    blocks, rest = _blocks(s)
    if "{{" in rest:  # an opened {{ that is never closed is a mistake, not text
        raise ValueError(f"Unclosed expression: '{{{{' without a matching '}}}}' in {rest[rest.find('{{'):][:60]!r}")
    _check_source(blocks)
    stripped = s.strip()
    # One expression → its native value. "{{ a }} x {{ b }}" also starts with {{ and ends with }}, but it is
    # several blocks: a "}} … {{" inside means text between expressions (nested dict braces never reopen).
    if len(stripped) >= 4 and stripped.startswith("{{") and stripped.endswith("}}"):
        inner = stripped[2:-2]
        close = inner.find("}}")
        if close < 0 or inner.find("{{", close + 2) < 0:
            return _own(_compile(inner)(**ctx))

    out, pos, total = [], 0, 0
    for a, b, code in blocks:
        v = _compile(code)(**ctx)
        if isinstance(v, (dict, list)):
            v = _json.dumps(_check_text(v), default=str)
        else:
            v = "" if v is None else str(_check_text(v))
        out.append(s[pos:a])
        out.append(v)
        total += len(v) + a - pos
        _check_len(total, "the text")
        pos = b
    out.append(s[pos:])
    return "".join(out)


_cache: dict[str, Any] = {}
_cache_chars = 0


def _compile(expr: str):
    global _cache_chars
    fn = _cache.get(expr)
    if fn is None:
        try:
            fn = _env.compile_expression(_translate(expr.strip()), undefined_to_none=True)
        except ExpressionTooLarge as e:
            raise ValueError(f"Invalid expression '{{{{{expr[:200]}}}}}': {e}") from e
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"Invalid expression '{{{{{expr}}}}}': {e}") from e
        if len(_cache) > 5000 or _cache_chars > 20_000_000:  # bounded by count and by size
            _cache.clear()
            _cache_chars = 0
        _cache[expr] = fn
        _cache_chars += len(expr)
    return fn


# ---------------------------------------------------------------------------
# Editor checks (n8n's inline expression states + field checks)
# ---------------------------------------------------------------------------

_ACCESSORS = {"json", "first", "last", "item", "all", "items"}


def _chain(n) -> tuple[object, list] | None:
    """Unwind `a.b['c'].d()` into (root, ['b', 'c', 'd()']) from a Jinja AST node."""
    from jinja2 import nodes as J

    path: list = []
    while True:
        if isinstance(n, J.Getattr):
            path.append(n.attr)
            n = n.node
        elif isinstance(n, J.Getitem) and isinstance(n.arg, J.Const):
            path.append(n.arg.value)
            n = n.node
        elif isinstance(n, J.Call) and not n.args and isinstance(n.node, J.Getattr) and n.node.attr in _ACCESSORS:
            n = n.node  # .first() / .all(): same as the attribute
        else:
            return n, list(reversed(path))


def _check_path(data, path: list, label: str) -> tuple[str, bool] | None:
    """None if `path` exists in `data`; else (message naming the missing key, whether a close match exists)."""
    import difflib

    cur, walked = data, label
    for key in path:
        if isinstance(cur, list):
            if isinstance(key, int) and -len(cur) <= key < len(cur):
                cur, walked = cur[key], f"{walked}[{key}]"
                continue
            return None  # list methods / slicing: not ours to judge
        if not isinstance(cur, dict):
            return None  # str / number methods etc.
        if key not in cur:
            # 0.75: real typos (messag→message 0.92, nmae→name 0.75), not unrelated words (query→user 0.67)
            close = difflib.get_close_matches(str(key), [str(k) for k in cur], n=1, cutoff=0.75)
            hint = f" — did you mean '{close[0]}'?" if close else (f" (it has: {', '.join(list(map(str, cur))[:8])})" if cur else "")
            return f"No field '{key}' in {walked}{hint}", bool(close)
        cur, walked = cur[key], f"{walked}.{key}"
    return None


def _field_warnings(expr: str, item: dict, node_outputs: dict[str, list[dict]]) -> list[str]:
    from jinja2 import nodes as J

    try:
        tree = _env.parse("{{ " + _translate(expr.strip()) + " }}")
    except Exception:  # noqa: BLE001 - syntax errors are reported separately
        return []
    # Fields with a fallback (`a or b`, `x if c else y`, `| default`) are often optional on purpose: flag those
    # only when they look like a typo of a real field. Anything else that's missing is flagged.
    soft: set[int] = set()
    for f in tree.find_all((J.Or, J.CondExpr, J.Filter, J.Test)):
        if isinstance(f, J.Filter) and f.name != "default" or isinstance(f, J.Test) and f.name != "defined":
            continue
        soft.update(id(x) for x in f.find_all((J.Getattr, J.Getitem)))
    found: list[str] = []
    for n in tree.find_all((J.Getattr, J.Getitem)):
        root, path = _chain(n)
        if not path:
            continue
        if isinstance(root, J.Name) and root.name == "json" and item:
            msg = _check_path(item, path, "the input item")
        elif (isinstance(root, J.Call) and isinstance(root.node, J.Name) and root.node.name == "node"
              and root.args and isinstance(root.args[0], J.Const) and root.args[0].value in node_outputs):
            rest = list(path)
            while rest and (rest[0] in _ACCESSORS or isinstance(rest[0], int)):  # .first().json / .all()[0].json
                rest = rest[1:]
            out = node_outputs[root.args[0].value]
            msg = _check_path(out[0] if out else {}, rest, f"$('{root.args[0].value}')") if out else None
        else:
            continue
        if msg and (msg[1] or id(n) not in soft) and msg[0] not in found:
            found.append(msg[0])
    return found[:3]


def inspect(value: str, ctx: dict[str, Any], node_names: set[str], node_outputs: dict[str, list[dict]]) -> list[dict]:
    """Per {{ }} segment, like n8n's inline expression editor: state valid | pending | invalid, its value or
    error, plus warnings for fields that don't exist in the data the editor has (did you mean …)."""
    segments: list[dict] = []
    item = ctx.get("json") or {}
    blocks, _ = _blocks(value or "")
    try:
        _check_source(blocks)
    except ValueError as e:
        return [{"from": 0, "to": len(value), "state": "invalid", "error": str(e), "warnings": []}]
    for start, end, expr in blocks:
        seg = {"from": start, "to": end, "state": "valid", "warnings": []}
        try:
            _compile(expr)
        except ValueError as e:
            seg.update(state="invalid", error=str(e).split(": ", 1)[-1])
            segments.append(seg)
            continue
        refs = re.findall(r"\$\(\s*['\"]([^'\"]+)['\"]\s*\)|\$node\[\s*['\"]([^'\"]+)['\"]\s*\]", expr)
        names = {a or b for a, b in refs}
        unknown = sorted(n for n in names if n not in node_names)
        not_run = sorted(n for n in names if n in node_names and n not in node_outputs)
        if unknown:
            seg.update(state="invalid", error=f"No node named '{unknown[0]}' in this workflow")
        elif not_run:
            seg.update(state="pending", error=f"Execute '{not_run[0]}' to see this value")
        else:
            try:
                v = _compile(expr)(**ctx)
                seg["value"] = v if isinstance(v, str) else _json.dumps(v, default=str)
            except Exception as e:  # noqa: BLE001
                seg.update(state="invalid", error=f"{type(e).__name__}: {e}"[:300])
            seg["warnings"] = _field_warnings(expr, item, node_outputs)
        segments.append(seg)
    return segments
