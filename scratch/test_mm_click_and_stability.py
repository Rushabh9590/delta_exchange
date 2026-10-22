import subprocess
import time
import json
import urllib.request
import asyncio
import websockets
import base64
import os

CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
USER_DATA = r"C:\Users\itmvivanta.ahmedabad\AppData\Local\Temp\chrome_mm_stability_test_3"
ARTIFACT_DIR = r"C:\Users\itmvivanta.ahmedabad\.gemini\antigravity-ide\brain\c6d57a0b-2de3-4475-9947-a06e9c02d90f"
SCREENSHOT_PATH = os.path.join(ARTIFACT_DIR, "mm_modal_single_click_verified.png")

proc = subprocess.Popen([
    CHROME_PATH,
    "--headless=new",
    "--window-size=1440,900",
    "--remote-debugging-port=9231",
    f"--user-data-dir={USER_DATA}",
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
    "http://127.0.0.1:5000/#mm"
], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

time.sleep(3)

async def run_test():
    try:
        req = urllib.request.urlopen("http://127.0.0.1:9231/json")
        tabs = json.loads(req.read().decode())
        target_tab = None
        for t in tabs:
            if t.get("type") == "page":
                target_tab = t
                break
        
        ws_url = target_tab["webSocketDebuggerUrl"]

        async with websockets.connect(ws_url) as ws:
            msg_id = 1
            async def send_cmd(method, params=None):
                nonlocal msg_id
                cid = msg_id
                msg_id += 1
                payload = {"id": cid, "method": method}
                if params:
                    payload["params"] = params
                await ws.send(json.dumps(payload))
                while True:
                    resp = await ws.recv()
                    data = json.loads(resp)
                    if data.get("id") == cid:
                        return data.get("result")

            await send_cmd("Runtime.enable")
            await send_cmd("Page.enable")
            
            # Switch to MM tab explicitly
            await send_cmd("Runtime.evaluate", {
                "expression": """
                (function() {
                    if (typeof switchNavTab === 'function') {
                        switchNavTab('mm');
                    }
                    if (typeof loadMarketMakingStrikes === 'function') {
                        loadMarketMakingStrikes('BTC', '');
                    }
                })()
                """
            })

            # Poll until rows are loaded and rendered
            print("Waiting for MM strikes data to load...")
            for _ in range(20):
                await asyncio.sleep(1)
                chk = await send_cmd("Runtime.evaluate", {
                    "expression": """
                    (function() {
                        const tbody = document.getElementById('mmTbody');
                        const rows = tbody ? tbody.querySelectorAll('tr') : [];
                        const hasRealRows = rows.length > 0 && !tbody.querySelector('td[colspan]');
                        return { hasRealRows, count: rows.length };
                    })()
                    """,
                    "returnByValue": True
                })
                val = chk.get("result", {}).get("value", {})
                if val.get("hasRealRows"):
                    print(f"Loaded {val.get('count')} strike rows successfully.")
                    break

            # Uncheck "Show Only Qualified" and filter by 81200 (exact user scenario)
            filter_res = await send_cmd("Runtime.evaluate", {
                "expression": """
                (function() {
                    const chk = document.getElementById('mmChkOnlyQual');
                    if (chk) {
                        chk.checked = false;
                        if (typeof toggleMmOnlyQualified === 'function') {
                            toggleMmOnlyQualified(false);
                        }
                    }
                    const inp = document.getElementById('mmInpSearch');
                    if (inp) {
                        inp.value = '81200';
                        if (typeof onMmSearch === 'function') {
                            onMmSearch('81200');
                        }
                    }
                    const rows = document.querySelectorAll('#mmTbody tr');
                    return {
                        chkState: chk ? chk.checked : null,
                        matchingRows: rows.length,
                        symbols: Array.from(rows).map(r => r.getAttribute('data-symbol'))
                    };
                })()
                """,
                "returnByValue": True
            })
            print("Filter result (Show all + 81200):", filter_res)

            # Wait 3 seconds to verify table stability (ticks flowing from all 110 contracts, ZERO table wipes)
            print("Observing table stability under live ticks for 3s...")
            await asyncio.sleep(3)

            stability_check = await send_cmd("Runtime.evaluate", {
                "expression": """
                (function() {
                    const rows = document.querySelectorAll('#mmTbody tr');
                    const btns = document.querySelectorAll('#mmTbody .mm-btn-1lot');
                    return {
                        rowCount: rows.length,
                        buttonCount: btns.length,
                        firstBtnText: btns[0] ? btns[0].innerText.trim() : null,
                        symbols: Array.from(rows).map(r => r.getAttribute('data-symbol'))
                    };
                })()
                """,
                "returnByValue": True
            })
            print("Stability check result:", json.dumps(stability_check, ensure_ascii=True))

            # Single Click on the first MM button
            print("Executing SINGLE CLICK on 50-Lot MM button...")
            click_res = await send_cmd("Runtime.evaluate", {
                "expression": """
                (function() {
                    const btn = document.querySelector('#mmTbody .mm-btn-1lot');
                    if (!btn) return { error: 'No button found' };
                    btn.click();
                    const modal = document.getElementById('mmTradeModal');
                    return {
                        clicked: true,
                        modalDisplay: modal ? modal.style.display : null,
                        modalSymbol: document.getElementById('mmModalSymbol')?.innerText,
                        modalSize: document.getElementById('mmModalInpSize')?.value,
                        account: document.getElementById('mmModalAccount')?.innerText
                    };
                })()
                """,
                "returnByValue": True
            })
            print("Single Click Result:", json.dumps(click_res, ensure_ascii=True))

            # Capture screenshot
            shot = await send_cmd("Page.captureScreenshot", {"format": "png"})
            if shot and "data" in shot:
                with open(SCREENSHOT_PATH, "wb") as f:
                    f.write(base64.b64decode(shot["data"]))
                print(f"Screenshot saved to: {SCREENSHOT_PATH}")

    finally:
        proc.terminate()

if __name__ == "__main__":
    asyncio.run(run_test())
