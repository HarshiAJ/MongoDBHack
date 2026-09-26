"""Record a captioned demo video of the running app (http://localhost:8000).

    .venv/bin/python scripts/record_demo.py          # -> demo/rmi_agent_demo.mp4

Drives the real app with Playwright (installed Chrome), captures each step with a caption,
and stitches the frames into an MP4 with the ffmpeg bundled in imageio-ffmpeg.
Note: it opens a real review case, approves it and records the AluCast reply in the database.
"""

import subprocess
import sys
import time
from pathlib import Path

import imageio_ffmpeg
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "demo"
FRAMES = OUT / "frames"
URL = "http://localhost:8000"
W, H = 1600, 900
PY = str(ROOT / ".venv" / "bin" / "python")

shots = []  # (path, seconds)

CAPTION_JS = """([step, text]) => {
  document.getElementById('demo-cap')?.remove();
  const d = document.createElement('div'); d.id = 'demo-cap';
  d.style.cssText = 'position:fixed;left:0;right:0;bottom:0;padding:18px 32px;background:rgba(18,53,43,.94);' +
    'color:#F4F1E8;font:600 24px/1.35 -apple-system,Segoe UI,Roboto,sans-serif;z-index:99999;display:flex;gap:18px;align-items:center;pointer-events:none';
  d.innerHTML = step ? `<span style="background:#E3A73A;color:#2A1C05;border-radius:8px;padding:4px 12px;white-space:nowrap">${step}</span><span>${text}</span>` : `<span>${text}</span>`;
  document.body.appendChild(d);
  document.querySelector('main') && (document.querySelector('main').style.paddingBottom = '120px');
}"""


def shot(page, step, text, seconds=7):
    page.evaluate(CAPTION_JS, [step, text])
    page.wait_for_timeout(300)
    path = FRAMES / f"{len(shots):02d}.png"
    page.screenshot(path=str(path))
    shots.append((path, seconds))
    print(f"[{len(shots):02d}] {step} {text[:70]}")


def card(page, title, lines, seconds=6, dark=True):
    bg, fg, sub = ("#12352B", "#F4F1E8", "#C9DDD4") if dark else ("#F4F1E8", "#1E2B26", "#4A5A53")
    items = "".join(f"<li>{l}</li>" for l in lines)
    page.set_content(f"""<body style="margin:0;background:{bg};color:{fg};font-family:-apple-system,Segoe UI,Roboto,sans-serif;
      display:flex;flex-direction:column;justify-content:center;height:{H}px;padding:0 110px;box-sizing:border-box">
      <p style="color:#E3A73A;letter-spacing:3px;text-transform:uppercase;font-weight:600;font-size:20px">MongoDB Hackathon</p>
      <h1 style="font-size:64px;margin:0 0 24px">{title}</h1>
      <ul style="font-size:28px;line-height:1.6;color:{sub};padding-left:28px">{items}</ul></body>""")
    path = FRAMES / f"{len(shots):02d}.png"
    page.screenshot(path=str(path))
    shots.append((path, seconds))


def terminal(page, title, output, seconds=9):
    body = output.replace("&", "&amp;").replace("<", "&lt;")
    page.set_content(f"""<body style="margin:0;background:#0F1A16;color:#D6E6DE;font-family:Menlo,monospace;padding:60px;
      height:{H}px;box-sizing:border-box"><p style="color:#E3A73A;font:600 26px -apple-system,sans-serif">{title}</p>
      <pre style="font-size:22px;line-height:1.5;white-space:pre-wrap">{body}</pre></body>""")


def tab(page, name):
    page.click(f"nav button:has-text('{name}')")
    page.wait_for_function("!document.querySelector('#view').innerText.includes('Loading')", timeout=120000)
    page.wait_for_timeout(800)


def wait_case_done(page):
    page.wait_for_function("""() => { const b = document.querySelector('#casebox');
        return b && !b.querySelector('.proposal') && b.innerText.includes('done'); }""", timeout=300000)
    page.wait_for_timeout(800)


