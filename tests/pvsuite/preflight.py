"""Preflight canary: before any test runs, prove that the federation is reachable
and that every fixture this depth needs still exists with its pinned size.

Anything that fails here is an ENVIRONMENT problem (federation down, fixture
drifted, scratch full) and aborts the run with a distinct exit code; it is never
reported as an application failure."""
import shutil
import threading

from .harness import fed


def canary(fx, timeout=25.0):
    """Cheap re-check of the federation; None if healthy, else a reason string.
    Also used between tests to decide whether a failure smells environmental."""
    result = {}

    def go():
        try:
            size = fed.file_size(fx["canary"])
            result["ok"] = size
        except Exception as e:      # noqa: BLE001
            result["err"] = f"{type(e).__name__}: {e}"
    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return f"canary timed out after {timeout:.0f}s"
    return result.get("err")


def run(fx, depth, data_dir):
    """Returns (problems, checked). `problems` is a list of strings, each
    starting with 'federation:', 'fixture:' or 'scratch:'."""
    problems, checked = [], []

    def ok(msg):
        checked.append(msg)

    def bad(kind, msg):
        problems.append(f"{kind}: {msg}")

    def want_size(label, entry):
        try:
            got = fed.file_size(entry["path"])
        except fed.FedError as e:
            return bad("federation", f"{label} {entry['path']}: {e}")
        except FileNotFoundError:
            return bad("fixture", f"{label} {entry['path']} no longer exists")
        if got != entry["size"]:
            return bad("fixture", f"{label} {entry['path']} size {got} != pinned {entry['size']}")
        ok(f"{label} ({got} bytes)")

    # ---- quick ----------------------------------------------------------------
    try:
        names = [e["name"] for e in fed.ls("/pelicanplatform/test")]
        ok(f"director reachable, /pelicanplatform/test lists {len(names)} entries")
    except Exception as e:      # noqa: BLE001
        bad("federation", f"director / /pelicanplatform/test unreachable: {type(e).__name__}: {e}")
        return problems, checked          # nothing else is meaningful
    want_size("tiny_file", fx["tiny_file"])
    want_size("small_file", fx["small_file"])
    for key in ("file", "nested_file"):
        p = fx["missing"][key]
        parent, _, name = p.rpartition("/")
        try:
            listing = [e["name"].rstrip("/").rsplit("/", 1)[-1] for e in fed.ls(parent)]
            if name in listing:
                bad("fixture", f"missing.{key} {p} now EXISTS; pick another nonexistent path")
            else:
                ok(f"missing.{key} parent exists and lacks the name")
        except Exception as e:      # noqa: BLE001
            bad("federation", f"missing.{key} parent {parent}: {type(e).__name__}: {e}")
    prot = fx["protected"]["namespaces"]
    verdicts = {ns: fed.auth_probe(ns) for ns in prot}
    if not any(v == "auth_required" for v in verdicts.values()):
        bad("fixture", f"no protected namespace refused a token-less listing: {verdicts} "
                       f"(a namespace may have become public, or the federation is misbehaving)")
    else:
        ok(f"token-required namespace refuses token-less access: {verdicts}")

    if depth == "quick":
        return problems, checked

    # ---- standard -------------------------------------------------------------
    nd = fx["nested_dir"]
    try:
        got = fed.walk_files(nd["path"])
        want = {f["path"]: f["size"] for f in nd["files"]}
        if got != want:
            bad("fixture", f"nested_dir differs from pin: missing={sorted(set(want) - set(got))[:3]} "
                           f"extra={sorted(set(got) - set(want))[:3]} "
                           f"size-diff={[k for k in want if k in got and got[k] != want[k]][:3]}")
        else:
            ok(f"nested_dir ({len(got)} files) matches pin")
    except Exception as e:      # noqa: BLE001
        bad("federation", f"nested_dir walk: {type(e).__name__}: {e}")
    sp = fx["special_name_file"]
    try:
        names = {e["name"].rstrip("/").rsplit("/", 1)[-1]: e for e in fed.ls(sp["parent"])}
        if sp["listed_name"] not in names or int(names[sp["listed_name"]]["size"]) != sp["size"]:
            bad("fixture", f"special_name_file listing differs: {sorted(names)}")
        else:
            ok("special_name_file listed with '+' name and pinned size")
    except Exception as e:      # noqa: BLE001
        bad("federation", f"special_name_file listing: {type(e).__name__}: {e}")
    bl = fx["big_listing"]
    try:
        n = len(fed.ls(bl["path"]))
        if n < 1000:
            bad("fixture", f"big_listing {bl['path']} lists only {n} entries; it no longer exercises the >1000 case")
        else:
            ok(f"big_listing lists {n} entries (true count {bl['true_entries']})")
    except Exception as e:      # noqa: BLE001
        bad("federation", f"big_listing: {type(e).__name__}: {e}")
    hd = fx["hour_dir"]
    try:
        got = fed.walk_files(hd["path"])
        if got != {k: int(v) for k, v in hd["sizes"].items()}:
            bad("fixture", f"hour_dir differs from pin ({len(got)} files vs {hd['file_count']})")
        else:
            ok(f"hour_dir ({len(got)} files) matches pin")
    except Exception as e:      # noqa: BLE001
        bad("federation", f"hour_dir walk: {type(e).__name__}: {e}")
    try:
        parent = fx["missing"]["dir"].rpartition("/")[0]
        names = [e["name"].rstrip("/").rsplit("/", 1)[-1] for e in fed.ls(parent)]
        if fx["missing"]["dir"].rsplit("/", 1)[-1] in names:
            bad("fixture", f"missing.dir {fx['missing']['dir']} now exists")
        else:
            ok("missing.dir parent exists and lacks the name")
    except Exception as e:      # noqa: BLE001
        bad("federation", f"missing.dir parent: {type(e).__name__}: {e}")

    if depth == "standard":
        return _scratch(problems, checked, data_dir, need_gb=1.0)

    # ---- deep -------------------------------------------------------------------
    for i, m in enumerate(fx["medium_files"]):
        want_size(f"medium_files[{i}]", m)
    mf = fx["many_files"]
    try:
        for p in mf["paths"]:
            h = p.rsplit("/", 1)[-1]
            got = fed.walk_files(p)
            pin = mf["per_dir"][h]
            if len(got) != pin["files"] or sum(got.values()) != pin["bytes"]:
                bad("fixture", f"many_files hour {h}: {len(got)} files/{sum(got.values())} bytes vs pin {pin}")
        ok("many_files hours match pins")
    except Exception as e:      # noqa: BLE001
        bad("federation", f"many_files walk: {type(e).__name__}: {e}")
    lr = fx["long_run"]
    try:
        got = fed.walk_files(lr["path"])
        if len(got) != lr["file_count"] or sum(got.values()) != lr["total_bytes"]:
            bad("fixture", f"long_run has {len(got)} files/{sum(got.values())} bytes vs pin "
                           f"{lr['file_count']}/{lr['total_bytes']}")
        else:
            ok(f"long_run ({len(got)} files) matches pin")
    except Exception as e:      # noqa: BLE001
        bad("federation", f"long_run walk: {type(e).__name__}: {e}")
    return _scratch(problems, checked, data_dir, need_gb=5.0)


def _scratch(problems, checked, data_dir, need_gb):
    try:
        free = shutil.disk_usage(data_dir).free / 1e9
        if free < need_gb:
            problems.append(f"scratch: only {free:.1f} GB free at {data_dir}, need >= {need_gb} GB")
        else:
            checked.append(f"scratch has {free:.0f} GB free at {data_dir}")
    except OSError as e:
        problems.append(f"scratch: cannot stat {data_dir}: {e}")
    return problems, checked
