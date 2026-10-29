"""Multi-language support for the Code node.

Python runs in-process (see core._code). Every other language runs as a subprocess with
the same contract, n8n-style:

- inputs:  ``items`` (list of objects), ``item`` + ``index`` (the current item in
  "run once for each item" mode) and ``vars`` (workflow variables);
- output:  return a list of objects or a single object (it becomes the node's items);
- logs:    whatever the program prints (stdout / stderr) shows in the execution log.

Data travels through two JSON files (``DEBBUILDER_INPUT`` / ``DEBBUILDER_OUTPUT``): a small
wrapper per language reads the input, calls the user's code and writes the result, so the
user only writes the function body. Shell-style languages (Bash, custom commands) instead
print JSON to stdout (plain text becomes ``{"stdout": ...}``; no output passes items on).

Runtimes are found on PATH (plus Git for Windows' bash/perl). Override any of them with an
env var such as ``DEBBUILDER_RUNTIME_GO=C:\\Go\\bin\\go.exe``.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable

# ---------------------------------------------------------------------------
# Default code per language
# ---------------------------------------------------------------------------

TEMPLATES = {
    "javascript": """// `items` = all input items, `item` / `index` = current item ("each" mode), `$vars` = variables.
// Return an array of objects (or one object). fetch() is available.
for (const item of items) {
  item.processed = true
}
return items
""",
    "typescript": """// Same as JavaScript, with type annotations (erased at run time).
type Row = Record<string, unknown>
const out: Row[] = items.map((it: Row) => ({ ...it, processed: true }))
return out
""",
    "bash": """# Input: $ITEMS (JSON text; also in the file $ITEMS_FILE), and $ITEM / $INDEX in "each" mode.
# Output: print JSON to stdout. Plain text becomes {"stdout": "..."}; printing nothing
# passes the items through. Messages for the log go to stderr (>&2).
echo "received $(printf '%s' "$ITEMS" | wc -c) bytes of items" >&2
echo '{"message": "hello from bash", "host": "'"$(hostname)"'"}'
""",
    "powershell": """# $items = input items, $item / $index = current item ("each" mode), $vars = variables.
# Everything the script outputs becomes the node's items; use Write-Host for logs.
foreach ($it in $items) {
  $it | Add-Member -NotePropertyName processed -NotePropertyValue $true -Force
  $it
}
""",
    "perl": """# $items = array ref of hash refs, $item / $index = current item ("each" mode), $vars = variables.
# Return an array ref (or a hash ref). JSON::PP is loaded.
for my $it (@$items) {
    $it->{processed} = JSON::PP::true;
}
return $items;
""",
    "java": """// items: List<Map<String, Object>>, item / index: current item ("each" mode), vars: Map.
// Return a List or a Map. Lines starting with `import` are moved to the top of the file.
List<Map<String, Object>> out = new ArrayList<>();
for (Map<String, Object> it : items) {
    it.put("processed", true);
    out.add(it);
}
return out;
""",
    "ruby": """# items = array of hashes, item / index = current item ("each" mode), vars = hash.
# Return an array (or one hash).
items.each { |it| it["processed"] = true }
items
""",
    "php": """// $items = array of assoc arrays, $item / $index = current item ("each" mode), $vars.
// Return an array of items (or one item).
foreach ($items as &$it) {
    $it['processed'] = true;
}
return $items;
""",
    "go": """// items []Item (Item = map[string]any), item / index: current item ("each" mode), vars.
// Return (any, error). fmt, strings, strconv, time, math and sort are imported;
// lines starting with `import "..."` are moved to the top of the file.
for _, it := range items {
	it["processed"] = true
}
return items, nil
""",
    "custom": """# Any language: the file runs with the command above.
