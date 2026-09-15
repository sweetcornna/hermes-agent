"""Execute the reviewed evaluation examples and assert their behavior."""
import ast
import contextlib
import io
import itertools
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent


def rows(name):
    return [json.loads(line) for line in (HERE / name).read_text().splitlines()]


def code(text):
    return "\n".join(re.findall(r"```python\s*\n(.*?)```", text, flags=re.S))


def namespace(text, initial=None):
    tree = ast.parse(code(text))
    # These reviewed examples need no I/O, imports, dynamic eval or attributes
    # other than list.sort. Fail closed if a future response changes that scope.
    for node in ast.walk(tree):
        assert not isinstance(node, (ast.Import, ast.ImportFrom, ast.With, ast.AsyncWith))
        if isinstance(node, ast.Attribute):
            assert node.attr == "sort" and isinstance(node.value, ast.Name) and node.value.id == "nums"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id in {"len", "print", "sorted", "binary_search_left", "binary_search_leftmost"}
    env = {"__builtins__": {"len": len, "print": print, "sorted": sorted}, **(initial or {})}
    with contextlib.redirect_stdout(io.StringIO()):
        exec(compile(tree, "<reviewed-example>", "exec"), env)
    return env


checks = []
regression = [r for r in rows("regression-v1.jsonl") if r["variant"] == "before"] + rows("final-regression.jsonl")
for row in regression:
    if row["id"] != "R23":
        continue
    env = namespace(row["output"])
    function = next(value for key, value in env.items() if key.startswith("binary_search_"))
    count = 0
    for length in range(7):
        for values in itertools.combinations_with_replacement(range(4), length):
            for target in range(-1, 5):
                array = list(values)
                expected = array.index(target) if target in array else -1
                assert function(array, target) == expected
                count += 1
    checks.append({"id": "R23", "variant": row["variant"], "assertions": count, "passed": True})

for row in rows("final-holdout.jsonl"):
    if row["id"] != "F04":
        continue
    original = [3, 1, 2]
    env = namespace(row["output"], {"nums": original})
    assert env["nums"] == [3, 1, 2]
    assert env["result"] == [1, 2, 3] and env["result"] is not env["nums"]
    if row["turn"] == 2:
        assert env["nums"] is original, "Continuation should preserve the supplied list"
    checks.append({"id": "F04", "variant": row["variant"], "replicate": row["replicate"],
                   "turn": row["turn"], "original_preserved": True, "new_sorted_list": True, "passed": True})

result = {"passed": True, "checks": checks}
(HERE / "code-check.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result))
