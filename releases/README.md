# NOC_Beam binary releases

Built `.exe` artifacts for boss / field testing.

> **Why these live in git instead of as GitHub Releases:** the CI artifact
> path was failing on every push at the time these were dropped here.
> When CI is green again, prefer downloading from the workflow run's
> Artifacts tab or from a proper GitHub Release.

## Current

| File | Built from | SHA-256 | Native PJSIP | Size |
|---|---|---|---|---|
| `NOC_Beam-a730ba9.exe` | `a730ba9` (fix(ui): five functional gaps from end-to-end demo walkthrough — trace capture, accounts detail pane, button widths, history columns, theme picker, BOM-tolerant config) | `e94e06ce819e1a471280fa2f1968262f2f867b39086b40792e5e42aa522f1a0d` | yes (bundled `_pjsua2.pyd`) | 59.6 MB |
| `NOC_Beam-966bcbd.exe` | `966bcbd` (fix(ui): unbreak Add/Edit Account dialogs on PySide6 6.7+ — accounts now actually persist) | `74d5ec13afd7cb319837695ef21de4d2ad7893d15f8832353821525affec773c` | yes (bundled `_pjsua2.pyd`) | 59.6 MB |
| `NOC_Beam-5613e0e.exe` | `5613e0e` (fix(ui): pull last inline style into QSS — save still broken at this commit) | `e38319095b95612df201a459107b91432aaa679ffcb4e2a89b8862c0bf8fe88e` | yes (bundled `_pjsua2.pyd`) | 59.6 MB |
| `NOC_Beam-d358d16.exe` | `d358d16` (fix: deepen native sip smoke and stun wiring) | `0e10e69e8ef3cd58bfd73f300ba516210c5f1c1d7dd6fa4aa95ca3a92d21ec5f` | yes (bundled `_pjsua2.pyd`) | 55.5 MB |

## How to run

1. Download the `.exe`.
2. Right-click → Properties → Unblock (Windows SmartScreen will quarantine
   unsigned binaries downloaded from the internet).
3. Double-click. The phone shell opens.

## How to use the test runner

Hamburger menu (`≡`) → **Test Runner...**

- Paste caller numbers in the left box (one per line; matched against
  configured account usernames)
- Paste target numbers in the right box (one per line)
- Pick **mode**: matrix / paired / fan-out / fan-in
- Pick **pass criteria**: reachability (180 Ringing, then CANCEL) or
  full call (200 OK + hold N seconds, then BYE)
- Set **parallelism** (1..16)
- Click **Run N calls** — results stream in live; **Export CSV** when done
