"""
poll_job.py
Polls /result/{job_id} every 10 seconds and prints live status.

Usage:
    python poll_job.py JOB_ID_HERE

Example:
    python poll_job.py 3f2a1b4c-8d9e-4f2a-b1c3-2d4e5f6a7b8c
"""

import sys
import time
import json
import requests

if len(sys.argv) < 2:
    print("Usage: python poll_job.py JOB_ID")
    print("Example: python poll_job.py 3f2a1b4c-...")
    sys.exit(1)

job_id   = sys.argv[1]
base_url = "http://localhost:8000"
url      = f"{base_url}/result/{job_id}"

print(f"\nPolling: {url}")
print("Press Ctrl+C to stop\n")

start = time.time()

while True:
    try:
        r = requests.get(url, timeout=5)
        data = r.json()
        status   = data.get("status", "unknown")
        elapsed  = round(time.time() - start)
        filename = data.get("filename", "")

        print(f"[{elapsed:>4}s]  status={status}  file={filename}")

        if status == "done":
            result = data.get("result", {})
            invoice = result.get("invoice") or {}
            conf    = result.get("composite_score")
            tier    = result.get("confidence_tier")
            action  = result.get("action")
            errors  = result.get("validation_errors", [])
            warnings= result.get("format_warnings", [])

            print("\n" + "="*55)
            print("  EXTRACTION COMPLETE")
            print("="*55)
            print(f"  InvoiceId    : {invoice.get('InvoiceId')}")
            print(f"  SellerName   : {invoice.get('SellerName')}")
            print(f"  BuyerName    : {invoice.get('BuyerName')}")
            print(f"  IssueDate    : {invoice.get('IssueDate')}")
            print(f"  GrandTotal   : {invoice.get('InvGrandTotal')} {invoice.get('Currency')}")
            print(f"  IBAN         : {invoice.get('Iban')}")
            print(f"  Score        : {conf}  ({tier})")
            print(f"  Action       : {action}")
            print(f"  Val. errors  : {errors if errors else 'none'}")
            print(f"  Warnings     : {warnings if warnings else 'none'}")
            li = result.get("line_items", [])
            if li:
                print(f"\n  Line items   : {len(li)}")
                for i, item in enumerate(li, 1):
                    inv = item.get("invoice") or item
                    print(f"    {i}. {inv.get('ProductName')}  "
                          f"qty={inv.get('BilledQuantity')}  "
                          f"total={inv.get('LineTotalAmount')}")
            print("="*55)
            print(f"\nFull result saved to:  output/processed/latest/invoices.json")
            break

        elif status == "failed":
            print(f"\nFAILED: {data.get('error')}")
            break

        time.sleep(10)

    except requests.ConnectionError:
        print("[!] Cannot reach http://localhost:8000 — is uvicorn running?")
        time.sleep(5)
    except KeyboardInterrupt:
        print("\nStopped.")
        break
