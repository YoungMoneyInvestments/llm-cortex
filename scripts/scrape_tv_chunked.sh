#!/bin/bash
# chunked date-range scrape: use Gmail "before:" operator to walk back in time
set -e
OUT=/Users/cameronbennion/Projects/llm-cortex/.playwright-cli/tv_rows.ndjson
: > "$OUT"
BASE_Q='from%3Anoreply%40tradingview.com'

STORE_JS='(() => { const rows = Array.from(document.querySelectorAll("tr.zA")).map(r => { const span = r.querySelector("td.xW span[title]") || r.querySelector("td.xW span"); return { subject: r.querySelector(".y6 span")?.innerText||"", snippet: (r.querySelector(".y2")?.innerText||"").replace(/[\u200c\u00a0]/g," ").slice(0,180), date_label: span?.innerText||"", date_title: span?.getAttribute("title")||span?.getAttribute("aria-label")||"" }; }); window.localStorage.setItem("__tv_rows", JSON.stringify(rows)); return rows.length; })()'

parse_and_append() {
  RAW=$(playwright-cli -s=gmail localstorage-get __tv_rows 2>&1 | sed -n '/^### Result$/,/^### Ran/p' | sed '1d;$d')
  echo "$RAW" | python3 -c '
import sys, json
raw = sys.stdin.read().strip()
if raw.startswith("```"): raw = raw.strip("`").strip()
if raw.startswith("\"") and raw.endswith("\""): raw = json.loads(raw)
if raw.startswith("__tv_rows="): raw = raw[len("__tv_rows="):]
try: rows = json.loads(raw)
except Exception as e:
    sys.stderr.write(f"decode err: {e}\n"); sys.exit(0)
for r in rows: print(json.dumps(r))
' >> "$OUT"
}

# Walk back in 2-week chunks from now for 1 year
for before in 2026/04/19 2026/04/04 2026/03/20 2026/03/06 2026/02/20 2026/02/06 2026/01/23 2026/01/09 2025/12/26 2025/12/12 2025/11/28 2025/11/14 2025/10/31 2025/10/17 2025/10/03 2025/09/19 2025/09/05 2025/08/22 2025/08/08 2025/07/25 2025/07/11 2025/06/27 2025/06/13 2025/05/30 2025/05/16 2025/05/02 2025/04/18; do
  URL="https://mail.google.com/mail/u/0/#search/${BASE_Q}+before%3A${before//\//%2F}"
  playwright-cli -s=gmail goto "$URL" >/dev/null 2>&1
  sleep 2
  COUNT=$(playwright-cli -s=gmail eval "$STORE_JS" 2>&1 | sed -n '/^### Result$/,/^### Ran/p' | grep -Eo '^[0-9]+' | head -1)
  PRE=$(wc -l < $OUT)
  parse_and_append
  POST=$(wc -l < $OUT)
  echo "before=$before rows=$COUNT added=$((POST-PRE)) total=$POST" >&2
  if [ -z "$COUNT" ] || [ "$COUNT" -lt 1 ]; then break; fi
done
echo "DONE. File rows: $(wc -l < $OUT)" >&2
