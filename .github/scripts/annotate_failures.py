"""Turn failed unittest output into GitHub annotations.

Job logs need a signed-in GitHub user, but annotations are public through the checks API,
so failures can be read (and fixed) without access to the logs.

    python .github/scripts/annotate_failures.py test-output.txt
"""

import re
import sys

MAX_ANNOTATIONS = 9  # GitHub keeps 10 error annotations per step; the last one is the summary
MAX_CHARS = 3500


def escape(text, title=False):
    text = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if title:
        text = text.replace(":", "%3A").replace(",", "%2C")
    return text


def main(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        output = fh.read()
    blocks = re.split(r"^={50,}\n", output, flags=re.M)
    failures = []
    for block in blocks[1:]:
        head, _, body = block.partition("\n")
        if not re.match(r"^(FAIL|ERROR): ", head):
            continue
        body = re.split(r"^-{50,}\nRan \d+ tests?", body, flags=re.M)[0]
        body = body.strip("-\n")
        failures.append((head.strip(), body[-MAX_CHARS:]))
    for title, body in failures[:MAX_ANNOTATIONS]:
        print("::error title=%s::%s" % (escape(title, title=True), escape(body)))
    summary = [line for line in output.splitlines() if re.match(r"^(Ran \d+ tests?|FAILED|OK)\b", line)]
    names = ", ".join(t for t, _ in failures) or "no FAIL/ERROR blocks found"
    tail = "\n".join(output.splitlines()[-40:]) if not failures else ""
    print("::error title=Test summary::%s" % escape("%s\n%s\n%s" % (" | ".join(summary), names, tail)[:MAX_CHARS * 2]))


if __name__ == "__main__":
    main(sys.argv[1])
