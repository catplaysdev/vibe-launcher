"""Validate catalog keys and named fields; --sync adds new English source strings."""
import argparse
import ast
import json
import re
from pathlib import Path
from string import Formatter

ROOT = Path(__file__).resolve().parents[1] / "src" / "vibe_launcher"


def fields(text):
    return {name for _, name, _, _ in Formatter().parse(text) if name is not None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sync", action="store_true")
    args = parser.parse_args()
    folder = ROOT / "locales"
    english = json.loads((folder / "en.json").read_text(encoding="utf-8"))
    keys = set()
    locations = {}
    for path in ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "tr" and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    keys.add(arg.value)
                    locations.setdefault(arg.value, f"{path.name}:{node.lineno}")
    # Labels that are looked up dynamically rather than passed as literal tr() arguments.
    import sys
    sys.path.insert(0, str(ROOT.parent))
    from vibe_launcher.shortcuts import LABELS
    from vibe_launcher.settings import BUILTIN_PROFILES
    from vibe_launcher.i18n import LANGUAGES
    catalogs = {locale: json.loads((folder / (locale + ".json")).read_text(encoding="utf-8"))
                for locale in LANGUAGES}
    keys.update(LABELS.values())
    keys.update(p["label"] for p in BUILTIN_PROFILES)
    if args.sync:
        for key in sorted(keys):
            if not re.fullmatch(r"[a-z_]+\.[a-f0-9]{12}", key):
                english.setdefault(key, key)
                for data in catalogs.values():
                    data.setdefault(key, key)
        catalogs["en"] = english
        for name, data in catalogs.items():
            (folder / (name + ".json")).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (folder / "sources.json").write_text(json.dumps(locations, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    errors = []
    for key in sorted(keys - english.keys()):
        errors.append("Missing source key: " + key)
    for locale, data in catalogs.items():
        if english.keys() != data.keys():
            errors.append(f"{locale}: missing {sorted(english.keys() - data.keys())}; extra {sorted(data.keys() - english.keys())}")
        for key in english.keys() & data.keys():
            original, translated = english[key], data[key]
            if not isinstance(original, str) or not isinstance(translated, str) or not translated.strip():
                errors.append(f"{locale}: empty or non-string value: {key}")
            elif ("\ufffd" in translated or re.search(r"\?{2,}", translated)
                  or translated.count("?") > original.count("?")):
                errors.append(f"{locale}: corrupt translation encoding: {key}")
            else:
                if re.search(r"\{[a-zA-Z_]\w*(?:[:!][^{}]*)?\}", original):
                    if fields(original) != fields(translated):
                        errors.append(f"{locale}: placeholder mismatch: {key}")
                for pattern, label in ((r"%(?:[-+0# ]*\d*(?:\.\d+)?)?[sd]", "percent format"),
                                       (r"</?[a-zA-Z][^<>]*>", "markup")):
                    if re.findall(pattern, original) != re.findall(pattern, translated):
                        errors.append(f"{locale}: {label} mismatch: {key}")
    print("\n".join(errors) if errors else f"Validated {len(english)} translations in {len(catalogs)} languages and {len(keys)} source keys")
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(main())
