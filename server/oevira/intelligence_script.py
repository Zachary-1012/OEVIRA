"""A small, no-OS Python subset for user-reviewed Skill scripts.

This interpreter evaluates a deliberately limited AST. It has no imports,
attributes, reflection, filesystem, process, socket or arbitrary Python eval;
its only effect is a statically named call through the current product Tool
Router. Packages requiring external dependencies or general Python remain
unsupported and fail closed.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re


MAX_SCRIPT_BYTES = 128 * 1024
MAX_SCRIPT_AST_NODES = 4000
MAX_SCRIPT_STEPS = 2500
MAX_SCRIPT_LOOP_ITEMS = 64
MAX_SCRIPT_TOOL_CALLS = 8
MAX_SCRIPT_VALUE_BYTES = 128 * 1024
MAX_SCRIPT_VALUE_DEPTH = 24
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
_ALLOWED_BINARY = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod)
_ALLOWED_COMPARE = (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn)
_ALLOWED_BUILTINS = {"len", "str", "int", "float", "bool", "min", "max", "sorted", "range"}


class RestrictedScriptError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _ReturnValue(Exception):
    def __init__(self, value):
        self.value = value


class _BreakLoop(Exception):
    pass


class _ContinueLoop(Exception):
    pass


class _ScriptInspector(ast.NodeVisitor):
    def __init__(self):
        self.tool_names: set[str] = set()
        self.nodes = 0

    def visit(self, node):
        self.nodes += 1
        if self.nodes > MAX_SCRIPT_AST_NODES:
            raise RestrictedScriptError("RESTRICTED_SCRIPT_AST_TOO_LARGE")
        return super().visit(node)

    def generic_visit(self, node):
        allowed = (
            ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return, ast.Assign, ast.Expr,
            ast.If, ast.For, ast.Pass, ast.Break, ast.Continue, ast.AugAssign, ast.Name, ast.Load, ast.Store, ast.Constant,
            ast.Dict, ast.List, ast.Tuple, ast.Subscript, ast.BinOp, ast.UnaryOp, ast.BoolOp,
            ast.Compare, ast.IfExp, ast.Call, ast.keyword, ast.JoinedStr, ast.FormattedValue,
            ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Eq, ast.NotEq,
            ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.And, ast.Or, ast.Not,
            ast.USub, ast.UAdd,
        )
        if not isinstance(node, allowed):
            raise RestrictedScriptError("RESTRICTED_SCRIPT_SYNTAX_UNSUPPORTED")
        super().generic_visit(node)

    def visit_Module(self, node: ast.Module):
        self.generic_visit(node)
        if len(node.body) != 1 or not isinstance(node.body[0], ast.FunctionDef):
            raise RestrictedScriptError("RESTRICTED_SCRIPT_ENTRYPOINT_INVALID")
        entrypoint = node.body[0]
        if (entrypoint.name != "run" or entrypoint.decorator_list or entrypoint.returns is not None
                or entrypoint.type_comment is not None or len(entrypoint.args.args) != 1
                or entrypoint.args.args[0].arg != "args" or entrypoint.args.posonlyargs
                or entrypoint.args.kwonlyargs or entrypoint.args.vararg or entrypoint.args.kwarg
                or entrypoint.args.defaults or entrypoint.args.kw_defaults):
            raise RestrictedScriptError("RESTRICTED_SCRIPT_ENTRYPOINT_INVALID")

    def visit_Name(self, node: ast.Name):
        if node.id.startswith("__") or node.id in {"open", "exec", "eval", "compile", "globals", "locals", "getattr", "setattr", "delattr", "type", "object", "super", "import"}:
            raise RestrictedScriptError("RESTRICTED_SCRIPT_NAME_DENIED")
        if isinstance(node.ctx, ast.Store) and not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,63}", node.id):
            raise RestrictedScriptError("RESTRICTED_SCRIPT_VARIABLE_INVALID")

    def visit_FunctionDef(self, node: ast.FunctionDef):
        if node.name != "run":
            raise RestrictedScriptError("RESTRICTED_SCRIPT_FUNCTION_INVALID")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call):
        if not isinstance(node.func, ast.Name) or node.keywords:
            raise RestrictedScriptError("RESTRICTED_SCRIPT_CALL_UNSUPPORTED")
        if node.func.id == "call":
            if (len(node.args) != 2 or not isinstance(node.args[0], ast.Constant)
                    or not isinstance(node.args[0].value, str) or not _TOOL_NAME.fullmatch(node.args[0].value)):
                raise RestrictedScriptError("RESTRICTED_SCRIPT_TOOL_CALL_INVALID")
            if not isinstance(node.args[1], ast.Dict):
                raise RestrictedScriptError("RESTRICTED_SCRIPT_TOOL_ARGUMENTS_INVALID")
            self.tool_names.add(node.args[0].value)
        elif node.func.id not in _ALLOWED_BUILTINS:
            raise RestrictedScriptError("RESTRICTED_SCRIPT_CALL_UNSUPPORTED")
        self.generic_visit(node)

    def visit_Attribute(self, node):
        raise RestrictedScriptError("RESTRICTED_SCRIPT_ATTRIBUTE_ACCESS_DENIED")

    def visit_Import(self, node):
        raise RestrictedScriptError("RESTRICTED_SCRIPT_IMPORT_DENIED")

    def visit_ImportFrom(self, node):
        raise RestrictedScriptError("RESTRICTED_SCRIPT_IMPORT_DENIED")

    def visit_While(self, node):
        raise RestrictedScriptError("RESTRICTED_SCRIPT_LOOP_UNSUPPORTED")

    def visit_AsyncFunctionDef(self, node):
        raise RestrictedScriptError("RESTRICTED_SCRIPT_ASYNC_UNSUPPORTED")

    def visit_Lambda(self, node):
        raise RestrictedScriptError("RESTRICTED_SCRIPT_LAMBDA_DENIED")


def inspect_script(source: str) -> dict:
    if (not isinstance(source, str) or not source or "\x00" in source
            or len(source.encode("utf-8")) > MAX_SCRIPT_BYTES):
        raise RestrictedScriptError("RESTRICTED_SCRIPT_SIZE_INVALID")
    try:
        tree = ast.parse(source, mode="exec", type_comments=False)
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise RestrictedScriptError("RESTRICTED_SCRIPT_SYNTAX_INVALID") from exc
    inspector = _ScriptInspector()
    inspector.visit(tree)
    return {
        "entrypoint": "run", "tool_names": sorted(inspector.tool_names),
        "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "ast_nodes": inspector.nodes,
        "runtime": "restricted_python_ast_v1",
        "capabilities": ["declared_product_tools_only"],
        "os_access": "DENIED",
        "network_access": "DENIED",
        "file_access": "DENIED",
        "dependencies": [],
    }


class _Interpreter:
    def __init__(self, *, arguments: dict, tool_caller, allowed_tools: set[str], is_cancelled=None):
        self.env = {"args": arguments}
        self.tool_caller = tool_caller
        self.allowed_tools = allowed_tools
        self.steps = 0
        self.tool_calls = 0
        self.loop_depth = 0
        self.is_cancelled = is_cancelled

    def _step(self):
        if self.is_cancelled is not None:
            try:
                cancelled = self.is_cancelled()
            except Exception as exc:
                raise RestrictedScriptError("RESTRICTED_SCRIPT_CANCEL_CHECK_FAILED") from exc
            if type(cancelled) is not bool:
                raise RestrictedScriptError("RESTRICTED_SCRIPT_CANCEL_CHECK_INVALID")
            if cancelled:
                raise RestrictedScriptError("RESTRICTED_SCRIPT_CANCELLED")
        self.steps += 1
        if self.steps > MAX_SCRIPT_STEPS:
            raise RestrictedScriptError("RESTRICTED_SCRIPT_STEP_BUDGET_EXCEEDED")

    @staticmethod
    def _bounded(value):
        try:
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise RestrictedScriptError("RESTRICTED_SCRIPT_VALUE_INVALID") from exc
        if len(encoded) > MAX_SCRIPT_VALUE_BYTES:
            raise RestrictedScriptError("RESTRICTED_SCRIPT_VALUE_TOO_LARGE")

        def depth(item, current=0):
            if current > MAX_SCRIPT_VALUE_DEPTH:
                raise RestrictedScriptError("RESTRICTED_SCRIPT_VALUE_TOO_DEEP")
            if isinstance(item, dict):
                if len(item) > 256:
                    raise RestrictedScriptError("RESTRICTED_SCRIPT_VALUE_TOO_LARGE")
                for key, child in item.items():
                    if not isinstance(key, str) or len(key) > 256:
                        raise RestrictedScriptError("RESTRICTED_SCRIPT_VALUE_INVALID")
                    depth(child, current + 1)
            elif isinstance(item, list):
                if len(item) > 256:
                    raise RestrictedScriptError("RESTRICTED_SCRIPT_VALUE_TOO_LARGE")
                for child in item:
                    depth(child, current + 1)
            elif isinstance(item, str) and len(item) > MAX_SCRIPT_VALUE_BYTES:
                raise RestrictedScriptError("RESTRICTED_SCRIPT_VALUE_TOO_LARGE")

        depth(value)
        return value

    def run(self, tree: ast.Module):
        function = tree.body[0]
        try:
            for statement in function.body:
                self._statement(statement)
        except _ReturnValue as returned:
            return self._bounded(returned.value)
        return None

    def _statement(self, node):
        self._step()
        if isinstance(node, ast.Return):
            raise _ReturnValue(self._expression(node.value) if node.value is not None else None)
        if isinstance(node, ast.Assign):
            if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                raise RestrictedScriptError("RESTRICTED_SCRIPT_ASSIGNMENT_INVALID")
            self.env[node.targets[0].id] = self._bounded(self._expression(node.value))
            return
        if isinstance(node, ast.AugAssign):
            if not isinstance(node.target, ast.Name) or not isinstance(node.op, ast.Add):
                raise RestrictedScriptError("RESTRICTED_SCRIPT_ASSIGNMENT_INVALID")
            value = self.env.get(node.target.id)
            self.env[node.target.id] = self._bounded(value + self._expression(node.value))
            return
        if isinstance(node, ast.Expr):
            self._expression(node.value)
            return
        if isinstance(node, ast.If):
            branch = node.body if self._expression(node.test) else node.orelse
            for statement in branch:
                self._statement(statement)
            return
        if isinstance(node, ast.For):
            if not isinstance(node.target, ast.Name) or self.loop_depth >= 8:
                raise RestrictedScriptError("RESTRICTED_SCRIPT_LOOP_INVALID")
            items = self._expression(node.iter)
            if not isinstance(items, (list, tuple, range)) or len(items) > MAX_SCRIPT_LOOP_ITEMS:
                raise RestrictedScriptError("RESTRICTED_SCRIPT_LOOP_BUDGET_EXCEEDED")
            self.loop_depth += 1
            try:
                for item in items:
                    self.env[node.target.id] = item
                    try:
                        for statement in node.body:
                            self._statement(statement)
                    except _ContinueLoop:
                        continue
                    except _BreakLoop:
                        break
                for statement in node.orelse:
                    self._statement(statement)
            finally:
                self.loop_depth -= 1
            return
        if isinstance(node, ast.Break):
            if self.loop_depth <= 0:
                raise RestrictedScriptError("RESTRICTED_SCRIPT_LOOP_INVALID")
            raise _BreakLoop
        if isinstance(node, ast.Continue):
            if self.loop_depth <= 0:
                raise RestrictedScriptError("RESTRICTED_SCRIPT_LOOP_INVALID")
            raise _ContinueLoop
        if isinstance(node, ast.Pass):
            return
        raise RestrictedScriptError("RESTRICTED_SCRIPT_SYNTAX_UNSUPPORTED")

    def _expression(self, node):
        self._step()
        if isinstance(node, ast.Constant):
            if type(node.value) not in {str, int, float, bool, type(None)}:
                raise RestrictedScriptError("RESTRICTED_SCRIPT_CONSTANT_INVALID")
            return self._bounded(node.value)
        if isinstance(node, ast.Name):
            if node.id in self.env:
                return self.env[node.id]
            if node.id in _ALLOWED_BUILTINS:
                return node.id
            raise RestrictedScriptError("RESTRICTED_SCRIPT_NAME_UNBOUND")
        if isinstance(node, (ast.List, ast.Tuple)):
            if len(node.elts) > 256:
                raise RestrictedScriptError("RESTRICTED_SCRIPT_VALUE_TOO_LARGE")
            return self._bounded([self._expression(item) for item in node.elts])
        if isinstance(node, ast.Dict):
            if len(node.keys) > 256:
                raise RestrictedScriptError("RESTRICTED_SCRIPT_VALUE_TOO_LARGE")
            result = {}
            for key_node, value_node in zip(node.keys, node.values):
                key = self._expression(key_node)
                if not isinstance(key, str) or key in result:
                    raise RestrictedScriptError("RESTRICTED_SCRIPT_DICTIONARY_INVALID")
                result[key] = self._expression(value_node)
            return self._bounded(result)
        if isinstance(node, ast.Subscript):
            if isinstance(node.slice, ast.Slice):
                raise RestrictedScriptError("RESTRICTED_SCRIPT_SLICE_UNSUPPORTED")
            value = self._expression(node.value)
            index = self._expression(node.slice)
            if not isinstance(value, (dict, list, tuple, str)) or type(index) not in {str, int}:
                raise RestrictedScriptError("RESTRICTED_SCRIPT_SUBSCRIPT_INVALID")
            try:
                return self._bounded(value[index])
            except (KeyError, IndexError, TypeError) as exc:
                raise RestrictedScriptError("RESTRICTED_SCRIPT_SUBSCRIPT_MISSING") from exc
        if isinstance(node, ast.BinOp):
            left, right = self._expression(node.left), self._expression(node.right)
            if not isinstance(node.op, _ALLOWED_BINARY):
                raise RestrictedScriptError("RESTRICTED_SCRIPT_OPERATOR_UNSUPPORTED")
            if isinstance(node.op, ast.Add):
                value = left + right
            elif isinstance(node.op, ast.Sub):
                value = left - right
            elif isinstance(node.op, ast.Mult):
                if isinstance(left, (str, list)) and type(right) is int:
                    if len(left) * max(0, right) > MAX_SCRIPT_VALUE_BYTES:
                        raise RestrictedScriptError("RESTRICTED_SCRIPT_VALUE_TOO_LARGE")
                elif isinstance(right, (str, list)) and type(left) is int:
                    if len(right) * max(0, left) > MAX_SCRIPT_VALUE_BYTES:
                        raise RestrictedScriptError("RESTRICTED_SCRIPT_VALUE_TOO_LARGE")
                value = left * right
            elif isinstance(node.op, ast.Div):
                value = left / right
            elif isinstance(node.op, ast.FloorDiv):
                value = left // right
            else:
                value = left % right
            return self._bounded(value)
        if isinstance(node, ast.UnaryOp):
            value = self._expression(node.operand)
            if isinstance(node.op, ast.Not):
                return not value
            if isinstance(node.op, ast.USub):
                return self._bounded(-value)
            if isinstance(node.op, ast.UAdd):
                return self._bounded(+value)
            raise RestrictedScriptError("RESTRICTED_SCRIPT_OPERATOR_UNSUPPORTED")
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                result = True
                for value in node.values:
                    result = self._expression(value)
                    if not result:
                        return result
                return result
            result = False
            for value in node.values:
                result = self._expression(value)
                if result:
                    return result
            return result
        if isinstance(node, ast.Compare):
            left = self._expression(node.left)
            for operator, comparator in zip(node.ops, node.comparators):
                right = self._expression(comparator)
                if isinstance(operator, ast.Eq):
                    matched = left == right
                elif isinstance(operator, ast.NotEq):
                    matched = left != right
                elif isinstance(operator, ast.Lt):
                    matched = left < right
                elif isinstance(operator, ast.LtE):
                    matched = left <= right
                elif isinstance(operator, ast.Gt):
                    matched = left > right
                elif isinstance(operator, ast.GtE):
                    matched = left >= right
                elif isinstance(operator, ast.In):
                    matched = left in right
                elif isinstance(operator, ast.NotIn):
                    matched = left not in right
                else:
                    raise RestrictedScriptError("RESTRICTED_SCRIPT_OPERATOR_UNSUPPORTED")
                if not matched:
                    return False
                left = right
            return True
        if isinstance(node, ast.IfExp):
            branch = node.body if self._expression(node.test) else node.orelse
            return self._expression(branch)
        if isinstance(node, ast.JoinedStr):
            parts = []
            for item in node.values:
                if isinstance(item, ast.Constant) and isinstance(item.value, str):
                    parts.append(item.value)
                elif isinstance(item, ast.FormattedValue):
                    value = self._expression(item.value)
                    if item.format_spec is not None:
                        if (len(item.format_spec.values) != 1 or not isinstance(item.format_spec.values[0], ast.Constant)
                                or not isinstance(item.format_spec.values[0].value, str)
                                or len(item.format_spec.values[0].value) > 32):
                            raise RestrictedScriptError("RESTRICTED_SCRIPT_FORMAT_INVALID")
                        format_spec = item.format_spec.values[0].value
                    else:
                        format_spec = ""
                    parts.append(format(value, format_spec))
                else:
                    raise RestrictedScriptError("RESTRICTED_SCRIPT_FORMAT_INVALID")
            return self._bounded("".join(parts))
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.keywords:
                raise RestrictedScriptError("RESTRICTED_SCRIPT_CALL_UNSUPPORTED")
            values = [self._expression(item) for item in node.args]
            if node.func.id == "call":
                if len(values) != 2 or not isinstance(values[0], str) or values[0] not in self.allowed_tools:
                    raise RestrictedScriptError("RESTRICTED_SCRIPT_TOOL_NOT_GRANTED")
                if values[0] == "skill.script.run" or values[0] == "skill.script.call":
                    raise RestrictedScriptError("RESTRICTED_SCRIPT_RECURSION_DENIED")
                if not isinstance(values[1], dict) or self.tool_calls >= MAX_SCRIPT_TOOL_CALLS:
                    raise RestrictedScriptError("RESTRICTED_SCRIPT_TOOL_BUDGET_EXCEEDED")
                self.tool_calls += 1
                return self._bounded(self.tool_caller(values[0], values[1]))
            builtins = {
                "len": len, "str": str, "int": int, "float": float, "bool": bool,
                "min": min, "max": max, "sorted": sorted,
            }
            if node.func.id == "range":
                if not 1 <= len(values) <= 3 or any(type(item) is not int for item in values):
                    raise RestrictedScriptError("RESTRICTED_SCRIPT_RANGE_INVALID")
                result = range(*values)
                if len(result) > MAX_SCRIPT_LOOP_ITEMS:
                    raise RestrictedScriptError("RESTRICTED_SCRIPT_LOOP_BUDGET_EXCEEDED")
                return result
            function = builtins.get(node.func.id)
            if function is None:
                raise RestrictedScriptError("RESTRICTED_SCRIPT_CALL_UNSUPPORTED")
            if node.func.id in {"int", "float"} and any(
                isinstance(value, str) and len(value) > 1024 for value in values
            ):
                raise RestrictedScriptError("RESTRICTED_SCRIPT_CONVERSION_TOO_LARGE")
            return self._bounded(function(*values))
        raise RestrictedScriptError("RESTRICTED_SCRIPT_EXPRESSION_UNSUPPORTED")


def run_restricted_script(source: str, arguments: dict, tool_caller, *, allowed_tools: set[str],
                          is_cancelled=None) -> dict:
    if not callable(tool_caller) or not isinstance(arguments, dict) or not isinstance(allowed_tools, set):
        raise RestrictedScriptError("RESTRICTED_SCRIPT_CONTEXT_INVALID")
    try:
        argument_bytes = json.dumps(
            arguments, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RestrictedScriptError("RESTRICTED_SCRIPT_ARGUMENTS_INVALID") from exc
    if len(argument_bytes) > MAX_SCRIPT_VALUE_BYTES:
        raise RestrictedScriptError("RESTRICTED_SCRIPT_ARGUMENTS_TOO_LARGE")
    metadata = inspect_script(source)
    if not set(metadata["tool_names"]).issubset(allowed_tools):
        raise RestrictedScriptError("RESTRICTED_SCRIPT_TOOL_NOT_GRANTED")
    try:
        tree = ast.parse(source, mode="exec", type_comments=False)
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise RestrictedScriptError("RESTRICTED_SCRIPT_SYNTAX_INVALID") from exc
    interpreter = _Interpreter(arguments=arguments, tool_caller=tool_caller, allowed_tools=allowed_tools,
                               is_cancelled=is_cancelled)
    result = interpreter.run(tree)
    return {
        "state": "completed", "value": result,
        "tool_calls": interpreter.tool_calls, "steps": interpreter.steps,
        "script_sha256": metadata["source_sha256"], "runtime": metadata["runtime"],
        "isolation": {"os": False, "filesystem": False, "network": False, "processes": False},
    }