# Read the input JSON from the path in env DEBBUILDER_INPUT ({"items": [...], "vars": {...}})
# and write the result JSON to the path in DEBBUILDER_OUTPUT (or print JSON to stdout).
""",
}

# ---------------------------------------------------------------------------
# Wrappers (USER_CODE is replaced by the node's code)
# ---------------------------------------------------------------------------

_JS = """import { readFileSync, writeFileSync } from 'node:fs'
const __in = JSON.parse(readFileSync(process.env.DEBBUILDER_INPUT, 'utf8'))
const items = __in.items
const $vars = __in.vars
const $node = (name) => __in.nodes[name] ?? []
const $execution = { id: __in.execution_id }
async function __user(items, item, index, $json) {
USER_CODE
}
const __out = []
const __add = (r) => { if (r === undefined || r === null) return; for (const x of Array.isArray(r) ? r : [r]) __out.push(x) }
if (__in.mode === 'each') { for (let i = 0; i < items.length; i++) __add(await __user(items, items[i], i, items[i])) }
else __add(await __user(items, items[0] ?? {}, 0, items[0] ?? {}))
writeFileSync(process.env.DEBBUILDER_OUTPUT, JSON.stringify(__out))
"""

_PS = """$ErrorActionPreference = 'Stop'
$__in = Get-Content -Raw -Encoding UTF8 -LiteralPath $env:DEBBUILDER_INPUT | ConvertFrom-Json
$items = @($__in.items)
$vars = $__in.vars
function __user($items, $item, $index) {
USER_CODE
}
$__out = New-Object System.Collections.ArrayList
if ($__in.mode -eq 'each') {
  for ($__i = 0; $__i -lt $items.Count; $__i++) { foreach ($__r in @(__user $items $items[$__i] $__i)) { if ($null -ne $__r) { [void]$__out.Add($__r) } } }
} else {
  $__first = if ($items.Count) { $items[0] } else { [pscustomobject]@{} }
  foreach ($__r in @(__user $items $__first 0)) { if ($null -ne $__r) { [void]$__out.Add($__r) } }
}
[System.IO.File]::WriteAllText($env:DEBBUILDER_OUTPUT, (ConvertTo-Json -InputObject @($__out) -Depth 40 -Compress))
"""

_PERL = """use JSON::PP;
my $__json = JSON::PP->new->utf8->allow_nonref->allow_blessed->convert_blessed;
my $__in = do { local $/; open(my $fh, '<:raw', $ENV{DEBBUILDER_INPUT}) or die "input: $!"; $__json->decode(<$fh>) };
my $vars = $__in->{vars};
sub __user { my ($items, $item, $index) = @_;
USER_CODE
}
my @__out;
sub __add { my ($r) = @_; return unless defined $r; push @__out, (ref($r) eq 'ARRAY' ? @$r : $r); }
my $__items = $__in->{items};
if ($__in->{mode} eq 'each') { for my $i (0 .. $#$__items) { __add(__user($__items, $__items->[$i], $i)); } }
else { __add(__user($__items, $__items->[0] // {}, 0)); }
open(my $__o, '>:raw', $ENV{DEBBUILDER_OUTPUT}) or die "output: $!"; print $__o $__json->encode(\\@__out); close $__o;
"""

_RUBY = """require 'json'
__in = JSON.parse(File.read(ENV['DEBBUILDER_INPUT'], encoding: 'UTF-8'))
def __user(items, item, index, vars)
USER_CODE
end
__out = []
__add = ->(r) { next if r.nil?; r.is_a?(Array) ? __out.concat(r) : __out << r }
if __in['mode'] == 'each'
  __in['items'].each_with_index { |it, i| __add.(__user(__in['items'], it, i, __in['vars'])) }
else
  __add.(__user(__in['items'], __in['items'][0] || {}, 0, __in['vars']))
end
File.write(ENV['DEBBUILDER_OUTPUT'], JSON.generate(__out))
"""

_PHP = """<?php
$__in = json_decode(file_get_contents(getenv('DEBBUILDER_INPUT')), true);
$vars = $__in['vars'];
$__user = function ($items, $item, $index) use ($vars) {
USER_CODE
};
$__out = [];
$__add = function ($r) use (&$__out) {
  if ($r === null) return;
  if (is_array($r) && array_is_list($r)) { foreach ($r as $x) $__out[] = $x; } else { $__out[] = $r; }
};
if ($__in['mode'] === 'each') { foreach ($__in['items'] as $i => $it) $__add($__user($__in['items'], $it, $i)); }
else $__add($__user($__in['items'], $__in['items'][0] ?? [], 0));
file_put_contents(getenv('DEBBUILDER_OUTPUT'), json_encode($__out, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE));
"""

_GO = """package main

import (
	"encoding/json"
	"fmt"
	"math"
	"os"
	"sort"
	"strconv"
	"strings"
	"time"
USER_IMPORTS)

var _ = fmt.Sprint
var _ = math.Abs
var _ = sort.Strings
var _ = strconv.Itoa
var _ = strings.ToUpper
var _ = time.Now

type Item = map[string]any

func run(items []Item, item Item, index int, vars map[string]any) (any, error) {
USER_CODE
}

func main() {
	raw, err := os.ReadFile(os.Getenv("DEBBUILDER_INPUT"))
	if err != nil { panic(err) }
	var in struct { Items []Item `json:"items"`; Vars map[string]any `json:"vars"`; Mode string `json:"mode"` }
	if err := json.Unmarshal(raw, &in); err != nil { panic(err) }
	out := []any{}
	add := func(r any) {
		switch v := r.(type) {
		case nil:
		case []Item: for _, x := range v { out = append(out, x) }
		case []any: out = append(out, v...)
		default: out = append(out, v)
		}
	}
	call := func(it Item, i int) {
		r, err := run(in.Items, it, i, in.Vars)
		if err != nil { fmt.Fprintln(os.Stderr, err); os.Exit(1) }
		add(r)
	}
	if in.Mode == "each" { for i, it := range in.Items { call(it, i) } } else {
		first := Item{}
		if len(in.Items) > 0 { first = in.Items[0] }
		call(first, 0)
	}
	b, err := json.Marshal(out)
	if err != nil { panic(err) }
	if err := os.WriteFile(os.Getenv("DEBBUILDER_OUTPUT"), b, 0o644); err != nil { panic(err) }
}
"""

# Java has no JSON in the JDK, so the wrapper carries a tiny parser / serializer.
_JAVA = """import java.util.*;
import java.nio.file.*;
import java.nio.charset.StandardCharsets;
USER_IMPORTS
public class Main {
    @SuppressWarnings("unchecked")
    static Object run(List<Map<String, Object>> items, Map<String, Object> item, int index, Map<String, Object> vars) throws Exception {
USER_CODE
    }

    @SuppressWarnings("unchecked")
    public static void main(String[] a) throws Exception {
        Map<String, Object> in = (Map<String, Object>) new J(Files.readString(Path.of(System.getenv("DEBBUILDER_INPUT")))).value();
        List<Map<String, Object>> items = (List<Map<String, Object>>) (Object) in.get("items");
        Map<String, Object> vars = (Map<String, Object>) in.get("vars");
        List<Object> out = new ArrayList<>();
        if ("each".equals(in.get("mode"))) { for (int i = 0; i < items.size(); i++) add(out, run(items, items.get(i), i, vars)); }
        else add(out, run(items, items.isEmpty() ? new LinkedHashMap<>() : items.get(0), 0, vars));
        Files.writeString(Path.of(System.getenv("DEBBUILDER_OUTPUT")), J.str(out), StandardCharsets.UTF_8);
    }

    static void add(List<Object> out, Object r) { if (r == null) return; if (r instanceof List<?> l) out.addAll(l); else out.add(r); }
}

final class J {
    private final String s; private int i;
    J(String s) { this.s = s; }
    Object value() {
        ws(); char c = s.charAt(i);
        if (c == '{') { i++; Map<String, Object> m = new LinkedHashMap<>(); ws(); if (s.charAt(i) == '}') { i++; return m; }
            while (true) { ws(); String k = (String) value(); ws(); i++; m.put(k, value()); ws(); if (s.charAt(i++) == '}') return m; } }
        if (c == '[') { i++; List<Object> l = new ArrayList<>(); ws(); if (s.charAt(i) == ']') { i++; return l; }
            while (true) { l.add(value()); ws(); if (s.charAt(i++) == ']') return l; } }
        if (c == '"') { StringBuilder b = new StringBuilder(); i++;
            while (true) { char d = s.charAt(i++); if (d == '"') return b.toString();
                if (d == '\\\\') { char e = s.charAt(i++); switch (e) { case 'n' -> b.append('\\n'); case 't' -> b.append('\\t'); case 'r' -> b.append('\\r');
                    case 'b' -> b.append('\\b'); case 'f' -> b.append('\\f'); case 'u' -> { b.append((char) Integer.parseInt(s.substring(i, i + 4), 16)); i += 4; }
                    default -> b.append(e); } } else b.append(d); } }
        if (s.startsWith("true", i)) { i += 4; return true; }
        if (s.startsWith("false", i)) { i += 5; return false; }
        if (s.startsWith("null", i)) { i += 4; return null; }
        int st = i; while (i < s.length() && "+-0123456789.eE".indexOf(s.charAt(i)) >= 0) i++;
        String n = s.substring(st, i);
        if (n.contains(".") || n.contains("e") || n.contains("E")) return Double.parseDouble(n);
        long v = Long.parseLong(n); return (v >= Integer.MIN_VALUE && v <= Integer.MAX_VALUE) ? (Object) (int) v : (Object) v;
    }
    private void ws() { while (i < s.length() && Character.isWhitespace(s.charAt(i))) i++; }
    static String str(Object o) {
        if (o == null) return "null";
        if (o instanceof String t) { StringBuilder b = new StringBuilder("\\"");
            for (char c : t.toCharArray()) { switch (c) { case '"' -> b.append("\\\\\\""); case '\\\\' -> b.append("\\\\\\\\"); case '\\n' -> b.append("\\\\n");
                case '\\r' -> b.append("\\\\r"); case '\\t' -> b.append("\\\\t"); default -> { if (c < 0x20) b.append(String.format("\\\\u%04x", (int) c)); else b.append(c); } } }
            return b.append('"').toString(); }
        if (o instanceof Map<?, ?> m) { StringJoiner j = new StringJoiner(",", "{", "}"); for (var e : m.entrySet()) j.add(str(String.valueOf(e.getKey())) + ":" + str(e.getValue())); return j.toString(); }
        if (o instanceof Collection<?> c) { StringJoiner j = new StringJoiner(",", "[", "]"); for (Object x : c) j.add(str(x)); return j.toString(); }
        if (o.getClass().isArray() && o instanceof Object[] arr) return str(Arrays.asList(arr));
        if (o instanceof Double d && (d.isNaN() || d.isInfinite())) return "null";
        if (o instanceof Number || o instanceof Boolean) return o.toString();
        return str(o.toString());
    }
}
"""

_BASH = """set -o pipefail
USER_CODE
"""


@dataclass
class Language:
    key: str
    label: str
    ext: str
    commands: list[str]                       # executable names to look for
    argv: Callable[[str, str], list[str]]     # (exe, script path) -> argv
    wrapper: str | None = None                # None: run the code as-is (shell style)
    version_args: list[str] = field(default_factory=lambda: ["--version"])
    indent: str = ""
    hoist_imports: str | None = None          # regex for import lines moved to USER_IMPORTS
    filename: str | None = None               # fixed script name (Java: Main.java)


LANGUAGES: dict[str, Language] = {l.key: l for l in [
    Language("javascript", "JavaScript (Node.js)", ".mjs", ["node"], lambda exe, f: [exe, f], _JS, indent="  "),
    Language("typescript", "TypeScript (Node.js)", ".mts", ["node"], lambda exe, f: [exe, "--no-warnings", f],
             _JS.replace("async function __user(items, item, index, $json) {",
                         "async function __user(items: any[], item: any, index: number, $json: any): Promise<any> {"),
             indent="  "),
    Language("bash", "Bash", ".sh", ["bash"], lambda exe, f: [exe, f], _BASH),
    Language("powershell", "PowerShell", ".ps1", ["pwsh", "powershell"],
             lambda exe, f: [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", f], _PS,
             version_args=["-NoProfile", "-Command", "$PSVersionTable.PSVersion.ToString()"], indent="  "),
    Language("perl", "Perl", ".pl", ["perl"], lambda exe, f: [exe, f], _PERL, indent="    "),
    Language("java", "Java", ".java", ["java"], lambda exe, f: [exe, f], _JAVA, version_args=["-version"], indent="        ",
             hoist_imports=r"^\s*import\s+[\w.*]+\s*;\s*$", filename="Main.java"),
    Language("ruby", "Ruby", ".rb", ["ruby"], lambda exe, f: [exe, f], _RUBY, indent="  "),
    Language("php", "PHP", ".php", ["php"], lambda exe, f: [exe, f], _PHP, indent="  "),
    Language("go", "Go", ".go", ["go"], lambda exe, f: [exe, "run", f], _GO, version_args=["version"], indent="\t",
             hoist_imports=r'^\s*import\s+(\w+\s+)?"[^"]+"\s*$', filename="main.go"),
]}

# ---------------------------------------------------------------------------
# Finding runtimes
# ---------------------------------------------------------------------------


def _git_tools() -> list[Path]:
    """Git for Windows ships bash and perl; prefer them over WSL's System32\\bash.exe stub."""
    roots = []
    git = shutil.which("git")
    if git:
        roots.append(Path(git).resolve().parent.parent)
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        if os.environ.get(env):
            roots += [Path(os.environ[env]) / "Git", Path(os.environ[env]) / "Programs" / "Git"]
    return [d for r in roots for d in (r / "bin", r / "usr" / "bin") if d.is_dir()]


def find_runtime(key: str) -> str | None:
    override = os.environ.get(f"DEBBUILDER_RUNTIME_{key.upper()}")
    if override:
        return override
    lang = LANGUAGES[key]
    for cmd in lang.commands:
        if sys.platform == "win32" and cmd in ("bash", "perl"):
            for d in _git_tools():
                p = d / f"{cmd}.exe"
                if p.is_file():
                    return str(p)
            continue  # never fall back to the WSL launcher: it can't see Windows temp paths
        p = shutil.which(cmd)
        if p:
            return p
    return None


@lru_cache(maxsize=None)
def _version(exe: str, args: tuple[str, ...]) -> str:
    try:
        r = subprocess.run([exe, *args], capture_output=True, text=True, timeout=15)
        text = (r.stdout or r.stderr).strip().splitlines()
        return text[0][:80] if text else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def runtimes_status() -> list[dict]:
    out = [{"key": "python", "label": "Python", "available": True, "path": sys.executable,
            "version": sys.version.split()[0], "builtin": True}]
    for lang in LANGUAGES.values():
        exe = find_runtime(lang.key)
        out.append({"key": lang.key, "label": lang.label, "available": bool(exe), "path": exe,
                    "version": _version(exe, tuple(lang.version_args)) if exe else None,
                    "install_hint": None if exe else f"Install {lang.label.split(' (')[0]} on the server, or set DEBBUILDER_RUNTIME_{lang.key.upper()}"})
    return out


# ---------------------------------------------------------------------------
# Running code
# ---------------------------------------------------------------------------


class CodeError(Exception):
    pass


def _render(lang: Language, code: str) -> tuple[str, int]:
    """Wrapper with the user's code inserted; returns (source, line offset of the user's first line)."""
    imports: list[str] = []
    if lang.hoist_imports:
        keep = []
        for line in code.splitlines():
            (imports if re.match(lang.hoist_imports, line) else keep).append(line)
        code = "\n".join(keep)
    if lang.key == "go":
        imports = [re.sub(r"^\s*import\s+", "\t", l) for l in imports]
        imports = [l for l in imports if l.strip() not in lang.wrapper]  # Go rejects duplicate imports
    body = "\n".join((lang.indent + l) if l.strip() else l for l in code.splitlines()) or f"{lang.indent}"
    src = lang.wrapper.replace("USER_IMPORTS", "\n".join(imports) + ("\n" if imports else ""))
    head = src.split("USER_CODE")[0]
    return src.replace("USER_CODE", body), head.count("\n")


def _fix_lines(text: str, script: str, offset: int) -> str:
    """Point error line numbers at the user's code ("line 5") instead of the generated wrapper file."""
    name = re.escape(Path(script).name)

    def sub(m: re.Match) -> str:
        n = int(m.group(1)) - offset
        return f"line {n}" if n > 0 else m.group(0)

    # The path part may be a file:// URL or a Windows short path (C:\Users\NAME~1\...).
    return re.sub(rf"[^\s'\"()]*{name}:(\d+)", sub, text)


def _parse_stdout(stdout: str, base: list[dict]) -> list:
    text = stdout.strip()
    if not text:
        return base  # nothing printed: pass the items through
    try:
        data = json.loads(text)
    except ValueError:
        return [{**(base[0] if len(base) == 1 else {}), "stdout": text}]
    return data if isinstance(data, list) else [data]


def run_code(language: str, code: str, items: list[dict], *, mode: str, vars: dict, nodes: dict,
             execution_id: str, timeout: float, command: str | None = None, ext: str | None = None) -> tuple[list, list[str]]:
    """Run code in a subprocess; returns (output items, log lines). Blocking: call via a thread."""
    custom = language == "custom"
    lang = None if custom else LANGUAGES.get(language)
    if not custom and not lang:
        raise CodeError(f"Unknown language '{language}'")
    if custom:
        if not command or "{file}" not in command:
            raise CodeError("Custom command must contain {file}, e.g. `deno run -A {file}` or `Rscript {file}`")
    else:
        exe = find_runtime(language)
        if not exe:
            raise CodeError(f"{lang.label} isn't installed on the server ({' / '.join(lang.commands)} not found on PATH). "
                            f"Install it, or set DEBBUILDER_RUNTIME_{language.upper()} to its executable.")

    tmp = Path(tempfile.mkdtemp(prefix="debbuilder_code_"))
    logs: list[str] = []
    try:
        shell_style = custom or lang.wrapper == _BASH
        if custom:
            script_name = f"code{ext if (ext or '').startswith('.') else '.' + (ext or 'txt')}"
            source, offset = code, 0
        else:
            script_name = lang.filename or f"code{lang.ext}"
            source, offset = _render(lang, code)
        script = tmp / script_name
        script.write_text(source, encoding="utf-8", newline="\n")
        out_file, in_file = tmp / "output.json", tmp / "input.json"

        def invoke(payload: dict, extra_env: dict) -> tuple[subprocess.CompletedProcess, list]:
            in_file.write_text(json.dumps(payload, default=str), encoding="utf-8")
            out_file.unlink(missing_ok=True)
            env = {**os.environ, "DEBBUILDER_INPUT": in_file.as_posix(), "DEBBUILDER_OUTPUT": out_file.as_posix(),
                   "ITEMS_FILE": in_file.as_posix(), "PYTHONIOENCODING": "utf-8", **extra_env}
            items_json = json.dumps(payload["items"], default=str)
            if len(items_json) < 24000:  # Windows caps the environment block at 32K
                env["ITEMS"] = items_json
            argv = command.replace("{file}", f'"{script.as_posix()}"') if custom else lang.argv(exe, script.as_posix())
            try:
                proc = subprocess.run(argv, cwd=tmp, env=env, capture_output=True, text=True, encoding="utf-8",
                                      errors="replace", timeout=timeout, shell=custom)
            except subprocess.TimeoutExpired as e:
                raise CodeError(f"Timed out after {timeout:.0f}s (raise 'Timeout' in the node settings)") from e
            except OSError as e:
                raise CodeError(f"Could not start the runtime: {e}") from e
            for stream in (proc.stdout if not shell_style else "", proc.stderr):
                logs.extend(l for l in (stream or "").splitlines() if l.strip())
            if proc.returncode != 0:
                detail = _fix_lines((proc.stderr or proc.stdout or "").strip(), script_name, offset)
                raise CodeError(f"Exited with code {proc.returncode}: {detail[-1500:] or 'no error output'}")
            if out_file.exists() and out_file.stat().st_size:
                raw = out_file.read_text(encoding="utf-8-sig")
                try:
                    data = json.loads(raw)
                except ValueError as e:
                    raise CodeError(f"The code wrote invalid JSON to DEBBUILDER_OUTPUT: {raw[:200]}") from e
                if shell_style and proc.stdout:
                    logs.extend(l for l in proc.stdout.splitlines() if l.strip())
                return proc, data if isinstance(data, list) else [data]
            if shell_style:
                return proc, _parse_stdout(proc.stdout, payload.get("_base", []))
            return proc, []

        base = {"vars": vars, "nodes": nodes, "execution_id": execution_id}
        if shell_style and mode == "each":
            # Shell code has no function to call per item: run the script once per item.
            out: list = []
            for i, it in enumerate(items):
                _, res = invoke({**base, "items": items, "item": it, "index": i, "mode": "each", "_base": [it]},
                                {"ITEM": json.dumps(it, default=str), "INDEX": str(i)})
                out.extend(res)
        else:
            _, out = invoke({**base, "items": items, "mode": mode, "_base": items},
                            {"ITEM": json.dumps(items[0] if items else {}, default=str), "INDEX": "0"})
        return [x if isinstance(x, dict) else {"value": x} for x in out], logs[:500]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
