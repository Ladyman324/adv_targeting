"""Capability-restricted parser fixture worker; not a general Python sandbox.

Only explicitly allowed parser/helper functions are compiled, with restricted
builtins and narrow library facades. Parent enforces process wall time. Never
import the proposed scraper module or execute its imports/top-level statements.
"""
from __future__ import annotations

import ast
import __future__
import json
import re
import sys
from types import SimpleNamespace
from lxml import etree, html

FUNCTIONS = {"extract_emails", "extract_bio", "find_profile_url"}
BUILTINS = {"str": str, "list": list, "dict": dict, "tuple": tuple, "set": set,
            "int": int, "bool": bool, "isinstance": isinstance, "sorted": sorted,
            "max": max, "min": min, "len": len, "any": any, "all": all, "enumerate": enumerate,
            "ValueError": ValueError, "TypeError": TypeError, "AttributeError": AttributeError,
            "KeyError": KeyError}
ATTRIBUTES = {"fromstring", "fragment_fromstring", "ParserError", "loads", "JSONDecodeError",
              "sub", "I", "match", "xpath", "text_content", "strip", "lower", "split", "rsplit",
              "startswith", "endswith", "append", "update", "get", "items", "values", "join"}
FORBIDDEN_NODES = (ast.Import, ast.ImportFrom, ast.ClassDef, ast.AsyncFunctionDef, ast.With,
                   ast.AsyncWith, ast.Global, ast.Nonlocal, ast.While, ast.Delete, ast.Await,
                   ast.Yield, ast.YieldFrom, ast.NamedExpr)
BUILTIN_REGRESSION = {"cases": [
    {"html": '<input name="field_email_override" value="original@example.invalid">',
     "expectedEmails": ["original@example.invalid"], "expectedBio": None},
    {"html": '<input data-drupal-selector="edit-field-email-override" value="second@example.invalid">',
     "expectedEmails": ["second@example.invalid"], "expectedBio": None},
    {"html": '<input name="field_email_override" value="not-an-email">', "expectedEmails": [], "expectedBio": None},
    {"html": '<html><body>No email field</body></html>', "expectedEmails": [], "expectedBio": None},
    {"html": '', "expectedEmails": [], "expectedBio": None},
    {"html": '<div data-widget="enhanced-fa-bio"><script type="application/json">'
             '{"content":"<p>First &amp; trusted</p><p>Second<br>line.</p>"}</script></div>',
     "expectedEmails": [], "expectedBio": "First & trusted\nSecond\nline."},
]}


def nested_values(value, path=""):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from nested_values(child, (path + "." + key) if path else key)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from nested_values(child, path + "[" + str(index) + "]")
    elif isinstance(value, str):
        yield path, value


def get_field(record, dotted_path):
    value = record
    for part in dotted_path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def restricted_namespace(source):
    if not isinstance(source, str) or len(source.encode("utf-8")) > 300000:
        raise ValueError("SOURCE_SIZE_INVALID")
    tree = ast.parse(source)
    definitions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS]
    if len({node.name for node in definitions}) != len(definitions):
        raise ValueError("DUPLICATE_FUNCTION")
    if not {"extract_emails", "extract_bio"}.issubset({node.name for node in definitions}):
        raise ValueError("PARSER_FUNCTION_MISSING")
    for definition in definitions:
        if definition.decorator_list or definition.args.defaults or any(x is not None for x in definition.args.kw_defaults):
            raise ValueError("FUNCTION_INITIALIZER_REFUSED")
        nodes = list(ast.walk(definition))
        if len(nodes) > 5000:
            raise ValueError("PARSER_AST_TOO_LARGE")
        for node in nodes:
            if isinstance(node, FORBIDDEN_NODES) or (isinstance(node, ast.FunctionDef) and node is not definition):
                raise ValueError("PARSER_CAPABILITY_REFUSED")
            if isinstance(node, ast.Name) and node.id.startswith("__"):
                raise ValueError("INTROSPECTION_REFUSED")
            if isinstance(node, ast.Attribute) and (node.attr not in ATTRIBUTES or not isinstance(node.ctx, ast.Load)):
                raise ValueError("ATTRIBUTE_CAPABILITY_REFUSED")
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    if node.func.id not in set(BUILTINS) | FUNCTIONS | {"get_field", "nested_values"}:
                        raise ValueError("CALL_CAPABILITY_REFUSED")
                elif not isinstance(node.func, ast.Attribute):
                    raise ValueError("DYNAMIC_CALL_REFUSED")
                if any(k.arg is None for k in node.keywords):
                    raise ValueError("DYNAMIC_KEYWORDS_REFUSED")
            if isinstance(node, ast.BinOp) and not isinstance(node.op, ast.Add):
                raise ValueError("EXPANSIVE_ARITHMETIC_REFUSED")
            if isinstance(node, ast.AugAssign) and not isinstance(node.op, (ast.Add, ast.Sub)):
                raise ValueError("EXPANSIVE_ARITHMETIC_REFUSED")
            if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)) and len(node.value) > 20000:
                raise ValueError("PARSER_CONSTANT_TOO_LARGE")
    namespace = {"__builtins__": dict(BUILTINS), "get_field": get_field, "nested_values": nested_values,
                 "html": SimpleNamespace(fromstring=html.fromstring, fragment_fromstring=html.fragment_fromstring),
                 "etree": SimpleNamespace(ParserError=etree.ParserError),
                 "json": SimpleNamespace(loads=json.loads, JSONDecodeError=json.JSONDecodeError),
                 "re": SimpleNamespace(sub=re.sub, I=re.I),
                 "EMAIL_RE": re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$"),
                 "URL_RE": re.compile(r"^https?://", re.I)}
    module = ast.Module(body=definitions, type_ignores=[])
    exec(compile(module, "<restricted-parser>", "exec", flags=__future__.annotations.compiler_flag), namespace)
    return namespace


