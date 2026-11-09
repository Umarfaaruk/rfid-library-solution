# D8 User guide and training plan

Open the console in a browser at `http://<server>:8000/`. Demo accounts are synthetic (see README).

## Roles

| Role | Can do |
|---|---|
| Public | Search the catalogue |
| Assistant | Search, lend, return, renew, view members and reports |
| Librarian | Everything an assistant can, plus tag items, run inventory, simulate the gate, add members, import data |
| Admin | Everything, including users, audit log, backup |
| Integration | Machine access for NCIP and SIP2 clients only |

## Procedures

**Search the catalogue.** On "Search (OPAC)" type words from the title, author or ISBN and press Search. Green labels mean the copy is available. Red means on loan or reference only.

**Sign in.** Choose "Staff sign in". Use a username and password, or enter the card UID for a card tap. Failed attempts are recorded in the audit log, which an administrator can review.

**Lend an item.** Circulation desk, enter the member number (or card) and the item (accession number, barcode or tag UID), press Check out. The due date is shown. The security bit is switched off so the gate stays silent. If the system refuses, the message says why: reference item, blocked member, fine limit reached, loan limit reached, or item already on loan.

**Return an item.** Enter the item and press Check in. The security bit is switched back on. A fine is shown if it was late.

**Renew.** Enter the item and press Renew. Not allowed when overdue, at the renewal limit, or for a blocked member.

**Look up a member or item.** Use "Member status" or "Item status".

**Tag an item.** RFID tools, "Tag an item". Enter the item. Leave the tag UID empty to read the tag on the staff reader. The page confirms the tag-to-item link. A second tag on the same item is refused unless the API field `replace` is true (a replace checkbox on the screen is backlog).

**Check a shelf.** RFID tools, "Handheld inventory". Enter the shelf location and the tag UIDs read. The result lists missing items, misplaced items (with the shelf they belong to), items shown as on loan but found on the shelf, and unknown tags. A high beep means complete, a low beep means exceptions were found.

**Gate events.** "Security gate" simulates a walk-through. In production the gate sends this itself. An unlent item raises an alarm, saves a camera image and queues an email to the security address.

**Dashboard.** Shows titles, items, tagged percent, members, operators, RFID clients, circulation, overdue, gate alarms, footfall and fines. Type a shelf location to filter. "Download circulation CSV" exports loans.

**Admin.** Audit log, backup now, send queued notifications, health.

## Training plan

| Session | Audience | Length | Content | Check |
|---|---|---|---|---|
| 1 Counter basics | Assistants | 45 min | Sign in, search, lend, return, renew, reading refusals | Each person lends and returns 3 items |
| 2 Stock and tags | Librarians | 60 min | Tagging, replacing a tag, shelf inventory, reading exceptions | Tag 5 items, run one inventory with a planted misplaced item |
| 3 Gate and alerts | Librarians, security | 30 min | Alarm meaning, evidence image, email, false alarms | Trigger and clear an alarm |
| 4 Reports | Managers | 30 min | Dashboard, filters, CSV export | Produce a monthly circulation file |
| 5 Administration | Admins | 90 min | Users, backup and restore, migration dry-run and rollback, offline update and rollback, health and runbook | Restore a backup and compare counts |

Materials: this guide, the admin guide, and the 6 minute demo recording as a refresher. A short quick-reference card (one page) of the refusal messages is recommended.
