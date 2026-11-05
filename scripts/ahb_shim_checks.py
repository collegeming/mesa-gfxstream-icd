#!/usr/bin/env python3
"""Static checks on the arm64 libahb_shim.so disassembly.

For both interposed symbols this asserts:
  * exactly one dlsym() call (the real symbol is resolved lazily, once),
  * a 'cbz x0' NULL branch on the incoming buffer that sits *before* any
    dlsym() call, so the NULL case cannot resolve the real symbol,
  * that NULL path ends by calling the matching shim logger
    (shim_log_acquire_null / shim_log_release_null) and never references dlsym
    or does an indirect 'br' tail branch,
  * the non-NULL path still tail-branches ('br') to the resolved function.

Usage: ahb_shim_checks.py <llvm-objdump> <libahb_shim.so>
"""
import re
import subprocess
import sys

# symbol -> logger the NULL path must end in
FUNCS = {
    "AHardwareBuffer_acquire": "shim_log_acquire_null",
    "AHardwareBuffer_release": "shim_log_release_null",
}
LABEL = re.compile(r"^([0-9a-f]+) <([^>]+)>:$")
INSN = re.compile(r"^\s*([0-9a-f]+):\s+(.*)$")


def parse(text):
    funcs = {}
    current = None
    for raw in text.splitlines():
        m = LABEL.match(raw.strip())
        if m:
            current = m.group(2)
            funcs[current] = []
            continue
        if current is None:
            continue
        m = INSN.match(raw)
        if not m:
            continue
        parts = m.group(2).strip().split(None, 1)
        funcs[current].append(
            (int(m.group(1), 16), parts[0], parts[1] if len(parts) > 1 else "")
        )
    return funcs


def fail(message):
    print("FAIL: " + message)
    sys.exit(1)


def check(name, logger, insns):
    dlsyms = [addr for addr, mnemonic, ops in insns
              if mnemonic in ("bl", "b") and "dlsym" in ops]
    if len(dlsyms) != 1:
        fail("%s: expected exactly one dlsym call, got %d" % (name, len(dlsyms)))
    resolve_at = dlsyms[0]

    candidates = [(addr, ops) for addr, mnemonic, ops in insns
                  if mnemonic == "cbz" and ops.startswith("x0")]
    if not candidates:
        fail("%s: no 'cbz x0' NULL branch" % name)
    early = [(addr, ops) for addr, ops in candidates if addr < resolve_at]
    if len(early) != 1:
        fail("%s: expected exactly one 'cbz x0' before dlsym@0x%x, got %d "
             "(a missing NULL check would look like this)"
             % (name, resolve_at, len(early)))

    _, ops = early[0]
    m = re.search(r"0x([0-9a-f]+)", ops)
    if m is None:
        fail("%s: cannot parse NULL branch target: %r" % (name, ops))
    target = int(m.group(1), 16)

    index = next((i for i, (addr, _, _) in enumerate(insns) if addr == target),
                 None)
    if index is None:
        fail("%s: NULL branch target 0x%x is outside the function"
             % (name, target))

    path = []
    while index < len(insns):
        path.append(insns[index])
        if insns[index][1] in ("b", "br", "ret"):
            break
        index += 1

    for addr, mnemonic, ops in path:
        if "dlsym" in ops:
            fail("%s: NULL path at 0x%x references dlsym" % (name, addr))
        if mnemonic == "br":
            fail("%s: NULL path at 0x%x tail-branches instead of returning"
                 % (name, addr))
    if logger not in path[-1][2]:
        fail("%s: NULL path ends in %r, expected a branch to %s"
             % (name, path[-1][2], logger))

    in_path = set(path)
    if not any(entry[1] == "br" for entry in insns if entry not in in_path):
        fail("%s: non-NULL path has no 'br' tail branch to the real function"
             % name)

    print("OK %s: dlsym@0x%x, NULL -> 0x%x (%d insns, %s, no dlsym/br), "
          "non-NULL tail branch present"
          % (name, resolve_at, target, len(path), logger))


def main():
    if len(sys.argv) != 3:
        fail("usage: ahb_shim_checks.py <llvm-objdump> <libahb_shim.so>")
    objdump, so_path = sys.argv[1], sys.argv[2]
    text = subprocess.run([objdump, "-d", "--no-show-raw-insn", so_path],
                          check=True, capture_output=True, text=True).stdout
    funcs = parse(text)
    for name, logger in FUNCS.items():
        if name not in funcs:
            fail("%s missing from disassembly" % name)
        check(name, logger, funcs[name])
    print("static checks PASS")


if __name__ == "__main__":
    main()