def main():
    FRAMES.mkdir(parents=True, exist_ok=True)
    for f in FRAMES.glob("*.png"):
        f.unlink()
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": W, "height": H})

        card(page, "RMI Agent", [
            "Raw material intelligence for automotive Purchasing &amp; Sales",
            "Metal price moves + contract fine print &#8594; approved actions + updated budget forecast",
            "MongoDB Atlas &#183; Vector Search (automated embeddings) &#183; LangGraph &#183; GPT-4.1",
        ], seconds=6)
        card(page, "The problem", [
            "7 inconsistent sources: SAP BW, Excel, BOM, sales, market indices, contract PDFs, emails",
            "Price formulas, triggers, caps and pass-through rules are buried in PDFs",
            "Validation is manual, so margin erosion and missed deadlines surface too late",
        ], seconds=7, dark=False)

        page.goto(URL)
        page.wait_for_function("!document.querySelector('#view').innerText.includes('Loading')", timeout=120000)
        page.wait_for_timeout(1000)
        shot(page, "Materials", "Seven sources cleaned into one MongoDB model. October contract prices vs budget: "
                                "copper +15%, aluminium up, steel down. Index history from a time series collection.", 8)

        tab(page, "Margins")
        shot(page, "1 · Margin watch", "Customer contract prices + surcharges vs supplier costs through the BOM. "
                                       "4 of 8 parts below target; Sakura's EV harness at 2.8%.", 9)

        tab(page, "Levers")
        shot(page, "2 · Customer levers", "Hardship, annual price review and price-down waiver clauses that apply, "
                                          "each with value and deadline (Brightline notice due 2 Nov).", 9)
        page.evaluate("document.querySelectorAll('.card')[1].scrollIntoView()")
        page.wait_for_timeout(500)
        shot(page, "3 · Vendor levers", "AluCast overcharge and late notice, Baltic decrease owed, Iberal RFQ triggers "
                                        "meet-competition, Keystone renewal vs a cheaper qualified offer.", 9)

        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.click("button:has-text('Let the agent prepare actions')")
        page.wait_for_selector("#casebox .proposal", timeout=300000)
        page.wait_for_timeout(800)
        page.evaluate("document.querySelector('#casebox').scrollIntoView()")
        page.wait_for_timeout(500)
        shot(page, "Agent", "LangGraph agent: engine scan + hybrid clause search ($rankFusion) + semantic negotiation "
                            "memory. It drafted one grounded action per finding and paused for approval.", 9)
        page.evaluate("document.querySelectorAll('#casebox .proposal')[0].scrollIntoView()")
        page.wait_for_timeout(500)
        shot(page, "Agent", "Each draft cites the contract section and uses only engine-computed figures. The case waits "
                            "in MongoDB (checkpointer + interrupt) until a buyer decides.", 9)

        page.click("#casebox button:has-text('Approve selected')")
        wait_case_done(page)
        page.evaluate("document.querySelector('#casebox').scrollIntoView()")
        shot(page, "Approved", "Approved emails become drafts ready to send; nothing leaves automatically. "
                               "The FY2027 forecast is re-run and the case report written.", 8)

        tab(page, "Agent cases")
        page.fill("#party", "V-100101")
        page.fill("#resp", (ROOT / "data" / "demo" / "alucast_reply_2026-09-29.txt").read_text())
        shot(page, "4 · Accept changes", "AluCast replies: 3.23 EUR/kg from November, October stays at 2.85. "
                                         "Paste the reply for the agent to read.", 7)
        page.click("button:has-text('Read response')")
        page.wait_for_selector("#casebox .proposal", timeout=300000)
        page.evaluate("document.querySelector('#casebox').scrollIntoView()")
        page.wait_for_timeout(500)
        shot(page, "4 · Accept changes", "The agent extracts both agreed prices, checks them against the contract "
                                         "formula, and waits for the buyer to confirm.", 8)
        page.click("#casebox button:has-text('Approve selected')")
        wait_case_done(page)

        tab(page, "Forecast vs Budget")
        shot(page, "4 · Re-plan", "FY2027 latest estimate vs approved budget, with the bridge: volume, price & "
                                  "surcharge, material. Stored as a new plan version in MongoDB.", 9)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(500)
        shot(page, "4 · Re-plan", "Accepted contract changes flow straight into the forecast.", 6)

        # live trigger: run the change-stream watcher and a simulated price tick, show the real output
        watcher = subprocess.Popen([PY, "-u", "-m", "rmi.agent.watcher"], cwd=ROOT, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True)
        time.sleep(6)
        sim = subprocess.run([PY, "scripts/simulate_price_move.py", "ALU", "3.0"], cwd=ROOT, capture_output=True,
                             text=True, env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"})
        lines, deadline = [], time.time() + 240
        while time.time() < deadline:
            line = watcher.stdout.readline()
            if line:
                lines.append(line.rstrip())
            if "waiting for approval" in line:
                break
        watcher.terminate()
        terminal(page, "Live trigger: MongoDB change stream",
                 "$ python scripts/simulate_price_move.py ALU 3.0\n" + sim.stdout.strip() +
                 "\n\n$ python -m rmi.agent.watcher\n" + "\n".join(lines))
        shot(page, "Real time", "A simulated aluminium tick updates index_monthly; the change stream opens a new "
                                "agent case on its own.", 9)

        page.goto(URL)
        page.wait_for_function("!document.querySelector('#view').innerText.includes('Loading')", timeout=120000)
        tab(page, "Ask")
        for q, cap in [
            ("If copper (RM-CU-ROD) rises another 10%, which customers and contracts are affected?",
             "Chat assistant with tools: $graphLookup over the entity graph gives relationship-aware context."),
            ("How many open data quality issues do we have per source system?",
             "Natural language to MongoDB: the assistant writes a read-only aggregation pipeline."),
        ]:
            page.fill("#q", q)
            page.click("main button.act:has-text('Ask')")  # the Ask button, not the Ask tab
            page.wait_for_selector("#pending", timeout=10000)
            page.wait_for_function("!document.getElementById('pending')", timeout=300000)
            page.wait_for_timeout(500)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            shot(page, "Ask", cap, 9)

        card(page, "Why it can be trusted", [
            "Numbers come from a deterministic engine; the LLM only uses the figures it is given",
            "Every extracted contract term has a verified verbatim quote (110/110 correct)",
            "Human approval before anything changes; row-level lineage on every record",
            "MongoDB: document model, time series, Vector Search + Atlas Search, $rankFusion,",
            "$graphLookup, change streams, LangGraph checkpoints, long-term memory",
        ], seconds=9)
        browser.close()

    # stitch frames into a video
    listing = OUT / "frames.txt"
    with listing.open("w") as f:
        for path, sec in shots:
            f.write(f"file '{path}'\nduration {sec}\n")
        f.write(f"file '{shots[-1][0]}'\n")
    video = OUT / "rmi_agent_demo.mp4"
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
                    "-vf", f"scale={W}:{H},fps=25,format=yuv420p", "-c:v", "libx264", "-preset", "medium",
                    "-crf", "20", str(video)], check=True, capture_output=True)
    print(f"\n{video} ({sum(s for _, s in shots)} s, {len(shots)} scenes)")


if __name__ == "__main__":
    sys.exit(main())