def check_cases(namespace, fixture):
    results = []
    for case in fixture["cases"]:
        try:
            emails, status = namespace["extract_emails"](case["html"])
            bio = namespace["extract_bio"](case["html"])
            passed = (emails == case["expectedEmails"] and bio == case["expectedBio"]
                      and isinstance(status, str) and status in {"ok", "field_missing", "field_empty", "invalid_html"})
            results.append({"passed": bool(passed)})
        except BaseException as exc:
            results.append({"passed": False, "errorType": type(exc).__name__})
    # Deterministic profile URL cases are never sent to the proposal model.
    if "find_profile_url" in namespace:
        for record, field, expected in [
            ({"faUrl": "/us-en/financial-advisor/fixture-one", "faImage": "https://example.com/photo.jpg"}, None,
             "/us-en/financial-advisor/fixture-one"),
            ({"nested": {"profile": "https://www.edwardjones.com/us-en/financial-advisor/fixture-two"}}, "nested.profile",
             "https://www.edwardjones.com/us-en/financial-advisor/fixture-two"),
            ({"other": "Not a URL"}, None, None),
        ]:
            try:
                results.append({"passed": namespace["find_profile_url"](record, field) == expected})
            except BaseException as exc:
                results.append({"passed": False, "errorType": type(exc).__name__})
    return results


def assess(payload):
    old = restricted_namespace(payload["original"])
    proposed = restricted_namespace(payload["replacement"])
    before_failure = check_cases(old, payload["failureFixture"])
    after_failure = check_cases(proposed, payload["failureFixture"])
    before_regression = check_cases(old, payload["regressionFixture"]) + check_cases(old, BUILTIN_REGRESSION)
    after_regression = check_cases(proposed, payload["regressionFixture"]) + check_cases(proposed, BUILTIN_REGRESSION)
    reproduced = any(not item["passed"] for item in before_failure)
    fixed = all(item["passed"] for item in after_failure)
    regression = all(item["passed"] for item in before_regression + after_regression)
    return {"status": "passed" if reproduced and fixed and regression else "failed",
            "failureReproduced": reproduced, "failureFixed": fixed, "regressionsPassed": regression,
            "beforeFailure": before_failure, "afterFailure": after_failure,
            "beforeRegression": before_regression, "afterRegression": after_regression}


def _audit(event, args):
    if event == "open" or event.startswith(("socket.", "subprocess.", "os.", "ctypes.")):
        raise PermissionError("WORKER_EXTERNAL_CAPABILITY_REFUSED")


def main():
    try:
        if sys.platform != "win32":
            import resource
            memory = 256 * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        raw = sys.stdin.buffer.read(500001)
        if len(raw) > 500000:
            raise ValueError("PAYLOAD_TOO_LARGE")
        payload = json.loads(raw)
        # All trusted libraries are loaded before denying external capabilities.
        sys.addaudithook(_audit)
        result = assess(payload)
    except BaseException as exc:
        result = {"status": "failed", "errorType": type(exc).__name__, "errorCode": "RESTRICTED_PARSER_TEST_FAILED"}
    sys.stdout.write(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
